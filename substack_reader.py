#!/usr/bin/env python3
"""
Substack Reader — Turns your Gmail into a calm reading page.

Works with your existing Gmail account. Pulls only newsletter *posts* from
Substack — reader replies, comment notifications, and other Substack messages
are left alone so you still see them in your inbox.

Setup:
  1. Enable IMAP in Gmail (Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP).
  2. Create an App Password (https://myaccount.google.com/apppasswords) — requires 2FA.
  3. Copy config.example.json to config.json and fill in your email + app password.
  4. (Optional) Set up a Gmail filter to auto-archive newsletter posts:
       From: *@substack.com
       Has the words: has:nouserlabels -"commented on" -"replied to" -"new subscriber" -"liked your"
       → Skip Inbox, Apply label "Newsletters"
     This keeps newsletter posts out of your inbox while reader messages come through normally.
  5. Run: python3 substack_reader.py
  6. Open the generated reading.html in your browser.

Dependencies: bleach, lxml (install via: pip install bleach lxml)
"""

import base64
import imaplib
import email
from email.header import decode_header
from html.parser import HTMLParser
import json
import os
import sys
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import bleach
from lxml import html as lxml_html
from lxml_html_clean import Cleaner

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
OUTPUT_PATH = SCRIPT_DIR / "reading.html"
STATE_PATH = SCRIPT_DIR / ".reader_state.json"  # tracks already-seen message IDs


def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: No config.json found. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    required = ["email", "app_password"]
    for key in required:
        if key not in cfg or not cfg[key]:
            print(f"ERROR: '{key}' is missing or empty in config.json")
            sys.exit(1)
    cfg.setdefault("imap_server", "imap.gmail.com")
    cfg.setdefault("max_articles", 50)
    cfg.setdefault("output_path", str(OUTPUT_PATH))
    cfg.setdefault("gmail_label", "")  # e.g. "Newsletters" — if set, searches this label instead of INBOX
    cfg.setdefault("auto_archive", False)  # if true, mark fetched newsletter posts as read and archive them
    return cfg


# ---------------------------------------------------------------------------
# State persistence (so we can append new articles on subsequent runs)
# ---------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen_ids": [], "articles": []}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

class HTMLTextExtractor(HTMLParser):
    """Fallback: strip HTML to plain text."""
    def __init__(self):
        super().__init__()
        self._pieces = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False
        if tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self._pieces.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._pieces.append(data)

    def get_text(self):
        return "".join(self._pieces).strip()


def decode_mime_header(value):
    if value is None:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def extract_body_html(msg):
    """Get the HTML body from a MIME message, falling back to plain text."""
    html_part = None
    text_part = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and html_part is None:
                html_part = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html_part = html_part.decode(charset, errors="replace")
            elif ct == "text/plain" and text_part is None:
                text_part = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                text_part = text_part.decode(charset, errors="replace")
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace") if payload else ""
        if ct == "text/html":
            html_part = decoded
        else:
            text_part = decoded

    return html_part, text_part


ALLOWED_TAGS = [
    'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'a', 'img',
    'blockquote', 'pre', 'code',
    'ul', 'ol', 'li',
    'em', 'i', 'strong', 'b', 'u', 's',
    'br', 'hr',
    'figure', 'figcaption',
    'sup', 'sub',
]

ALLOWED_ATTRS = {
    'a': ['href'],
    'img': ['src', 'alt'],
}


def clean_html(html_body):
    """
    Sanitize newsletter HTML down to clean, readable content.
    Uses lxml to extract body text, then bleach to whitelist safe tags.
    """
    if not html_body:
        return ""

    # Parse with lxml to handle malformed HTML and extract body
    try:
        doc = lxml_html.fromstring(html_body)
    except Exception:
        return ""

    # For Substack emails, extract the "body markup" div which contains only article content,
    # skipping all email header chrome (subtitle, author, date, cross-post info, avatars)
    body_markup = doc.find('.//*[@class="body markup"]')
    if body_markup is not None:
        doc = body_markup

    # Add whitespace around block elements before stripping, so text doesn't run together
    block_tags = {'div', 'td', 'tr', 'table', 'section', 'article', 'header', 'footer',
                  'nav', 'aside', 'main', 'center'}
    for el in doc.iter():
        if el.tag in block_tags:
            if el.text:
                el.text = ' ' + el.text
            if el.tail:
                el.tail = ' ' + el.tail
            else:
                el.tail = ' '

    # Remove elements we never want
    cleaner = Cleaner(
        scripts=True, javascript=True, style=True, comments=True,
        forms=True, meta=True, page_structure=True, processing_instructions=True,
        remove_tags=['span', 'div', 'table', 'tbody', 'thead', 'tr', 'td', 'th',
                     'font', 'center', 'section', 'article', 'header', 'footer', 'nav',
                     'aside', 'main'],
        kill_tags=['script', 'style', 'head', 'noscript', 'iframe', 'object', 'embed'],
    )
    try:
        cleaned_doc = cleaner.clean_html(doc)
    except Exception:
        return ""

    # Serialize back to HTML string
    raw_html = lxml_html.tostring(cleaned_doc, encoding='unicode')

    # Remove tracking pixels (1x1 images)
    raw_html = re.sub(
        r'<img[^>]*(?:width="1"|height="1")[^>]*/?\s*>',
        '', raw_html, flags=re.IGNORECASE
    )

    # Remove Substack UI icons — both direct and CDN-proxied versions
    # Direct: src="...substack.com/icon/..."
    # Proxied: src="...substackcdn.com/image/fetch/...substack.com%2Ficon%2F..."
    raw_html = re.sub(
        r'<img[^>]*src="[^"]*(?:substack\.com/icon/|substack\.com%2Ficon%2F)[^"]*"[^>]*/?\s*>',
        '', raw_html, flags=re.IGNORECASE
    )

    # Remove Substack author avatar/profile images
    raw_html = re.sub(
        r'<img[^>]*class="[^"]*(?:avatar|email-avatar)[^"]*"[^>]*/?\s*>',
        '', raw_html, flags=re.IGNORECASE
    )

    # Remove Substack app-link URLs BEFORE bleach (bleach strips complex URLs containing these patterns)
    raw_html = re.sub(
        r'<a[^>]*href="[^"]*(?:substack\.com/app-link|read-in-app|redirect=app-store|restack-comment|email-checkout)[^"]*"[^>]*>.*?</a>',
        '', raw_html, flags=re.IGNORECASE | re.DOTALL
    )

    # Cut at footer markers — only high-confidence patterns that won't match article text
    # Search from the END of the document to find the actual footer, not content matches
    footer_patterns = [
        # Substack-specific footers (very precise)
        (r"You're (?:currently )?a (?:free|paid) subscriber to\b", 0.5),
        (r"You're on the (?:free|paid) list for\b", 0.5),
        (r'A subscription gets you:', 0.7),
        (r'Upgrade to paid', 0.8),
        # Generic newsletter footers (require being in last 30% of content)
        (r'© \d{4}', 0.7),
        (r'Unsubscribe', 0.7),
        (r'Email preferences', 0.7),
        (r'Update your profile', 0.7),
        (r'Manage your subscription', 0.7),
        (r'Sent by Mailchimp', 0.7),
    ]
    for pattern, min_pct in footer_patterns:
        match = re.search(pattern, raw_html, re.IGNORECASE)
        if match and match.start() > len(raw_html) * min_pct:
            raw_html = raw_html[:match.start()]
            break  # Only cut once at the earliest footer match

    # Sanitize with bleach — only keep readable tags
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        strip=True,
    )

    # Remove empty tags left behind
    for _ in range(3):
        cleaned = re.sub(r'<(p|h[1-6]|blockquote|li|ul|ol|pre|figure|figcaption)\b[^>]*>[\s\u00a0]*</\1>',
                         '', cleaned, flags=re.IGNORECASE)

    # Remove unsubscribe/tracking links
    cleaned = re.sub(
        r'<a[^>]*href="[^"]*(?:unsubscribe|opt[_-]?out|email-preferences|list-manage)[^"]*"[^>]*>.*?</a>',
        '', cleaned, flags=re.IGNORECASE | re.DOTALL
    )

    # Fallback: remove any remaining Restack/READ IN APP links by link text (in case bleach stripped the URL)
    cleaned = re.sub(
        r'<a[^>]*>\s*Restack\s*</a>',
        '', cleaned, flags=re.IGNORECASE
    )

    # Decode Substack type-2 redirect URLs to actual destinations
    def decode_redirect_url(redirect_url):
        m = re.search(r'substack\.com/redirect/2/([^"?&]+)', redirect_url)
        if not m:
            return redirect_url
        try:
            payload = m.group(1).split('.')[0]
            payload += '=' * (4 - len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return data.get('e', redirect_url)
        except Exception:
            return redirect_url

    def replace_redirect_href(m):
        old_url = m.group(1)
        new_url = decode_redirect_url(old_url)
        if new_url != old_url:
            return m.group(0).replace(old_url, new_url.replace('&', '&amp;'))
        return m.group(0)

    cleaned = re.sub(
        r'<a([^>]*)href="(https?://substack\.com/redirect/2/[^"]+)"',
        lambda m: f'<a{m.group(1)}href="{decode_redirect_url(m.group(2)).replace(chr(38), "&amp;")}"',
        cleaned, flags=re.IGNORECASE
    )

    # Unwrap Substack image links (keep images, drop the wrapping <a> tag)
    def unwrap_substack_img_link(m):
        imgs = re.findall(r'<img[^>]*>', m.group(0))
        return ' '.join(imgs) if imgs else ''
    cleaned = re.sub(
        r'<a[^>]*href="[^"]*substack\.com[^"]*"[^>]*>\s*(?:<img[^>]*>\s*)*</a>',
        unwrap_substack_img_link, cleaned, flags=re.IGNORECASE
    )

    # Remove unclosed <a> tags (e.g. Substack wrapper links around entire article body)
    open_count = len(re.findall(r'<a\b', cleaned, re.IGNORECASE))
    close_count = len(re.findall(r'</a>', cleaned, re.IGNORECASE))
    if open_count > close_count:
        # Remove the extra opening <a> tags (typically substack redirect wrappers at the start)
        for _ in range(open_count - close_count):
            cleaned = re.sub(r'<a\b[^>]*>', '', cleaned, count=1, flags=re.IGNORECASE)

    # Remove empty links (no visible text)
    cleaned = re.sub(
        r'<a[^>]*>\s*</a>',
        '', cleaned, flags=re.IGNORECASE
    )

    # Wrap bare image captions (text not in any tag, directly after <img>)
    # This pattern only appears in NYT emails — Substack uses <figcaption> natively
    cleaned = re.sub(
        r'(<img[^>]*>)\s*([^<]{5,300}?)\s*(<(?:p|h[1-6]|ul|ol|figure|hr)\b)',
        lambda m: f'<figure>{m.group(1)}<figcaption>{m.group(2).strip()}</figcaption></figure>{m.group(3)}',
        cleaned, flags=re.IGNORECASE
    )

    # Remove zero-width/invisible Unicode characters and non-breaking space padding
    cleaned = re.sub(r'[\u200c\u200b\u200d\u034f\u00ad\ufeff\u2060\u2061\u2062\u2063\u2064]+', '', cleaned)
    # Collapse runs of non-breaking spaces (email preheader padding)
    cleaned = re.sub(r'(\u00a0\s*){3,}', ' ', cleaned)
    # Remove runs of whitespace-only text between tags
    cleaned = re.sub(r'>\s{3,}<', '><', cleaned)
    # Collapse whitespace
    cleaned = re.sub(r'\n\s*\n\s*\n', '\n\n', cleaned)

    # Final pass: remove empty tags created by link/image cleanup above
    for _ in range(3):
        cleaned = re.sub(r'<(p|h[1-6]|blockquote|li|ul|ol|pre|figure|figcaption)\b[^>]*>[\s\u00a0]*</\1>',
                         '', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


def extract_substack_author(from_header):
    """Parse the publication name from the From header."""
    # Typical format: "Newsletter Name <something@substack.com>"
    # or: "Person Name from Publication Name <something@substack.com>"
    match = re.match(r'^"?([^"<]+)"?\s*<', from_header)
    if match:
        name = match.group(1).strip()
        # Extract publication name after "from" (e.g. "Zvi Mowshowitz from Don't Worry About the Vase")
        from_match = re.search(r'\bfrom\s+(.+)$', name, re.IGNORECASE)
        if from_match:
            return from_match.group(1).strip()
        return name
    return from_header


def is_newsletter_post(msg):
    """
    Distinguish newsletter post deliveries from reader interactions.
    
    Newsletter posts:  "Author Name <something@substack.com>" with article content
    Reader messages:   Subjects/headers containing comment, reply, subscriber notifications
    """
    subject = decode_mime_header(msg["Subject"]).lower()
    from_hdr = decode_mime_header(msg["From"]).lower()

    # Skip notification emails (comments, replies, likes, new subscribers, digests)
    notification_patterns = [
        "commented on",
        "replied to",
        "new subscriber",
        "liked your",
        "new comment",
        "new reply",
        "someone replied",
        "digest for",
        "your post stats",
        "noreply@substack.com",
        "notifications@substack.com",
        "support@substack.com",
        "no-reply@substack.com",
    ]
    for pattern in notification_patterns:
        if pattern in subject or pattern in from_hdr:
            return False

    # Newsletter posts come from <name>@substack.com (not noreply/notifications)
    if "@substack.com" in from_hdr:
        return True

    return False


def fetch_articles(cfg):
    """Connect to IMAP, download Substack newsletter posts (not reader messages)."""
    print(f"Connecting to {cfg['imap_server']}...")
    mail = imaplib.IMAP4_SSL(cfg["imap_server"])
    mail.login(cfg["email"], cfg["app_password"])

    # If a Gmail label is configured, search there; otherwise search INBOX
    label = cfg.get("gmail_label", "")
    if label:
        # Gmail labels accessed via IMAP use the label name directly
        status, _ = mail.select(f'"{label}"')
        if status != "OK":
            print(f"Could not open label '{label}', falling back to INBOX.")
            mail.select("INBOX")
            label = ""
    else:
        mail.select("INBOX")

    search_location = f"label '{label}'" if label else "INBOX"
    print(f"Searching {search_location} for Substack emails...")

    # Search for all emails in this mailbox/label
    status, data = mail.search(None, "ALL")
    if status != "OK":
        print("Search failed.")
        return []

    msg_ids = data[0].split()
    print(f"Found {len(msg_ids)} emails from Substack.")

    # Take most recent N
    msg_ids = msg_ids[-cfg["max_articles"]:]

    state = load_state()
    seen = set(state["seen_ids"])
    articles = state["articles"]

    new_count = 0
    skipped_notifications = 0
    archive_ids = []

    for mid in msg_ids:
        mid_str = mid.decode()
        if mid_str in seen:
            continue

        status, msg_data = mail.fetch(mid, "(RFC822)")
        if status != "OK":
            continue

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = decode_mime_header(msg["Subject"])
        from_hdr = decode_mime_header(msg["From"])
        date_str = msg["Date"]
        message_id = msg["Message-ID"] or mid_str

        # Skip non-newsletter emails (verification codes, account notifications)
        skip_subjects = ['verification code', 'confirm your email', 'reset your password',
                         'verify your email', 'sign in to', 'login verification']
        if any(p in subject.lower() for p in skip_subjects):
            seen.add(mid_str)
            continue

        # Parse date
        try:
            date_tuple = email.utils.parsedate_to_datetime(date_str)
        except Exception:
            date_tuple = datetime.now(timezone.utc)

        author = extract_substack_author(from_hdr)

        # Extract original article URL from List-Post header or "View in browser" link
        original_url = ""
        list_post = msg.get("List-Post", "")
        lp_match = re.search(r'<(.+?)>', list_post)
        if lp_match:
            original_url = lp_match.group(1)

        html_body, text_body = extract_body_html(msg)

        # Extract subtitle from Substack's preview div (hidden email preview text)
        subtitle = ""
        if html_body:
            preview_matches = re.findall(r'class="preview"[^>]*>([^<]+)<', html_body)
            for p in preview_matches:
                text = p.strip()
                # Skip spacer divs full of unicode entities and very short strings
                if len(text) > 10 and not text.startswith('&#'):
                    # Clean up common prefixes
                    text = re.sub(r'^(?:Listen|Watch|Read) now \(\d+ mins?\)\s*\|\s*', '', text).strip()
                    if text:
                        subtitle = text
                    break

        if not original_url and html_body:
            vib = re.search(
                r'href="([^"]+)"[^>]*>\s*(?:View (?:in browser|online)|Read online)',
                html_body, re.IGNORECASE
            )
            if vib:
                original_url = vib.group(1)

        content_html = clean_html(html_body)

        # If no HTML, wrap plain text in <pre>
        if not content_html and text_body:
            content_html = f"<pre style='white-space:pre-wrap;'>{text_body}</pre>"

        if not content_html:
            seen.add(mid_str)
            continue

        articles.append({
            "title": subject,
            "author": author,
            "subtitle": subtitle,
            "date": date_tuple.isoformat(),
            "date_display": date_tuple.strftime("%B %-d, %Y"),
            "content_html": content_html,
            "message_id": message_id,
            "original_url": original_url,
        })

        seen.add(mid_str)
        archive_ids.append(mid)
        new_count += 1

    # Optionally auto-archive fetched newsletter posts (mark read + move out of inbox)
    if cfg.get("auto_archive") and archive_ids:
        print(f"Auto-archiving {len(archive_ids)} newsletter posts...")
        for mid in archive_ids:
            mail.store(mid, "+FLAGS", "\\Seen")
            # In Gmail IMAP, "archiving" = removing INBOX label
            mail.copy(mid, "[Gmail]/All Mail")
            mail.store(mid, "+FLAGS", "\\Deleted")
        mail.expunge()

    mail.logout()

    # Sort newest first
    articles.sort(key=lambda a: a["date"], reverse=True)

    # Save state
    state["seen_ids"] = list(seen)
    state["articles"] = articles
    save_state(state)

    print(f"Fetched {new_count} new articles ({len(articles)} total).")
    if skipped_notifications:
        print(f"Skipped {skipped_notifications} notification emails (comments, replies, etc.).")
    return articles


# ---------------------------------------------------------------------------
# Digest generation
# ---------------------------------------------------------------------------

SELECTION_PROMPT = """\
You are curating a weekly reading digest. Below are {count} articles from the past {days} days, given with index, publication, title, date, and a short excerpt.

Your job: select the {target} most digest-worthy articles.

Criteria for inclusion (in rough priority order):
- Substantive essays, arguments, and explorations — prefer depth over brevity
- News and developments that are genuinely significant, not routine updates
- Variety: if multiple articles cover the same event or story, pick the single best one
- Skip: pure paywalled previews with no real content, subscription upsells, meetup announcements, comment notifications, "open thread" posts with no content, and routine daily news roundups that don't add original reporting

Return ONLY a JSON array of the selected INDEX numbers, in the order you'd want them read (oldest to newest is fine). No explanation, no preamble — just the array.

Example: [3, 7, 12, 15, 22]

Articles:

{articles}
"""

DIGEST_PROMPT = """\
You are preparing a weekly reading digest. Below are articles from the past {days} days. Each article has an INDEX number — use it in your output exactly as shown.

Produce a digest in EXACTLY TWO sections, in this order. Do not merge them, skip either, or add extra sections.

SECTION 1 — output this h2 exactly:
<h2 class="digest-section">What Happened</h2>

News items, roundups, AI developments, current events, link roundups. For each entry:
- Start with: <p class="digest-byline">PUBLICATION · DATE</p>
- Then 1-2 tight sentences: what happened and why it matters. Be concise.
- Then a verbatim pull quote in <blockquote> tags (one sentence or short passage).
- Then a link: <a class="digest-source" data-article-index="INDEX">Read in app</a>
  (use the INDEX from the article header; if original_url is available also add href="URL")

SECTION 2 — output this h2 exactly:
<h2 class="digest-section">What People Are Thinking About</h2>

Essays, arguments, personal explorations, meditations, longer-form pieces. For each entry:
- Start with: <p class="digest-byline">PUBLICATION · DATE</p>
- Then one characterizing sentence capturing the move or angle of the piece (not a summary).
- Then 3-5 sentences of substance — what the piece actually says or does.
- Then a verbatim pull quote in <blockquote> tags.
- Then a link: <a class="digest-source" data-article-index="INDEX">Read in app</a>
  (use the INDEX from the article header; if original_url is available also add href="URL")

Rules:
- Every article goes in exactly one section.
- Skip only: pure paywalled previews with no real content, subscription upsells, comment notifications, and meetup announcements.
- Pull quotes must be verbatim from the article text.
- Do not editorialize. Do not say "the author argues" — just describe what the piece does.
- Wrap each entry in <div class="digest-entry">...</div>.
- Output starts immediately with the first <h2>. No preamble, no conclusion.

Here are the articles:

{articles}
"""

def articles_to_digest_input(articles, index_offset=1):
    """Format articles as plain text for the digest prompt. index_offset is position in full article list."""
    parts = []
    for i, (art, full_idx) in enumerate(articles):
        body = re.sub(r'<[^>]+>', ' ', art.get('content_html', ''))
        body = re.sub(r'\s+', ' ', body).strip()
        if len(body) > 4000:
            body = body[:4000] + '... [truncated]'
        entry = (
            f"--- ARTICLE INDEX={full_idx} ---\n"
            f"Publication: {art['author']}\n"
            f"Title: {art['title']}\n"
            f"Date: {art['date_display']}\n"
            f"URL: {art.get('original_url', '')}\n\n"
            f"{body}\n"
        )
        parts.append(entry)
    return "\n".join(parts)


def articles_to_selection_input(articles_with_indices):
    """Format articles as short summaries for the selection pass."""
    parts = []
    for art, full_idx in articles_with_indices:
        body = re.sub(r'<[^>]+>', ' ', art.get('content_html', ''))
        body = re.sub(r'\s+', ' ', body).strip()
        excerpt = body[:300]
        parts.append(
            f"INDEX={full_idx} | {art['author']} | {art['date_display']}\n"
            f"Title: {art['title']}\n"
            f"Excerpt: {excerpt}\n"
        )
    return "\n".join(parts)


def run_selection_pass(client, articles_with_indices, target, days):
    """Ask Claude to select the best `target` articles. Returns list of full_idx ints."""
    selection_input = articles_to_selection_input(articles_with_indices)
    prompt = SELECTION_PROMPT.format(
        count=len(articles_with_indices),
        days=days,
        target=target,
        articles=selection_input,
    )
    print(f"Selecting {target} articles from {len(articles_with_indices)}... ", end="", flush=True)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    print("done.")
    raw = response.content[0].text.strip()
    # Parse JSON array from response
    match = re.search(r'\[[\d,\s]+\]', raw)
    if not match:
        print(f"Warning: could not parse selection response, using all articles. Response: {raw[:200]}")
        return [idx for _, idx in articles_with_indices]
    try:
        selected = json.loads(match.group(0))
        return [int(x) for x in selected]
    except Exception:
        print(f"Warning: could not parse selection JSON, using all articles.")
        return [idx for _, idx in articles_with_indices]


def generate_digest(days=7):
    """
    Generate a weekly digest article from articles in the past `days` days.
    Returns an article dict suitable for prepending to the articles list, or None on failure.
    """
    cfg = load_config()
    api_key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: No anthropic_api_key in config.json and ANTHROPIC_API_KEY env var not set.")
        return None

    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic package not installed. Run: pip install anthropic")
        return None

    # Config options
    exclude_sources = cfg.get("digest_exclude", [])  # list of publication name substrings to skip
    selection_threshold = cfg.get("digest_selection_threshold", 50)  # run selection pass above this count
    selection_target = cfg.get("digest_selection_target", 40)  # how many to select down to

    state = load_state()
    all_articles = state.get("articles", [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for art in all_articles:
        try:
            date = datetime.fromisoformat(art["date"])
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date >= cutoff:
                recent.append(art)
        except Exception:
            continue

    # Apply exclude list
    if exclude_sources:
        before = len(recent)
        recent = [
            a for a in recent
            if not any(ex.lower() in a.get("author", "").lower() for ex in exclude_sources)
        ]
        excluded = before - len(recent)
        if excluded:
            print(f"Excluded {excluded} articles from {len(exclude_sources)} source(s).")

    if not recent:
        print(f"No articles found in the past {days} days.")
        return None

    # Sort oldest-first for the prompt so Claude reads them chronologically
    # Keep track of each article's index in the full list (digest will be prepended at 0,
    # so full articles are at indices 1..N)
    all_articles_sorted = sorted(all_articles, key=lambda a: a.get("date", ""), reverse=True)
    # Build a lookup from message_id to full-list index (1-based since digest is at 0)
    id_to_index = {a.get("message_id", ""): i + 1 for i, a in enumerate(all_articles_sorted)}

    recent_sorted = sorted(recent, key=lambda a: a["date"])
    recent_with_indices = [(art, id_to_index.get(art.get("message_id", ""), 0)) for art in recent_sorted]

    client = anthropic.Anthropic(api_key=api_key)

    # Selection pass if over threshold
    if len(recent_with_indices) > selection_threshold:
        print(f"{len(recent_with_indices)} articles exceeds threshold of {selection_threshold}.")
        selected_indices = run_selection_pass(client, recent_with_indices, selection_target, days)
        selected_set = set(selected_indices)
        recent_with_indices = [(art, idx) for art, idx in recent_with_indices if idx in selected_set]
        # Preserve order from selection response
        order = {idx: i for i, idx in enumerate(selected_indices)}
        recent_with_indices.sort(key=lambda x: order.get(x[1], 999))
        print(f"Selected {len(recent_with_indices)} articles for digest.")

    print(f"Generating digest from {len(recent_with_indices)} articles over the past {days} days...")

    articles_text = articles_to_digest_input(recent_with_indices)
    prompt = DIGEST_PROMPT.format(days=days, articles=articles_text)

    print("Calling Claude... ", end="", flush=True)
    digest_chunks = []
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for i, text in enumerate(stream.text_stream):
            digest_chunks.append(text)
            if i % 50 == 0:
                print(".", end="", flush=True)
    print(" done.")
    digest_html = "".join(digest_chunks).strip()

    # Post-process: convert data-article-index into onclick="showArticle(N)" calls
    def rewrite_article_link(m):
        full_tag = m.group(0)
        idx_m = re.search(r'data-article-index="(\d+)"', full_tag)
        href_m = re.search(r'href="([^"]+)"', full_tag)
        text_m = re.search(r'>([^<]+)<', full_tag)
        idx = idx_m.group(1) if idx_m else None
        href = href_m.group(1) if href_m else None
        label = text_m.group(1).strip() if text_m else "Read"
        if idx and int(idx) > 0:
            onclick = f'onclick="showArticle({idx}); return false;"'
            href_attr = f'href="{href}"' if href else 'href="#"'
            return f'<a class="digest-source" {href_attr} {onclick}>{label}</a>'
        elif href:
            return f'<a class="digest-source" href="{href}" target="_blank">{label}</a>'
        return full_tag

    digest_html = re.sub(
        r'<a[^>]*class="digest-source"[^>]*>.*?</a>',
        rewrite_article_link,
        digest_html,
        flags=re.DOTALL,
    )

    # Date range label — use the full recent set (before selection) for the date range
    all_recent = [art for art, _ in recent_with_indices] or recent
    oldest = min(all_recent, key=lambda a: a["date"])
    newest = max(all_recent, key=lambda a: a["date"])
    try:
        oldest_dt = datetime.fromisoformat(oldest["date"])
        newest_dt = datetime.fromisoformat(newest["date"])
        date_range = f"{oldest_dt.strftime('%B %-d')} – {newest_dt.strftime('%B %-d, %Y')}"
    except Exception:
        date_range = datetime.now().strftime("%B %-d, %Y")

    title = f"Weekly Digest: {date_range}"
    now = datetime.now(timezone.utc)

    digest_article = {
        "title": title,
        "author": "Digest",
        "subtitle": f"{len(recent_with_indices)} articles from the past {days} days",
        "date": now.isoformat(),
        "date_display": now.strftime("%B %-d, %Y"),
        "content_html": digest_html,
        "message_id": f"digest-{now.strftime('%Y-%m-%d')}",
        "original_url": "",
        "is_digest": True,
    }

    # Persist digest into state so it survives refreshes
    state = load_state()
    digests = state.get("digests", [])
    # Replace any existing digest with the same message_id (same day re-run)
    digests = [d for d in digests if d.get("message_id") != digest_article["message_id"]]
    digests.insert(0, digest_article)
    state["digests"] = digests
    save_state(state)

    print(f"Digest generated: {title}")
    return digest_article


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(articles, output_path):
    timestamp = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    count_str = f'{len(articles)} article{"s" if len(articles) != 1 else ""}'

    from html import escape

    article_blocks = []
    for i, art in enumerate(articles):
        mid = art.get('message_id', f'article-{i}')
        safe_title = escape(art['title'])
        safe_author = escape(art['author'])
        safe_mid = escape(mid, quote=True)
        # Content is already sanitized by bleach — safe to embed directly
        article_blocks.append(
            f'<article class="article" id="article-{i}" data-id="{safe_mid}" style="display:none;">'
            f'<div class="article-nav">'
            f'<button class="back-btn" onclick="history.back()">&larr; Back</button>'
            f'<button class="delete-btn" onclick="deleteArticle(\'{safe_mid}\')" aria-label="Remove" title="Remove">&times;</button>'
            f'</div>'
            f'<header class="article-header">'
            f'<div class="article-meta">{safe_author} &middot; {art["date_display"]}'
            + (f' &middot; <a href="{escape(art["original_url"], quote=True)}" class="original-link" target="_blank">View original</a>' if art.get("original_url") else '')
            + f'</div>'
            f'<h2 class="article-title">{safe_title}</h2>'
            + (f'<p class="article-subtitle">{escape(art["subtitle"])}</p>' if art.get("subtitle") else '')
            + f'</header>'
            f'<div class="article-body">'
            f'{art["content_html"]}'
            f'</div>'
            f'</article>'
        )

    toc_items = []
    for i, art in enumerate(articles):
        mid = art.get('message_id', f'article-{i}')
        safe_title = escape(art['title'])
        safe_author = escape(art['author'])
        safe_mid = escape(mid, quote=True)
        is_digest = art.get('is_digest', False)
        digest_badge = '<span class="toc-digest-badge">Digest</span>' if is_digest else ''
        toc_class = 'toc-item toc-item--digest' if is_digest else 'toc-item'
        toc_items.append(
            f'<div class="{toc_class}" data-toc-id="{safe_mid}" onclick="showArticle({i})">'
            f'<span class="toc-author">{safe_author}</span>'
            f'<span class="toc-title">{safe_title}{digest_badge}</span>'
            f'<span class="toc-date">{art["date_display"]}</span>'
            f'</div>'
        )

    toc_html = "".join(toc_items)

    # CSS as a raw string (no f-string escaping needed)
    css = """<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  :root {
    --bg: #f5f0e8;
    --bg-code: #ece6da;
    --text: #1a1a1a;
    --text-secondary: #7a7368;
    --accent: #a63e20;
    --border: #ddd6cb;
    --serif: 'Source Serif 4', Georgia, serif;
    --sans: 'IBM Plex Sans', -apple-system, sans-serif;
    --content-width: 860px;
    --toc-width: 960px;
  }

  [data-theme="dark"] {
    --bg: #1a1a1a;
    --bg-code: #252525;
    --text: #d4d0c8;
    --text-secondary: #8a8578;
    --accent: #d4785e;
    --border: #2e2e2e;
  }

  body {
    font-family: var(--serif);
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
    font-feature-settings: 'kern' 1, 'liga' 1, 'calt' 1;
    transition: background 0.3s, color 0.3s;
  }

  .page-header {
    max-width: var(--toc-width);
    margin: 0 auto;
    padding: 60px 32px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }
  .page-header h1 {
    font-family: var(--sans);
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
  }
  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .page-header .timestamp {
    font-family: var(--sans);
    font-size: 13px;
    color: var(--text-secondary);
  }
  .article-count {
    font-family: var(--sans);
    font-size: 13px;
    color: var(--text-secondary);
  }
  .theme-toggle {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    width: 34px;
    height: 34px;
    border-radius: 50%;
    font-size: 16px;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .theme-toggle:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .toc {
    max-width: var(--toc-width);
    margin: 0 auto;
    padding: 24px 32px 32px;
    border-bottom: 1px solid var(--border);
  }
  .toc-label {
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-secondary);
    margin-bottom: 12px;
  }
  .toc-item {
    display: flex;
    align-items: baseline;
    gap: 16px;
    padding: 7px 0;
    color: var(--text);
    cursor: pointer;
    transition: color 0.15s;
  }
  .toc-item:hover { color: var(--accent); }
  .toc-item--digest {
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 4px;
  }
  .toc-digest-badge {
    display: inline-block;
    font-family: var(--sans);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 3px;
    padding: 1px 5px;
    margin-left: 8px;
    vertical-align: middle;
    opacity: 0.8;
  }
  .toc-author {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    flex-shrink: 0;
    min-width: 160px;
  }
  .toc-title {
    font-size: 15px;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .toc-date {
    font-family: var(--sans);
    font-size: 12px;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .article {
    max-width: var(--content-width);
    margin: 0 auto;
    padding: 0 32px 80px;
  }
  .article-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 0 0;
  }
  .back-btn {
    font-family: var(--sans);
    font-size: 14px;
    font-weight: 500;
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    padding: 8px 0;
    transition: opacity 0.15s;
  }
  .back-btn:hover { opacity: 0.7; }
  .delete-btn {
    flex-shrink: 0;
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    width: 32px;
    height: 32px;
    border-radius: 50%;
    font-size: 18px;
    cursor: pointer;
    transition: all 0.15s;
    font-family: var(--sans);
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .delete-btn:hover {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .article-header {
    padding: 28px 0 36px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 36px;
  }
  .article-meta {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }
  .original-link {
    color: var(--accent);
    text-decoration: none;
    font-weight: 500;
  }
  .original-link:hover { text-decoration: underline; }
  .article-title {
    font-family: var(--serif);
    font-size: 32px;
    font-weight: 600;
    line-height: 1.2;
    letter-spacing: -0.02em;
    color: var(--text);
  }
  .article-subtitle {
    font-family: var(--serif);
    font-size: 19px;
    font-weight: 300;
    line-height: 1.5;
    color: var(--text-secondary);
    margin-top: 10px;
    font-style: italic;
  }
  .article-body {
    font-size: 19px;
    line-height: 1.75;
    letter-spacing: -0.003em;
    word-spacing: 0.01em;
  }
  .article-body p { margin-bottom: 1.15em; }
  .article-body h1, .article-body h2, .article-body h3, .article-body h4 {
    font-family: var(--serif);
    margin-top: 1.8em;
    margin-bottom: 0.5em;
    line-height: 1.25;
    letter-spacing: -0.015em;
  }
  .article-body h1 { font-size: 26px; }
  .article-body h2 { font-size: 22px; }
  .article-body h3 { font-size: 19px; font-weight: 600; }
  .article-body a {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 3px;
    text-decoration-color: color-mix(in srgb, var(--accent) 40%, transparent);
    transition: text-decoration-color 0.15s;
  }
  .article-body a:hover {
    text-decoration-color: var(--accent);
  }
  figure {
    margin: 1.8em 0;
    padding: 0;
    text-align: center;
  }
  figure img {
    margin-bottom: 0 !important;
  }
  figcaption {
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.4;
    color: var(--text-secondary);
    margin-top: 8px;
    padding: 0 3em;
    text-align: center;
    letter-spacing: 0.01em;
  }
  figcaption em {
    font-style: italic;
  }
  figcaption a {
    color: var(--text-secondary);
  }
  .article-body img {
    max-width: 100%;
    height: auto;
    border-radius: 6px;
    margin: 1.2em auto;
    display: block;
  }
  .article-body blockquote {
    border-left: 2px solid var(--accent);
    padding-left: 24px;
    margin: 1.6em 0;
    color: var(--text-secondary);
    font-style: italic;
    font-size: 18px;
  }
  .article-body pre {
    background: var(--bg-code);
    padding: 16px 20px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 14px;
    line-height: 1.5;
    margin: 1.2em 0;
  }
  .article-body ul, .article-body ol {
    margin: 1em 0;
    padding-left: 1.5em;
  }
  .article-body li { margin-bottom: 0.5em; }
  .article-body hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 2em 0;
  }

  .back-top {
    position: fixed;
    bottom: 28px;
    right: 28px;
    background: var(--accent);
    color: #fff;
    border: none;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    font-size: 18px;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.3s;
    font-family: var(--sans);
  }
  .back-top.visible { opacity: 1; }

  /* Digest styles */
  .digest-section {
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-secondary);
    margin: 2.8em 0 1.6em;
    padding-top: 2em;
    border-top: 1px solid var(--border);
  }
  .article-body .digest-section:first-child {
    margin-top: 0;
    padding-top: 0;
    border-top: none;
  }
  .digest-entry {
    margin-bottom: 2.4em;
    padding-bottom: 2.4em;
    border-bottom: 1px solid var(--border);
  }
  .digest-entry:last-child {
    border-bottom: none;
  }
  .digest-byline {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 0.5em !important;
    text-transform: none;
    letter-spacing: 0;
  }
  .digest-entry p { margin-bottom: 0.8em; }
  .digest-entry blockquote {
    border-left: 2px solid var(--accent);
    padding-left: 20px;
    margin: 1em 0;
    color: var(--text-secondary);
    font-style: italic;
    font-size: 17px;
    line-height: 1.6;
  }
  .digest-entry a.digest-source {
    display: inline-block;
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    color: var(--accent);
    text-decoration: none;
    margin-top: 0.4em;
    cursor: pointer;
  }
  .digest-entry a.digest-source:hover { text-decoration: underline; }

  /* Digest button + loading overlay */
  .digest-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    font-family: var(--sans);
    font-size: 12px;
    font-weight: 500;
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }
  .digest-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .digest-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .digest-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.45);
    z-index: 100;
    align-items: center;
    justify-content: center;
  }
  .digest-overlay.visible {
    display: flex;
  }
  .digest-overlay-box {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 36px 48px;
    text-align: center;
    font-family: var(--sans);
  }
  .digest-overlay-title {
    font-size: 15px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 12px;
  }
  .digest-overlay-sub {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 20px;
  }
  .digest-spinner {
    width: 28px;
    height: 28px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 1024px) {
    :root {
      --content-width: 100%;
      --toc-width: 100%;
    }
  }
  @media (max-width: 600px) {
    .page-header {
      padding: 40px 16px 16px;
      flex-direction: column;
      gap: 12px;
    }
    .header-right { align-self: flex-start; }
    .toc { padding: 16px 16px 24px; }
    .article-title { font-size: 24px; }
    .toc-item { flex-wrap: wrap; gap: 4px; }
    .toc-author { min-width: auto; }
    .toc-date { display: none; }
    .back-top { bottom: 16px; right: 16px; }
    .article { padding: 0 16px 60px; }
    .article-title { font-size: 24px; }
    .article-body { font-size: 17px; line-height: 1.75; }
    .article-body img { margin: 0.8em -16px; max-width: calc(100% + 32px); border-radius: 0; }
  }
</style>"""

    # JS as a raw string
    js = """<script>
    const btn = document.querySelector('.back-top');
    window.addEventListener('scroll', () => {
      btn.classList.toggle('visible', window.scrollY > 400);
    });

    function toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('substack-membrane-theme', next);
      updateThemeIcon();
    }
    function updateThemeIcon() {
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      document.querySelector('.theme-icon').textContent = isDark ? '\\u2600' : '\\u263E';
    }
    (function() {
      const saved = localStorage.getItem('substack-membrane-theme');
      if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.setAttribute('data-theme', 'dark');
      }
      updateThemeIcon();
    })();

    function showArticle(index, pushState) {
      document.getElementById('view-toc').style.display = 'none';
      document.querySelectorAll('.article').forEach(a => a.style.display = 'none');
      const target = document.getElementById('article-' + index);
      if (target) target.style.display = 'block';
      window.scrollTo(0, 0);
      if (pushState !== false) history.pushState({article: index}, '');
    }

    function showToc(pushState) {
      document.querySelectorAll('.article').forEach(a => a.style.display = 'none');
      document.getElementById('view-toc').style.display = 'block';
      window.scrollTo(0, 0);
      if (pushState !== false) history.pushState({toc: true}, '');
    }

    window.addEventListener('popstate', function(e) {
      if (e.state && e.state.article !== undefined) {
        showArticle(e.state.article, false);
      } else {
        showToc(false);
      }
    });

    history.replaceState({toc: true}, '');

    const STORAGE_KEY = 'substack-membrane-deleted';

    function getDeleted() {
      try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); }
      catch { return []; }
    }

    function deleteArticle(id) {
      // Remove article element
      const articles = document.querySelectorAll('.article');
      articles.forEach(a => { if (a.dataset.id === id) a.remove(); });
      // Remove from TOC
      const tocLink = document.querySelector('[data-toc-id="' + id + '"]');
      if (tocLink) tocLink.remove();
      // Persist
      const deleted = getDeleted();
      if (!deleted.includes(id)) deleted.push(id);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(deleted));
      updateCount();
      showToc(false);
      history.replaceState({toc: true}, '');
    }

    function updateCount() {
      const remaining = document.querySelectorAll('.toc-item').length;
      const countEl = document.querySelector('.article-count');
      if (countEl) countEl.textContent = remaining + ' article' + (remaining !== 1 ? 's' : '');
    }

    (function() {
      const deleted = getDeleted();
      deleted.forEach(id => {
        document.querySelectorAll('.article').forEach(a => { if (a.dataset.id === id) a.remove(); });
        const tocLink = document.querySelector('[data-toc-id="' + id + '"]');
        if (tocLink) tocLink.remove();
      });
      updateCount();
    })();

    // Digest generation
    function startDigest() {
      const btn = document.getElementById('digest-btn');
      const overlay = document.getElementById('digest-overlay');
      const sub = document.getElementById('digest-overlay-sub');
      btn.disabled = true;
      overlay.classList.add('visible');
      sub.textContent = 'Reading your newsletters\u2026';
      fetch('/digest?days=7')
        .then(r => r.json())
        .then(data => {
          if (data.status === 'started' || data.status === 'running') {
            pollDigest(overlay, sub, btn);
          } else {
            showDigestError(overlay, sub, btn, 'Unexpected response from server.');
          }
        })
        .catch(e => {
          showDigestError(overlay, sub, btn, 'Could not reach server. Are you running --serve?');
        });
    }

    function pollDigest(overlay, sub, btn) {
      const messages = [
        'Reading your newsletters\u2026',
        'Selecting articles\u2026',
        'Summarising\u2026',
        'Finding pull quotes\u2026',
        'Almost done\u2026',
      ];
      let tick = 0;
      const interval = setInterval(() => {
        tick++;
        sub.textContent = messages[Math.min(tick, messages.length - 1)];
        fetch('/digest/status')
          .then(r => r.json())
          .then(data => {
            if (data.status === 'done') {
              clearInterval(interval);
              overlay.classList.remove('visible');
              btn.disabled = false;
              window.location.reload();
            } else if (data.status === 'error') {
              clearInterval(interval);
              showDigestError(overlay, sub, btn, data.message || 'Generation failed.');
            }
            // still 'running' — keep polling
          })
          .catch(() => {
            // network hiccup — keep trying
          });
      }, 4000);
    }

    function showDigestError(overlay, sub, btn, msg) {
      overlay.classList.remove('visible');
      btn.disabled = false;
      alert('Digest error: ' + msg);
    }
</script>"""

    articles_html = "\n".join(article_blocks)

    # Assemble HTML by concatenation (no f-string — css/js contain braces)
    parts = [
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>Reading</title>\n',
        css,
        '\n</head>\n<body>\n'
        '  <div class="page-header">\n'
        '    <div>\n'
        '      <h1>Reading</h1>\n',
        f'      <div class="timestamp">Updated {timestamp}</div>\n'
        f'      <div class="article-count">{count_str}</div>\n',
        '    </div>\n'
        '    <div class="header-right">\n'
        '      <button class="digest-btn" onclick="startDigest()" id="digest-btn">Generate Digest</button>\n'
        '      <button class="theme-toggle" onclick="toggleTheme()" aria-label="Toggle dark mode" title="Toggle dark mode">\n'
        '        <span class="theme-icon"></span>\n'
        '      </button>\n'
        '    </div>\n'
        '  </div>\n'
        '\n'
        '  <div class="digest-overlay" id="digest-overlay">\n'
        '    <div class="digest-overlay-box">\n'
        '      <div class="digest-overlay-title">Generating digest\u2026</div>\n'
        '      <div class="digest-overlay-sub" id="digest-overlay-sub">Reading your newsletters</div>\n'
        '      <div class="digest-spinner"></div>\n'
        '    </div>\n'
        '  </div>\n'
        '\n'
        '  <div id="view-toc">\n'
        '    <nav class="toc">\n'
        '      <div class="toc-label">Contents</div>\n',
        toc_html,
        '\n    </nav>\n'
        '  </div>\n\n',
        articles_html,
        '\n  <button class="back-top" onclick="window.scrollTo(0,0)" aria-label="Back to top">&uarr;</button>\n',
        js,
        '\n</body>\n</html>',
    ]
    html = "".join(parts)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def refresh(digest_article=None):
    """Fetch new articles and regenerate the HTML file. Returns the output path."""
    cfg = load_config()
    articles = fetch_articles(cfg)
    output = cfg.get("output_path", str(OUTPUT_PATH))

    # Load persisted digests from state and prepend them
    state = load_state()
    saved_digests = state.get("digests", [])

    # If a fresh digest was just generated, it takes priority over the saved one for the same day
    if digest_article:
        saved_digests = [d for d in saved_digests if d.get("message_id") != digest_article.get("message_id")]
        saved_digests = [digest_article] + saved_digests

    articles = saved_digests + articles

    if articles:
        generate_html(articles, output)
    return output


def main():
    if "--serve" in sys.argv:
        serve()
        return

    # Parse --digest flag (optionally followed by number of days, e.g. --digest 7)
    digest_days = None
    args = sys.argv[1:]
    if "--digest" in args:
        idx = args.index("--digest")
        digest_days = 7  # default
        if idx + 1 < len(args):
            try:
                digest_days = int(args[idx + 1])
            except ValueError:
                pass

    if digest_days is not None:
        digest_article = generate_digest(days=digest_days)
        if digest_article:
            output = refresh(digest_article=digest_article)
            print(f"\nDone! Open {output} in your browser.")
        else:
            print("Digest generation failed.")
    else:
        output = refresh()
        if Path(output).exists():
            print(f"\nDone! Open {output} in your browser.")
        else:
            print("No articles found. Check your config and make sure Substack emails are arriving.")


def serve(port=8000):
    """Run a local HTTP server that refreshes articles on each page load."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    import urllib.parse

    cfg = load_config()
    output = cfg.get("output_path", str(OUTPUT_PATH))

    # Shared state for digest generation
    digest_state = {"running": False, "error": None}

    def run_digest(days):
        try:
            digest_article = generate_digest(days=days)
            if digest_article:
                refresh(digest_article=digest_article)
                digest_state["error"] = None
            else:
                digest_state["error"] = "Digest generation returned no content."
        except Exception as e:
            digest_state["error"] = str(e)
            print(f"Digest generation error: {e}")
        finally:
            digest_state["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path == "/digest":
                # Trigger digest generation in background thread
                params = urllib.parse.parse_qs(parsed.query)
                days = int(params.get("days", ["7"])[0])
                if digest_state["running"]:
                    self._json({"status": "running"})
                    return
                digest_state["running"] = True
                digest_state["error"] = None
                t = threading.Thread(target=run_digest, args=(days,), daemon=True)
                t.start()
                self._json({"status": "started"})
                return

            if parsed.path == "/digest/status":
                if digest_state["running"]:
                    self._json({"status": "running"})
                elif digest_state["error"]:
                    self._json({"status": "error", "message": digest_state["error"]})
                else:
                    self._json({"status": "done"})
                return

            if parsed.path in ("/", "/index.html"):
                try:
                    refresh()
                except Exception as e:
                    print(f"Refresh failed: {e}")
                try:
                    with open(output, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"No reading.html yet. Check your config.")
                return

            self.send_response(404)
            self.end_headers()

        def _json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    # Do an initial refresh before starting
    print("Initial refresh...")
    try:
        refresh()
    except Exception as e:
        print(f"Initial refresh failed: {e}")

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving at http://localhost:{port}")
    print(f"Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
