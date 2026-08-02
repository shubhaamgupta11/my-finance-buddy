"""
Blog tracker for Counterpoint Research insights.

Designed to be source-agnostic: add a new website by subclassing ``BlogSource``,
overriding ``extract_cards`` (and optionally ``build_message``), and appending it
to ``SOURCES`` at the bottom of this file. No other changes required.

Persistence uses a single source of truth: ``processed_links.txt`` (one URL per
line). An empty/missing file means "first run, nothing sent before", in which case
every post published *today* is sent.
"""

import os
import re
import sys
import time
import html as html_lib
import logging
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import List, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MEMORY_FILE = "processed_links.txt"
LOG_FILE = "tracker.log"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 20
TELEGRAM_TIMEOUT = 15
TELEGRAM_MAX_RETRIES = 3
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging():
    logger = logging.getLogger("tracker")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()


def _load_env_file(path=".env"):
    """Load KEY=VALUE pairs from a local .env file (local runs only).

    GitHub Actions supplies TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID via secrets;
    .env is purely an optional convenience for running the script locally.
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


_load_env_file()

# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------
@dataclass
class BlogPost:
    title: str
    link: str
    display_date: str
    category: str
    published: date  # parsed date, used for the "published today" filter


class BlogSource:
    """Base class for a blog listing source.

    Subclass and implement ``extract_cards``. Everything else (fetch, dedupe,
    notification) is handled generically.
    """

    name: str = "unnamed"
    listing_url: str = ""
    base_url: str = ""
    timezone: str = "UTC"
    date_format: str = "%B %d, %Y"

    # --- pure helpers ------------------------------------------------------
    def today(self) -> date:
        try:
            tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("[%s] Unknown timezone %r, falling back to UTC", self.name, self.timezone)
            tz = timezone.utc
        return datetime.now(tz).date()

    def parse_date(self, value: str) -> Optional[date]:
        try:
            return datetime.strptime(value, self.date_format).date()
        except (ValueError, TypeError):
            return None

    # --- I/O + scraping ----------------------------------------------------
    def fetch_posts(self) -> List[BlogPost]:
        log.info("[%s] Fetching %s ...", self.name, self.listing_url)
        try:
            res = requests.get(self.listing_url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.error("[%s] Request failed: %s", self.name, e)
            raise

        if res.status_code != 200:
            log.error("[%s] Unexpected HTTP status %s", self.name, res.status_code)
            raise RuntimeError(f"HTTP {res.status_code}")

        soup = BeautifulSoup(res.text, "html.parser")
        posts = self.extract_cards(soup)
        log.info("[%s] Found %d post(s) on page", self.name, len(posts))
        return posts

    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        raise NotImplementedError

    # --- notification ------------------------------------------------------
    def build_message(self, post: BlogPost) -> str:
        title = html_lib.escape(post.title)
        display_date = html_lib.escape(post.display_date)
        link = html_lib.escape(post.link)
        source = html_lib.escape(self.name)

        message = f"🔔 <b>New post on {source}!</b>\n\n<b>Title:</b> {title}"
        if post.category:
            message += f"\n<b>Category:</b> {html_lib.escape(post.category)}"
        message += f"\n<b>Date:</b> {display_date}\n\n🔗 <a href=\"{link}\">Read article</a>"
        return message


class CounterpointSource(BlogSource):
    name = "Counterpoint Research"
    listing_url = "https://counterpointresearch.com/en/insights"
    base_url = "https://counterpointresearch.com"
    timezone = "UTC"
    date_format = "%B %d, %Y"

    # Each card is an <a> to /en/insights/<slug> whose text is [category, title, date].
    link_pattern = re.compile(r"^/en/insights/[a-z0-9-]+/?$")

    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if not self.link_pattern.match(href):
                continue

            lines = [line.strip() for line in tag.get_text("\n").split("\n") if line.strip()]
            if len(lines) < 3:
                log.warning("[%s] Skipping card with unexpected structure (link=%s, lines=%s)",
                            self.name, href, lines)
                continue

            category, title, display_date = lines[0], lines[1], lines[2]
            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (link=%s)",
                            self.name, display_date, href)
                continue

            posts.append(BlogPost(
                title=title,
                link=urljoin(self.base_url, href),
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


# Register every supported website here. Add new sources by appending to this list.
SOURCES: List[BlogSource] = [CounterpointSource()]

# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
def send_telegram_message(text: str, max_retries: int = TELEGRAM_MAX_RETRIES) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets")
        return False

    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(1, max_retries + 1):
        try:
            res = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
            if res.status_code == 200:
                log.info("Telegram API accepted message (attempt %d)", attempt)
                return True
            try:
                description = res.json().get("description", res.text[:200])
            except ValueError:
                description = res.text[:200]
            log.error("Telegram API returned HTTP %s (attempt %d/%d): %s",
                      res.status_code, attempt, max_retries, description)
        except requests.RequestException as e:
            log.error("Telegram network error (attempt %d/%d): %s", attempt, max_retries, e)

        if attempt < max_retries:
            time.sleep(2 * attempt)

    return False


# ---------------------------------------------------------------------------
# Memory (single source of truth)
# ---------------------------------------------------------------------------
def load_processed_links() -> set:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            links = {line.strip() for line in f if line.strip()}
        log.info("Loaded %d processed link(s) from %s", len(links), MEMORY_FILE)
        return links

    log.info("No %s yet: first run, nothing sent before.", MEMORY_FILE)
    return set()


def save_processed_links(links: set) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        for link in sorted(links):
            f.write(link + "\n")
    log.info("Wrote %d processed link(s) to %s", len(links), MEMORY_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    started = datetime.now(timezone.utc)
    log.info("=" * 72)
    log.info("Blog tracker started (UTC: %s) | sources=%s",
             started.isoformat(), ", ".join(s.name for s in SOURCES))

    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets")
        return 1

    processed = load_processed_links()
    exit_code = 0
    total_new = 0
    total_sent = 0
    total_failed = 0

    for source in SOURCES:
        today = source.today()
        log.info("[%s] 'today' in %s = %s", source.name, source.timezone, today.isoformat())

        try:
            posts = source.fetch_posts()
        except Exception as e:
            log.error("[%s] Failed to scrape listing: %s", source.name, e)
            exit_code = 1
            continue

        today_posts = [p for p in posts if p.published == today]
        older_counts = {}
        for p in posts:
            if p.published != today:
                older_counts[p.published.isoformat()] = older_counts.get(p.published.isoformat(), 0) + 1
        log.info("[%s] Posts on page: %d | published today: %d | older (ignored): %s",
                 source.name, len(posts), len(today_posts),
                 ", ".join(f"{d}: {n}" for d, n in sorted(older_counts.items())) or "none")

        new_posts = [p for p in today_posts if p.link not in processed]
        log.info("[%s] New posts to notify: %d", source.name, len(new_posts))
        for p in new_posts:
            log.info("[%s]   NEW: [%s] %s (%s) -> %s", source.name, p.category, p.title, p.display_date, p.link)

        total_new += len(new_posts)
        for post in new_posts:
            if send_telegram_message(source.build_message(post)):
                total_sent += 1
                processed.add(post.link)
                log.info("[%s] Notified: %s", source.name, post.title)
            else:
                total_failed += 1
                log.error("[%s] FAILED to notify: %s -> %s", source.name, post.title, post.link)

    if total_sent:
        save_processed_links(processed)
    elif total_new == 0:
        log.info("No new posts published today. Nothing to notify.")
    else:
        log.warning("No notifications were delivered; memory not updated (will retry next run).")

    log.info("Summary: new=%d sent=%d failed=%d | elapsed=%.1fs",
             total_new, total_sent, total_failed,
             (datetime.now(timezone.utc) - started).total_seconds())
    log.info("=" * 72)

    if total_failed:
        log.error("%d notification(s) failed. Check Telegram token/chat-id and network.", total_failed)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unhandled exception in main")
        sys.exit(1)
