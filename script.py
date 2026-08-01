import os
import re
import sys
import time
import logging
import html as html_lib
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_URL = "https://counterpointresearch.com/en/insights"
BASE_URL = "https://counterpointresearch.com"
MEMORY_FILE = "processed_links.txt"
SEED_FILE = "last_link.txt"
STATUS_FILE = "sync_status.txt"
LOG_FILE = "tracker.log"
MAX_NOTIFY_PER_RUN = 10

INSIGHT_LINK_RE = re.compile(r"^/en/insights/[a-z0-9-]+/?$")
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _load_env_file(path=".env"):
    """Load KEY=VALUE pairs from a local .env file (for local runs only).

    GitHub Actions supplies TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID via secrets,
    so .env is purely an optional convenience for running the script locally.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and os.environ.get(key) is None:
                os.environ[key] = value
            log.info("Loaded %s from %s (env override wins)", key, path)


def setup_logging():
    logger = logging.getLogger("tracker")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()

_load_env_file()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def fetch_recent_blogs():
    """Fetch blog posts from the Counterpoint insights listing page.

    Each card is an <a> linking to /en/insights/<slug> whose text is exactly
    three lines: [category, title, date]. Newest posts come first.
    """
    log.info("Fetching %s ...", TARGET_URL)
    try:
        res = requests.get(TARGET_URL, headers=REQUEST_HEADERS, timeout=20)
    except requests.RequestException as e:
        log.error("Request failed for %s: %s", TARGET_URL, e)
        return []

    if res.status_code != 200:
        log.error("Unexpected HTTP status %s for %s", res.status_code, TARGET_URL)
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    blogs = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not INSIGHT_LINK_RE.match(href):
            continue

        lines = [line.strip() for line in tag.get_text("\n").split("\n") if line.strip()]
        if len(lines) < 3:
            log.warning("Skipping card with unexpected structure (link=%s, lines=%s)", href, lines)
            continue

        blogs.append(
            {
                "category": lines[0],
                "title": lines[1],
                "date": lines[2],
                "link": urljoin(BASE_URL, href),
            }
        )

    log.info("Scraped %d blog post(s)", len(blogs))
    return blogs


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
def build_message(blog):
    title = html_lib.escape(blog["title"])
    category = html_lib.escape(blog["category"])
    date = html_lib.escape(blog["date"])
    link = html_lib.escape(blog["link"])
    return (
        "🔔 <b>New blog on Counterpoint Research!</b>\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Category:</b> {category}\n"
        f"<b>Date:</b> {date}\n\n"
        f"🔗 <a href=\"{link}\">Read article</a>"
    )


def send_telegram_message(text, max_retries=3):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                log.info("Telegram API accepted message (attempt %d)", attempt)
                return True
            try:
                description = res.json().get("description", res.text[:200])
            except ValueError:
                description = res.text[:200]
            log.error(
                "Telegram API returned HTTP %s (attempt %d/%d): %s",
                res.status_code,
                attempt,
                max_retries,
                description,
            )
        except requests.RequestException as e:
            log.error("Telegram network error (attempt %d/%d): %s", attempt, max_retries, e)

        if attempt < max_retries:
            time.sleep(2 * attempt)

    return False


# ---------------------------------------------------------------------------
# State / memory
# ---------------------------------------------------------------------------
def load_processed_links():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            links = {line.strip() for line in f if line.strip()}
        log.info("Loaded %d processed link(s) from %s", len(links), MEMORY_FILE)
        return links

    if os.path.exists(SEED_FILE):
        with open(SEED_FILE, encoding="utf-8") as f:
            seed = f.read().strip()
        if seed:
            log.info("No %s found, seeding memory from %s: %s", MEMORY_FILE, SEED_FILE, seed)
            return {seed}

    log.info("No %s found and no seed available; starting with empty memory", MEMORY_FILE)
    return set()


def write_processed_links(links):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        for link in sorted(links):
            f.write(link + "\n")
    log.info("Wrote %d processed link(s) to %s", len(links), MEMORY_FILE)


def write_status(value):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        f.write(value)
    log.info("Status file %s set to '%s'", STATUS_FILE, value)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    started = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("Counterpoint Insights tracker started (UTC: %s)", started.isoformat())

    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets")
        write_status("no_update")
        sys.exit(1)

    blogs = fetch_recent_blogs()
    if not blogs:
        log.warning(
            "No blog posts found. Either the site structure changed or the request failed. "
            "Check tracker.log for details."
        )
        write_status("no_update")
        return

    processed = load_processed_links()
    new_blogs = [b for b in blogs if b["link"] not in processed]
    log.info("Total posts on page: %d | Already processed: %d | New: %d",
             len(blogs), len(processed), len(new_blogs))
    for b in new_blogs:
        log.info("  NEW: [%s] %s (%s)", b["category"], b["title"], b["date"])

    if not new_blogs:
        log.info("No new posts since last check. Nothing to notify.")
        write_status("no_update")
        return

    notified = 0
    failed = 0
    for blog in new_blogs[:MAX_NOTIFY_PER_RUN]:
        message = build_message(blog)
        if send_telegram_message(message):
            notified += 1
            processed.add(blog["link"])
            log.info("Notified: %s -> %s", blog["title"], blog["link"])
        else:
            failed += 1
            log.error("FAILED to notify: %s -> %s", blog["title"], blog["link"])

    if notified:
        write_processed_links(processed)
        write_status("update_committed")
    else:
        write_status("no_update")

    log.info(
        "Run finished in %.1fs | total=%d | notified=%d | failed=%d",
        (datetime.now(timezone.utc) - started).total_seconds(),
        len(blogs),
        notified,
        failed,
    )

    if failed:
        log.error("%d notification(s) failed. Check Telegram token/chat-id and network.", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
