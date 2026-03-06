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

Dependencies: None beyond Python 3.7+ standard library.
"""

import imaplib
import email
from email.header import decode_header
from html.parser import HTMLParser
import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

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


def clean_substack_html(html_body):
    """
    Strip Substack email chrome (header nav, footers, share buttons, etc.)
    and return just the article content. This is heuristic but works well.
    """
    if not html_body:
        return ""

    cleaned = html_body

    # Strip email HTML structure — extract body content only
    # Remove everything in <head>...</head>
    cleaned = re.sub(r'<head\b[^>]*>.*?</head>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    # Remove <html> and <body> wrapper tags (keep inner content)
    cleaned = re.sub(r'</?(?:html|body)\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
    # Remove <style> blocks
    cleaned = re.sub(r'<style\b[^>]*>.*?</style>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    # Remove <script> blocks
    cleaned = re.sub(r'<script\b[^>]*>.*?</script>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    # Remove XML declarations and doctype
    cleaned = re.sub(r'<\?xml[^>]*\?>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<!DOCTYPE[^>]*>', '', cleaned, flags=re.IGNORECASE)
    # Remove HTML comments
    cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
    # Remove tracking pixels (1x1 images, hidden images)
    cleaned = re.sub(r'<img[^>]*(?:width="1"|height="1"|display:\s*none)[^>]*/?\s*>', '', cleaned, flags=re.IGNORECASE)
    # Remove empty elements and whitespace-only divs/spans/tds
    cleaned = re.sub(r'<(div|span|td|tr|p)\b[^>]*>\s*</\1>', '', cleaned, flags=re.IGNORECASE)
    # Run empty element removal a few times to catch nested empties
    for _ in range(3):
        cleaned = re.sub(r'<(div|span|td|tr|table|p)\b[^>]*>\s*</\1>', '', cleaned, flags=re.IGNORECASE)
    # Remove all inline style attributes (we handle styling ourselves)
    cleaned = re.sub(r'\s+style="[^"]*"', '', cleaned, flags=re.IGNORECASE)
    # Remove class attributes (email-specific classes are meaningless here)
    cleaned = re.sub(r'\s+class="[^"]*"', '', cleaned, flags=re.IGNORECASE)
    # Remove data- attributes
    cleaned = re.sub(r'\s+data-[a-z-]+="[^"]*"', '', cleaned, flags=re.IGNORECASE)
    # Remove id attributes
    cleaned = re.sub(r'\s+id="[^"]*"', '', cleaned, flags=re.IGNORECASE)
    # Remove width/height/align/valign/bgcolor/border attributes
    cleaned = re.sub(r'\s+(?:width|height|align|valign|bgcolor|border|cellpadding|cellspacing|role)="[^"]*"', '', cleaned, flags=re.IGNORECASE)
    # Collapse excessive whitespace
    cleaned = re.sub(r'\n\s*\n\s*\n', '\n\n', cleaned)

    # Balance unclosed/extra closing tags so article content
    # doesn't break the page structure
    for tag in ('div', 'table', 'tr', 'td', 'span'):
        opens = len(re.findall(rf'<{tag}\b', cleaned, re.IGNORECASE))
        closes = len(re.findall(rf'</{tag}>', cleaned, re.IGNORECASE))
        if closes > opens:
            # Remove excess closing tags from the end
            for _ in range(closes - opens):
                idx = cleaned.rfind(f'</{tag}>')
                if idx == -1:
                    idx = cleaned.rfind(f'</{tag.upper()}>')
                if idx >= 0:
                    cleaned = cleaned[:idx] + cleaned[idx+len(f'</{tag}>'):]
        elif opens > closes:
            # Add missing closing tags
            cleaned += f'</{tag}>' * (opens - closes)

    # Remove common email footer/boilerplate patterns
    # Cut at typical footer markers
    footer_markers = [
        r'<div[^>]*class="[^"]*footer[^"]*"',
        r'<hr[^>]*/?>.*$',
        r'<table[^>]*class="[^"]*post-ufi[^"]*"',  # like/comment/share bar
        r"You're a.*subscriber",
        r'© \d{4}',
        r'Unsubscribe',
        r'Get the app',
    ]

    for marker in footer_markers:
        match = re.search(marker, cleaned, re.IGNORECASE | re.DOTALL)
        if match:
            # Only cut if we're past the halfway point of the content
            if match.start() > len(cleaned) * 0.3:
                cleaned = cleaned[:match.start()]

    # Remove dangerous/unwanted links:
    # 1. Links that wrap large blocks of content (tracking wrappers)
    # 2. Unsubscribe/manage subscription links
    # 3. "View in browser" links

    # Remove unsubscribe and management links entirely
    cleaned = re.sub(
        r'<a[^>]*href="[^"]*(?:unsubscribe|manage[_-]?subscription|opt[_-]?out|email-preferences|list-manage)[^"]*"[^>]*>.*?</a>',
        '', cleaned, flags=re.IGNORECASE | re.DOTALL
    )

    # Remove "view in browser" / "view online" links
    cleaned = re.sub(
        r'<a[^>]*href="[^"]*"[^>]*>[^<]*(?:view\s+(?:in\s+(?:your\s+)?browser|online|this\s+email))[^<]*</a>',
        '', cleaned, flags=re.IGNORECASE
    )

    # Unwrap links that contain block elements or very long content (tracking wrappers)
    # These are <a> tags wrapping entire paragraphs/divs — keep the inner content, remove the link
    def unwrap_wrapper_links(match):
        inner = match.group(1)
        # If the link contains block elements, it's a wrapper — unwrap it
        if re.search(r'<(?:p|div|table|tr|td|h[1-6]|blockquote|ul|ol|li)\b', inner, re.IGNORECASE):
            return inner
        # If the inner text is very long (>200 chars of text), likely a wrapper
        text_only = re.sub(r'<[^>]+>', '', inner)
        if len(text_only.strip()) > 200:
            return inner
        return match.group(0)

    cleaned = re.sub(r'<a\b[^>]*>(.*?)</a>', unwrap_wrapper_links, cleaned, flags=re.DOTALL)

    return cleaned


def extract_substack_author(from_header):
    """Parse the author/newsletter name from the From header."""
    # Typical format: "Newsletter Name <something@substack.com>"
    match = re.match(r'^"?([^"<]+)"?\s*<', from_header)
    if match:
        return match.group(1).strip()
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

        # Parse date
        try:
            date_tuple = email.utils.parsedate_to_datetime(date_str)
        except Exception:
            date_tuple = datetime.now(timezone.utc)

        author = extract_substack_author(from_hdr)
        html_body, text_body = extract_body_html(msg)
        content_html = clean_substack_html(html_body)

        # If no HTML, wrap plain text in <pre>
        if not content_html and text_body:
            content_html = f"<pre style='white-space:pre-wrap;'>{text_body}</pre>"

        if not content_html:
            seen.add(mid_str)
            continue

        articles.append({
            "title": subject,
            "author": author,
            "date": date_tuple.isoformat(),
            "date_display": date_tuple.strftime("%B %-d, %Y"),
            "content_html": content_html,
            "message_id": message_id,
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
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(articles, output_path):
    timestamp = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")

    article_blocks = []
    for i, art in enumerate(articles):
        mid = art.get('message_id', f'article-{i}')
        # Escape curly braces in content so they don't break the f-string
        safe_content = art['content_html'].replace('{', '&#123;').replace('}', '&#125;')
        safe_title = art['title'].replace('{', '&#123;').replace('}', '&#125;')
        article_blocks.append(f"""
        <article class="article" id="article-{i}" data-id="{mid}">
            <header class="article-header">
                <div class="article-header-row">
                    <div>
                        <div class="article-meta">{art['author']} · {art['date_display']}</div>
                        <h2 class="article-title">{safe_title}</h2>
                    </div>
                    <button class="delete-btn" onclick="deleteArticle(this)" aria-label="Remove article" title="Remove">&times;</button>
                </div>
            </header>
            <div class="article-body">
                {safe_content}
            </div>
        </article>
        """)

    toc_items = []
    for i, art in enumerate(articles):
        mid = art.get('message_id', f'article-{i}')
        safe_title = art['title'].replace('{', '&#123;').replace('}', '&#125;')
        toc_items.append(
            f'<div class="toc-item" data-toc-id="{mid}" onclick="showArticle({i})">'
            f'<span class="toc-author">{art["author"]}</span>'
            f'<span class="toc-title">{safe_title}</span>'
            f'<span class="toc-date">{art["date_display"]}</span>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reading</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,300;0,8..60,400;0,8..60,600;1,8..60,300;1,8..60,400&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  :root {{
    --bg: #f5f0e8;
    --bg-code: #ece6da;
    --text: #2c2c2c;
    --text-secondary: #7a7368;
    --accent: #b44a2d;
    --border: #ddd6cb;
    --serif: 'Source Serif 4', Georgia, serif;
    --sans: 'IBM Plex Sans', -apple-system, sans-serif;
    --content-width: 860px;
    --toc-width: 960px;
  }}

  [data-theme="dark"] {{
    --bg: #1a1a1a;
    --bg-code: #252525;
    --text: #d4d0c8;
    --text-secondary: #8a8578;
    --accent: #d4785e;
    --border: #2e2e2e;
  }}

  body {{
    font-family: var(--serif);
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
    transition: background 0.3s, color 0.3s;
  }}

  /* ---------- Header ---------- */
  .page-header {{
    max-width: var(--toc-width);
    margin: 0 auto;
    padding: 60px 32px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }}
  .page-header h1 {{
    font-family: var(--sans);
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
  }}
  .header-right {{
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .page-header .timestamp {{
    font-family: var(--sans);
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .article-count {{
    font-family: var(--sans);
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .theme-toggle {{
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
  }}
  .theme-toggle:hover {{
    border-color: var(--accent);
    color: var(--accent);
  }}

  /* ---------- Table of Contents ---------- */
  .toc {{
    max-width: var(--toc-width);
    margin: 0 auto;
    padding: 24px 32px 32px;
    border-bottom: 1px solid var(--border);
  }}
  .toc-label {{
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-secondary);
    margin-bottom: 12px;
  }}
  .toc-item {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    padding: 7px 0;
    text-decoration: none;
    color: var(--text);
    cursor: pointer;
    transition: color 0.15s;
  }}
  .toc-item:hover {{
    color: var(--accent);
  }}
  .toc-author {{
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    flex-shrink: 0;
    min-width: 160px;
  }}
  .toc-title {{
    font-size: 15px;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .toc-date {{
    font-family: var(--sans);
    font-size: 12px;
    color: var(--text-secondary);
    flex-shrink: 0;
  }}

  /* ---------- Articles ---------- */
  .articles {{
    max-width: var(--content-width);
    margin: 0 auto;
    padding: 0 32px 120px;
  }}

  .article {{
    padding: 56px 0;
    border-bottom: 1px solid var(--border);
  }}

  .article-header {{
    margin-bottom: 32px;
  }}
  .article-header-row {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
  }}
  .article-meta {{
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 8px;
    letter-spacing: 0.01em;
  }}
  .article-title {{
    font-family: var(--serif);
    font-size: 30px;
    font-weight: 600;
    line-height: 1.3;
    color: var(--text);
  }}
  .delete-btn {{
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
    margin-top: 4px;
  }}
  .delete-btn:hover {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }}

  .article-body {{
    font-size: 19px;
    line-height: 1.8;
  }}
  .article-body p {{
    margin-bottom: 1.2em;
  }}
  .article-body h1, .article-body h2, .article-body h3 {{
    font-family: var(--serif);
    margin-top: 1.6em;
    margin-bottom: 0.6em;
    line-height: 1.3;
  }}
  .article-body h1 {{ font-size: 26px; }}
  .article-body h2 {{ font-size: 22px; }}
  .article-body h3 {{ font-size: 19px; font-weight: 600; }}
  .article-body a {{
    color: var(--accent);
    text-decoration-thickness: 1px;
    text-underline-offset: 3px;
  }}
  .article-body img {{
    max-width: 100%;
    height: auto;
    border-radius: 6px;
    margin: 1.2em 0;
  }}
  .article-body blockquote {{
    border-left: 3px solid var(--accent);
    padding-left: 24px;
    margin: 1.6em 0;
    color: var(--text-secondary);
    font-style: italic;
  }}
  .article-body pre {{
    background: var(--bg-code);
    padding: 16px 20px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 14px;
    line-height: 1.5;
    margin: 1.2em 0;
  }}
  .article-body ul, .article-body ol {{
    margin: 1em 0;
    padding-left: 1.5em;
  }}
  .article-body li {{
    margin-bottom: 0.5em;
  }}

  /* Tame email HTML */
  .article-body table {{
    border: none !important;
    width: 100% !important;
  }}
  .article-body td {{
    border: none !important;
    padding: 0 !important;
  }}
  /* Tame inline styles from email HTML */
  .article-body * {{
    max-width: 100% !important;
  }}

  /* ---------- Article nav ---------- */
  .article-nav {{
    max-width: var(--content-width);
    margin: 0 auto;
    padding: 20px 32px 0;
  }}
  .back-btn {{
    font-family: var(--sans);
    font-size: 14px;
    font-weight: 500;
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    padding: 8px 0;
    transition: opacity 0.15s;
  }}
  .back-btn:hover {{
    opacity: 0.7;
  }}

  /* ---------- Fixed controls ---------- */
  .back-top {{
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
  }}
  .back-top.visible {{
    opacity: 1;
  }}

  /* ---------- Responsive ---------- */
  @media (max-width: 1024px) {{
    :root {{
      --content-width: 100%;
      --toc-width: 100%;
    }}
  }}
  @media (max-width: 600px) {{
    .page-header {{
      padding: 40px 16px 16px;
      flex-direction: column;
      gap: 12px;
    }}
    .header-right {{ align-self: flex-start; }}
    .toc {{ padding: 16px 16px 24px; }}
    .articles {{ padding: 0 16px 80px; }}
    .article {{ padding: 36px 0; }}
    .article-title {{ font-size: 24px; }}
    .article-body {{ font-size: 17px; line-height: 1.75; }}
    .article-body img {{ margin: 0.8em -16px; max-width: calc(100% + 32px); border-radius: 0; }}
    .toc-item {{ flex-wrap: wrap; gap: 4px; }}
    .toc-author {{ min-width: auto; }}
    .toc-date {{ display: none; }}
    .back-top {{ bottom: 16px; right: 16px; }}
    .article-nav {{ padding: 16px 16px 0; }}
  }}
</style>
</head>
<body>
  <div class="page-header">
    <div>
      <h1>Reading</h1>
      <div class="timestamp">Updated {timestamp}</div>
      <div class="article-count">{len(articles)} article{"s" if len(articles) != 1 else ""}</div>
    </div>
    <div class="header-right">
      <button class="theme-toggle" onclick="toggleTheme()" aria-label="Toggle dark mode" title="Toggle dark mode">
        <span class="theme-icon"></span>
      </button>
    </div>
  </div>

  <div id="view-toc">
    <nav class="toc">
      <div class="toc-label">Contents</div>
      {"".join(toc_items)}
    </nav>
  </div>

  <div id="view-article" style="display:none;">
    <div class="article-nav">
      <button class="back-btn" onclick="showToc()">&larr; Back</button>
    </div>
    <main class="articles">
      {"".join(article_blocks)}
    </main>
  </div>

  <button class="back-top" onclick="window.scrollTo({{top:0,behavior:'smooth'}})" aria-label="Back to top">↑</button>

  <script>
    const btn = document.querySelector('.back-top');
    window.addEventListener('scroll', () => {{
      btn.classList.toggle('visible', window.scrollY > 400);
    }});

    // Theme toggle
    function toggleTheme() {{
      const html = document.documentElement;
      const current = html.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('substack-membrane-theme', next);
      updateThemeIcon();
    }}
    function updateThemeIcon() {{
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      document.querySelector('.theme-icon').textContent = isDark ? '\u2600' : '\u263E';
    }}
    // Apply saved theme on load
    (function() {{
      const saved = localStorage.getItem('substack-membrane-theme');
      if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
        document.documentElement.setAttribute('data-theme', 'dark');
      }}
      updateThemeIcon();
    }})();

    // View switching — TOC vs single article
    function showArticle(index) {{
      console.log('[membrane] showArticle called with index:', index);
      const tocView = document.getElementById('view-toc');
      const articleView = document.getElementById('view-article');
      console.log('[membrane] view-toc element:', tocView);
      console.log('[membrane] view-article element:', articleView);
      tocView.style.display = 'none';
      articleView.style.display = 'block';
      const allArticles = document.querySelectorAll('.article');
      console.log('[membrane] total .article elements:', allArticles.length);
      allArticles.forEach(a => a.style.display = 'none');
      const target = document.getElementById('article-' + index);
      console.log('[membrane] target article-' + index + ':', target);
      if (target) {{
        target.style.display = 'block';
        const body = target.querySelector('.article-body');
        console.log('[membrane] article body element:', body);
        console.log('[membrane] article body innerHTML length:', body ? body.innerHTML.length : 'NO BODY');
        console.log('[membrane] article body first 200 chars:', body ? body.innerHTML.substring(0, 200) : 'NO BODY');
      }} else {{
        console.error('[membrane] could not find article-' + index);
      }}
      window.scrollTo({{top: 0}});
    }}

    function showToc() {{
      console.log('[membrane] showToc called');
      document.getElementById('view-article').style.display = 'none';
      document.getElementById('view-toc').style.display = 'block';
      window.scrollTo({{top: 0}});
    }}

    // Delete functionality — persists in localStorage
    const STORAGE_KEY = 'substack-membrane-deleted';

    function getDeleted() {{
      try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); }}
      catch {{ return []; }}
    }}

    function deleteArticle(button) {{
      const article = button.closest('.article');
      const id = article.dataset.id;
      article.style.transition = 'opacity 0.3s, max-height 0.4s, padding 0.4s, margin 0.4s';
      article.style.opacity = '0';
      article.style.overflow = 'hidden';
      setTimeout(() => {{
        article.style.maxHeight = '0';
        article.style.padding = '0';
        article.style.borderBottom = 'none';
        setTimeout(() => article.remove(), 400);
      }}, 300);

      // Hide from TOC
      const tocLink = document.querySelector(`[data-toc-id="${{id}}"]`);
      if (tocLink) tocLink.remove();

      // Persist
      const deleted = getDeleted();
      if (!deleted.includes(id)) deleted.push(id);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(deleted));

      // Update count and go back to TOC
      updateCount();
      showToc();
    }}

    function updateCount() {{
      const remaining = document.querySelectorAll('.article').length;
      const countEl = document.querySelector('.article-count');
      if (countEl) countEl.textContent = remaining + ' article' + (remaining !== 1 ? 's' : '');
    }}

    // On load, hide previously deleted articles and log diagnostics
    (function() {{
      const deleted = getDeleted();
      console.log('[membrane] deleted IDs from localStorage:', deleted);
      deleted.forEach(id => {{
        const article = document.querySelector(`.article[data-id="${{id}}"]`);
        if (article) article.remove();
        const tocLink = document.querySelector(`[data-toc-id="${{id}}"]`);
        if (tocLink) tocLink.remove();
      }});
      updateCount();

      // Diagnostic: check all articles
      const allArticles = document.querySelectorAll('.article');
      console.log('[membrane] total articles in DOM:', allArticles.length);
      allArticles.forEach(art => {{
        const body = art.querySelector('.article-body');
        const title = art.querySelector('.article-title');
        const bodyLen = body ? body.innerHTML.trim().length : -1;
        if (bodyLen < 50) {{
          console.warn('[membrane] EMPTY article:', art.id, 'title:', title ? title.textContent : 'NO TITLE', 'body length:', bodyLen);
        }}
      }});
    }})();
  </script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    articles = fetch_articles(cfg)
    if not articles:
        print("No articles found. Check your config and make sure Substack emails are arriving.")
        return
    output = cfg.get("output_path", str(OUTPUT_PATH))
    generate_html(articles, output)
    print(f"\nDone! Open {output} in your browser.")


if __name__ == "__main__":
    main()
