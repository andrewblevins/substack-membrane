# Substack Membrane

Pulls newsletters from your Gmail and renders them as a clean, calm reading page. No accounts, no apps, no tracking — just your email and a local HTML file.

Works with Substack, WordPress, Buttondown, Patreon, and any other newsletter that lands in your inbox.

![Preview](reading_preview.html)

## How it works

1. Connects to your Gmail over IMAP
2. Pulls emails from a label you choose (e.g. `Newsletters`)
3. Strips email chrome (footers, unsubscribe links, share buttons)
4. Generates a static HTML reading page with a table of contents
5. Tracks what it's already seen, so subsequent runs only fetch new articles

Articles can be dismissed with the delete button — deletions persist across refreshes.

## Requirements

- Python 3.7+ (no external dependencies)
- A Gmail account with 2FA enabled

## Setup

### 1. Create a Google App Password

You need an App Password so the script can access your Gmail over IMAP.

1. Go to [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. You may need to enable 2FA first if you haven't already
3. Create a new app password (name it whatever you like)
4. Copy the 16-character password Google generates

### 2. Set up a Gmail filter

Create a filter that sends newsletters to a dedicated label, keeping them out of your inbox:

1. In Gmail, go to **Settings** (gear icon) > **See all settings** > **Filters and Blocked Addresses** > **Create a new filter**
2. In the **From** field, enter the domains/addresses your newsletters come from, e.g.:
   ```
   @substack.com OR @wordpress.com OR newsletter@example.com
   ```
3. If you want comment notifications and replies to still reach your inbox, add exclusions in the **Has the words** field:
   ```
   -from:noreply@substack.com -from:notifications@substack.com -from:forum@mg1.substack.com
   ```
4. Click **Create filter**, then check:
   - **Skip the Inbox**
   - **Apply the label** — create a new one called `Newsletters` (or whatever you prefer)
5. Optionally check **Also apply filter to matching conversations** to catch existing emails

### 3. Configure the script

```bash
cd substack-membrane
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "email": "you@gmail.com",
    "app_password": "abcd efgh ijkl mnop",
    "imap_server": "imap.gmail.com",
    "max_articles": 50,
    "output_path": "reading.html",
    "gmail_label": "Newsletters",
    "auto_archive": false
}
```

| Field | Description |
|-------|-------------|
| `email` | Your Gmail address |
| `app_password` | The 16-character app password from step 1 |
| `imap_server` | Default `imap.gmail.com` — change if using a different provider |
| `max_articles` | Max number of emails to fetch per run |
| `output_path` | Where to write the generated HTML |
| `gmail_label` | Gmail label to search — leave empty `""` to search your inbox |
| `auto_archive` | If `true`, marks fetched emails as read and archives them |

### 4. Run it

```bash
python3 substack_reader.py
```

Then open `reading.html` in your browser.

Run it again anytime to fetch new articles — it remembers what it's already seen.

## Files

| File | Purpose |
|------|---------|
| `substack_reader.py` | Main script |
| `config.json` | Your credentials and settings (git-ignored) |
| `config.example.json` | Template config |
| `reading.html` | Generated reading page (git-ignored) |
| `reading_preview.html` | Demo page with sample articles |
| `.reader_state.json` | Tracks seen articles between runs (git-ignored) |

## Tips

- **Increase `max_articles`** if you have a large backlog to catch up on
- **Run on a schedule** with cron to keep your reading page fresh:
  ```
  0 */4 * * * cd /path/to/substack-membrane && python3 substack_reader.py
  ```
- **Delete articles** you've read by clicking the X button — deletions are stored in your browser's localStorage
