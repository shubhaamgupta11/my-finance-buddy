"""
Blog / report monitor for multiple websites.

Each website is a self-contained ``BlogSource`` subclass with its own
``extract_cards`` function, so sources never conflict with each other.
To add a new website: subclass ``BlogSource``, implement ``extract_cards``
(and ``extract_from_html`` if the page needs the raw response), and append an
instance to ``SOURCES``. Nothing else needs to change.

Sources with only a coarse publish date (e.g. FADA's month-level dates) can
override ``matches_date``; per-site memory still prevents re-sends.

Every cron run sends ONE aggregated Telegram report with a block per source:

    - Title + website link
    - "New reports: N" followed by a numbered list, or
    - "No new reports for now. Stay tuned" when there is nothing new, or
    - "Failed to fetch report" when scraping that source errored.

Memory is kept per website (``processed_<key>.txt``) so duplicates are never
re-sent and one website's state never touches another's.
"""

import os
import re
import sys
import time
import json
import html as html_lib
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import List, Optional
from urllib.parse import urljoin, quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOG_FILE = "tracker.log"
NOTIFICATION_LOG_FILE = "notified_message.txt"
IST = ZoneInfo("Asia/Kolkata")  # user-facing timezone for cron "today" filtering
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 20
REQUEST_MAX_RETRIES = 3
TELEGRAM_TIMEOUT = 15
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_MSG_LIMIT = 4000  # keep safely under Telegram's 4096-char limit
TITLE_LIMIT = 140
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


@dataclass
class SourceReport:
    source: "BlogSource"
    status: str  # "ok" | "error"
    new_posts: List[BlogPost] = field(default_factory=list)
    error: str = ""


class BlogSource:
    """Base class for a blog listing source.

    Subclass and implement ``extract_cards``. Everything else (fetch, dedupe,
    reporting) is handled generically and independently per source.
    """

    name: str = "unnamed"
    key: str = "unnamed"  # used for the per-source memory file name
    listing_url: str = ""
    base_url: str = ""
    timezone: str = "Asia/Kolkata"
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

    def matches_date(self, published: date) -> bool:
        """Whether a post with this publish date counts as "new".

        Defaults to published today. Sources whose listing only exposes a
        coarser granularity (e.g. month) override this.
        """
        return published == self.today()

    def extract_from_html(self, raw_html: str) -> List[BlogPost]:
        """Hook for sources that need the raw response (e.g. RSS, JSON blobs).

        The default parses the HTML and delegates to ``extract_cards``.
        """
        soup = BeautifulSoup(raw_html, "html.parser")
        return self.extract_cards(soup)

    # --- I/O + scraping ----------------------------------------------------
    def fetch_posts(self) -> List[BlogPost]:
        last_error: Optional[Exception] = None
        for attempt in range(1, REQUEST_MAX_RETRIES + 1):
            log.info("[%s] Fetching %s (attempt %d/%d) ...",
                     self.name, self.listing_url, attempt, REQUEST_MAX_RETRIES)
            try:
                res = requests.get(self.listing_url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                last_error = e
                log.error("[%s] Request failed (attempt %d/%d): %s",
                          self.name, attempt, REQUEST_MAX_RETRIES, e)
                if attempt < REQUEST_MAX_RETRIES:
                    time.sleep(2 * attempt)
                continue

            if res.status_code != 200:
                last_error = RuntimeError(f"HTTP {res.status_code}")
                log.error("[%s] Unexpected HTTP status %s (attempt %d/%d)",
                          self.name, res.status_code, attempt, REQUEST_MAX_RETRIES)
                if attempt < REQUEST_MAX_RETRIES:
                    time.sleep(2 * attempt)
                continue

            posts = self.extract_from_html(res.text)
            log.info("[%s] Found %d post(s) on page", self.name, len(posts))
            return posts

        raise last_error or RuntimeError("fetch_posts failed")

    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        raise NotImplementedError


class CounterpointSource(BlogSource):
    name = "Counterpoint Research"
    key = "counterpoint"
    listing_url = "https://counterpointresearch.com/en/insights"
    base_url = "https://counterpointresearch.com"
    timezone = "Asia/Kolkata"
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


class GartnerSource(BlogSource):
    name = "Gartner Newsroom"
    key = "gartner"
    listing_url = "https://www.gartner.com/en/newsroom/archive"
    base_url = "https://www.gartner.com"
    timezone = "Asia/Kolkata"
    date_format = "%b %d, %Y"

    # Each card is div.individual-block with an <a> to /en/newsroom/press-releases/<slug>,
    # a <h5> title and an eyebrow <p> containing "Category | <date>".
    eyebrow_date_re = re.compile(r"([A-Z][a-z]{2,3}\s+\d{1,2},\s+\d{4})")

    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        for card in soup.select("div.individual-block"):
            link_tag = card.find("a", href=True)
            if not link_tag or "/press-releases/" not in link_tag["href"]:
                continue

            title_tag = card.select_one("h5")
            if not title_tag:
                continue
            title = title_tag.get_text(" ", strip=True)
            if not title:
                continue

            category = ""
            display_date = ""
            eyebrow = card.select_one("p.rotate-90-acw")
            if eyebrow:
                eyebrow_text = eyebrow.get_text(" ", strip=True)
                parts = re.split(r"\s*\|\s*", eyebrow_text)
                category = parts[0].strip()
                match = self.eyebrow_date_re.search(eyebrow_text)
                if match:
                    display_date = match.group(1)

            published = self.parse_date(display_date) if display_date else None
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (link=%s)",
                            self.name, display_date or title, link_tag["href"])
                continue

            posts.append(BlogPost(
                title=title,
                link=urljoin(self.base_url, link_tag["href"]),
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


class CrisilSource(BlogSource):
    name = "CRISIL - All Our Thinking"
    key = "crisil"
    listing_url = "https://www.crisil.com/en/home/what-we-think/all-our-thinking.html"
    base_url = "https://www.crisil.com"
    timezone = "Asia/Kolkata"
    date_format = "%b %d, %Y"

    # Each card (div.crisil-cards) has a .card-title, .card-publish-date,
    # a .card-image-tag-text category and an <a class="card-redirection-link">.
    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        for card in soup.select("div.crisil-cards"):
            title_tag = card.select_one(".card-title")
            date_tag = card.select_one(".card-publish-date")
            link_tag = card.select_one("a.card-redirection-link")
            if not title_tag or not date_tag or not link_tag:
                log.warning("[%s] Skipping card missing title/date/link", self.name)
                continue

            title = title_tag.get_text(" ", strip=True)
            display_date = date_tag.get_text(" ", strip=True)
            if not title or not display_date:
                log.warning("[%s] Skipping card with empty title/date", self.name)
                continue

            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (title=%s)",
                            self.name, display_date, title)
                continue

            category_tag = card.select_one(".card-image-tag-text")
            category = category_tag.get_text(" ", strip=True) if category_tag else ""

            posts.append(BlogPost(
                title=title,
                link=urljoin(self.base_url, link_tag["href"]),
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


class CushmanSource(BlogSource):
    name = "Cushman & Wakefield India Insights"
    key = "cushman"
    listing_url = "https://www.cushmanwakefield.com/en/india/insights"
    base_url = "https://www.cushmanwakefield.com"
    timezone = "Asia/Kolkata"
    date_format = "%d/%m/%Y"
    date_re = re.compile(r"(\d{2}/\d{2}/\d{4})")

    # Each card (div.featuredContent) has a .rowItem-title category, a title
    # <a href="/en/..."> wrapping a .font-weight-bold, and a .rowItem-foot date.
    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        seen: set = set()
        for card in soup.select("div.featuredContent"):
            link_tag = card.select_one("a[href]")
            if not link_tag or not link_tag["href"].startswith("/en/"):
                continue

            href = link_tag["href"]
            if href in seen:
                continue

            title_tag = link_tag.select_one("div.font-weight-bold")
            title = (title_tag or link_tag).get_text(" ", strip=True)
            if not title:
                continue

            display_date = ""
            foot = card.select_one(".rowItem-foot")
            if foot:
                match = self.date_re.search(foot.get_text(" ", strip=True))
                if match:
                    display_date = match.group(1)

            published = self.parse_date(display_date) if display_date else None
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (link=%s)",
                            self.name, display_date or title, href)
                continue

            category_tag = card.select_one(".rowItem-title")
            category = category_tag.get_text(" ", strip=True) if category_tag else ""

            seen.add(href)
            posts.append(BlogPost(
                title=title,
                link=urljoin(self.base_url, href),
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


class TkcSource(BlogSource):
    """TKC publishes an RSS feed at /feed/; the post grid itself has no dates.

    The feed is XML, so this source parses the raw response instead of the
    default HTML path (BeautifulSoup's html.parser drops RSS <link> contents).
    """
    name = "TKC Perspective"
    key = "tkc"
    listing_url = "https://tkc.in/feed/"
    base_url = "https://tkc.in"
    timezone = "Asia/Kolkata"
    date_format = "%a, %d %b %Y %H:%M:%S %z"

    item_re = re.compile(r"<item>(.*?)</item>", re.S)
    cdata_re = re.compile(r"^<!\[CDATA\[|\]\]>$")

    def _tag_re(self, name: str):
        return re.compile(rf"<{name}>(.*?)</{name}>", re.S)

    def _clean(self, value: str) -> str:
        value = self.cdata_re.sub("", value.strip())
        return html_lib.unescape(value)

    def extract_from_html(self, raw_html: str) -> List[BlogPost]:
        posts: List[BlogPost] = []
        for item in self.item_re.findall(raw_html):
            title_m = self._tag_re("title").search(item)
            link_m = self._tag_re("link").search(item)
            date_m = self._tag_re("pubDate").search(item)
            if not (title_m and link_m and date_m):
                continue

            title = self._clean(title_m.group(1))
            link = self._clean(link_m.group(1))
            display_date = self._clean(date_m.group(1))
            if not title or not link:
                continue

            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping feed item with unparseable date %r (title=%s)",
                            self.name, display_date, title)
                continue

            category_m = self._tag_re("category").search(item)
            category = self._clean(category_m.group(1)) if category_m else ""

            posts.append(BlogPost(
                title=title,
                link=link,
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


class AnarockReportsSource(BlogSource):
    """Anarock renders its report list client-side into a Next.js JSON blob.

    The blob lives inside self.__next_f.push(...) script payloads with
    double-escaped quotes (\"); entries carry title/slug/date as YYYY-MM-DD.
    """
    name = "Anarock Research Reports"
    key = "anarock"
    listing_url = "https://www.anarock.com/research/research-reports"
    base_url = "https://www.anarock.com"
    timezone = "Asia/Kolkata"
    date_format = "%Y-%m-%d"

    blob_re = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
    entry_re = re.compile(
        r'\{"id":\d+,"documentId":"[^"]+","title":"([^"]+)","slug":"([^"]+)".*?"date":"([\d-]+)"',
        re.S,
    )

    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        # Each push payload is a JSON string literal; decoding it resolves
        # \" escapes and \uXXXX unicode sequences (e.g. \u0026 -> &) in titles.
        payloads = []
        for group in self.blob_re.findall(str(soup)):
            try:
                payloads.append(json.loads('"' + group + '"'))
            except ValueError:
                log.warning("[%s] Could not decode a Next.js payload, skipping", self.name)
        text = "".join(payloads)

        posts: List[BlogPost] = []
        seen: set = set()
        for title, slug, display_date in self.entry_re.findall(text):
            link = urljoin(self.base_url, f"/research/research-reports/{slug}")
            if link in seen:
                continue
            seen.add(link)

            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping entry with unparseable date %r (title=%s)",
                            self.name, display_date, title)
                continue

            posts.append(BlogPost(
                title=title,
                link=link,
                display_date=display_date,
                category="",
                published=published,
            ))
        return posts


class SiamSource(BlogSource):
    name = "SIAM Press Releases"
    key = "siam"
    listing_url = "https://www.siam.in/news-&-updates/press-releases"
    base_url = "https://www.siam.in"
    timezone = "Asia/Kolkata"
    date_format = "%d-%b-%Y"

    # Each card (.press_inner .pressbx_large) has <ul><li>date</li><li>city</li></ul>,
    # an <h6> title and a "View" link to /news-&-updates/press-releases/<slug>.
    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        seen: set = set()
        for card in soup.select(".press_inner .pressbx_large"):
            link_tag = card.find("a", href=True)
            title_tag = card.select_one("h6")
            date_lis = card.select("ul li")
            if not link_tag or not title_tag or not date_lis:
                continue

            href = link_tag["href"]
            if href in seen:
                continue

            title = title_tag.get_text(strip=True)
            display_date = date_lis[0].get_text(strip=True)
            if not title or not display_date:
                continue

            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (title=%s)",
                            self.name, display_date, title)
                continue

            city = date_lis[1].get_text(strip=True) if len(date_lis) > 1 else ""

            seen.add(href)
            posts.append(BlogPost(
                title=title,
                link=urljoin(self.base_url, href),
                display_date=display_date,
                category=city,
                published=published,
            ))
        return posts


class FadaSource(BlogSource):
    """FADA press releases only publish a month + year (e.g. "July, 2026").

    ``matches_date`` is overridden so any release from the current month counts
    as new; per-site memory still prevents repeats within the month.
    """
    name = "FADA Press Releases"
    key = "fada"
    listing_url = "https://fada.in/press-release-list.php"
    base_url = "https://fada.in"
    timezone = "Asia/Kolkata"
    date_format = "%B, %Y"

    def matches_date(self, published: date) -> bool:
        today = self.today()
        return (published.year, published.month) == (today.year, today.month)

    # Each card (.card.overflow-hidden) has a <h3> title, a .item7-card-desc
    # date/author row and a "Download" link to a PDF (path may contain spaces).
    def extract_cards(self, soup: BeautifulSoup) -> List[BlogPost]:
        posts: List[BlogPost] = []
        seen: set = set()
        for card in soup.select(".card.overflow-hidden"):
            title_tag = card.select_one("h3.font-weight-semibold")
            date_tag = card.select_one(".item7-card-desc a")
            link_tag = card.select_one("a.btn.btn-primary")
            if not title_tag or not date_tag or not link_tag or not link_tag.get("href"):
                continue

            title = title_tag.get_text(" ", strip=True)
            display_date = date_tag.get_text(" ", strip=True)
            if not title or not display_date:
                continue

            published = self.parse_date(display_date)
            if published is None:
                log.warning("[%s] Skipping card with unparseable date %r (title=%s)",
                            self.name, display_date, title)
                continue

            href = quote(link_tag["href"], safe="/:?&=#%@+,;$~*'()-.")
            link = urljoin(self.base_url, href)
            if link in seen:
                continue
            seen.add(link)

            desc_as = card.select(".item7-card-desc a")
            category = desc_as[1].get_text(strip=True) if len(desc_as) > 1 else "FADA"

            posts.append(BlogPost(
                title=title,
                link=link,
                display_date=display_date,
                category=category,
                published=published,
            ))
        return posts


# Register every supported website here. Add new sources by appending to this list.
SOURCES: List[BlogSource] = [
    CounterpointSource(),
    GartnerSource(),
    CrisilSource(),
    CushmanSource(),
    TkcSource(),
    AnarockReportsSource(),
    SiamSource(),
    FadaSource(),
]

# ---------------------------------------------------------------------------
# Report building (pure)
# ---------------------------------------------------------------------------
def truncate(text: str, limit: int = TITLE_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_source_block(report: SourceReport) -> str:
    source = report.source
    name = html_lib.escape(source.name)
    link = html_lib.escape(source.listing_url)

    block = f"🏢 <b>{name}</b>\n🔗 <a href=\"{link}\">Website</a>\n"

    if report.status == "error":
        block += "⚠️ <b>Failed to fetch report.</b> Please check the tracker log.\n"
        return block

    if not report.new_posts:
        block += "<b>New reports:</b> 0\n<i>No new reports for now. Stay tuned ✌️</i>\n"
        return block

    block += f"<b>New reports:</b> {len(report.new_posts)}\n"
    for index, post in enumerate(report.new_posts, start=1):
        title = html_lib.escape(truncate(post.title))
        post_link = html_lib.escape(post.link)
        block += f"{index}. <a href=\"{post_link}\">{title}</a>\n"
    return block


def _split_block(report: SourceReport, limit: int) -> List[str]:
    """Split a single oversized source block into <=limit chunks."""
    source = report.source
    head = (f"🏢 <b>{html_lib.escape(source.name)}</b>\n"
            f"🔗 <a href=\"{html_lib.escape(source.listing_url)}\">Website</a>\n")

    if report.status == "error":
        return [head + "⚠️ <b>Failed to fetch report.</b> Please check the tracker log.\n"]
    if not report.new_posts:
        return [head + "<b>New reports:</b> 0\n<i>No new reports for now. Stay tuned ✌️</i>\n"]

    chunks: List[str] = []
    chunk_lines = [head + f"<b>New reports:</b> {len(report.new_posts)}\n"]
    for index, post in enumerate(report.new_posts, start=1):
        line = (f"{index}. <a href=\"{html_lib.escape(post.link)}\">"
                f"{html_lib.escape(truncate(post.title))}</a>\n")
        if len("".join(chunk_lines)) + len(line) > limit:
            chunks.append("".join(chunk_lines))
            chunk_lines = [head + "<b>New reports (continued):</b>\n"]
        chunk_lines.append(line)
    chunks.append("".join(chunk_lines))
    return chunks


def build_report_messages(reports: List[SourceReport], limit: int = TELEGRAM_MSG_LIMIT) -> List[str]:
    """Build the aggregated report, splitting into <=limit messages if needed.

    Oversized source blocks are chunked by post; blocks/chunks are then packed
    into messages that account for the header and footer so nothing ever exceeds
    Telegram's 4096-char limit.
    """
    generated = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    header = f"📊 <b>Blog &amp; Report Monitor</b>\n🕒 <i>Generated {generated}</i>\n\n"
    footer = "\n—\n<i>Daily tracker report</i>"
    chunk_limit = max(100, limit - len(header) - len(footer))

    chunks: List[str] = []
    for report in reports:
        block = build_source_block(report)
        if len(block) <= chunk_limit:
            chunks.append(block)
        else:
            chunks.extend(_split_block(report, chunk_limit))

    messages: List[str] = []
    current = header
    for chunk in chunks:
        candidate = current + chunk + "\n"
        if len(candidate) + len(footer) > limit and current != header:
            messages.append(current.rstrip() + footer)
            current = header + chunk + "\n"
        else:
            current = candidate

    if current.strip():
        messages.append(current.rstrip() + footer)

    return messages or [header + footer]


# ---------------------------------------------------------------------------
# Notification log (what was published, for the user's visibility)
# ---------------------------------------------------------------------------
def write_notification_log(messages: List[str], reports: List[SourceReport]) -> None:
    """Record what is about to be sent to Telegram in notified_message.txt.

    The file is committed back to the repo by the workflow so the user always
    has visibility into what the tracker is posting.
    """
    ok_count = sum(1 for r in reports if r.status == "ok")
    failed = [r.source.name for r in reports if r.status == "error"]
    new_count = sum(len(r.new_posts) for r in reports)

    lines = [
        "=" * 72,
        f"Run: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Sources: {len(reports)} | ok: {ok_count} | failed: {len(failed)} | new reports: {new_count}",
    ]
    if failed:
        lines.append(f"Failed sources: {', '.join(failed)}")
    lines.append("=" * 72)
    lines.append("")

    for message in messages:
        lines.append(message)
        lines.append("")

    with open(NOTIFICATION_LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log.info("Wrote %d message(s) to %s", len(messages), NOTIFICATION_LOG_FILE)


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
# Memory (per-source source of truth)
# ---------------------------------------------------------------------------
def memory_path(source: BlogSource) -> str:
    return f"processed_{source.key}.txt"


def load_processed_links(source: BlogSource) -> set:
    path = memory_path(source)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            links = {line.strip() for line in f if line.strip()}
        log.info("[%s] Loaded %d processed link(s) from %s", source.name, len(links), path)
        return links

    log.info("[%s] No %s yet: first run, nothing sent before.", source.name, path)
    return set()


def save_processed_links(source: BlogSource, links: set) -> None:
    path = memory_path(source)
    with open(path, "w", encoding="utf-8") as f:
        for link in sorted(links):
            f.write(link + "\n")
    log.info("[%s] Wrote %d processed link(s) to %s", source.name, len(links), path)


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

    reports: List[SourceReport] = []

    for source in SOURCES:
        today = source.today()
        log.info("[%s] 'today' in %s = %s", source.name, source.timezone, today.isoformat())

        try:
            posts = source.fetch_posts()
        except Exception as e:
            log.error("[%s] Failed to scrape listing: %s", source.name, e)
            reports.append(SourceReport(source=source, status="error", error=str(e)))
            continue

        processed = load_processed_links(source)

        today_posts = [p for p in posts if source.matches_date(p.published)]
        older_counts = {}
        for p in posts:
            if not source.matches_date(p.published):
                older_counts[p.published.isoformat()] = older_counts.get(p.published.isoformat(), 0) + 1
        log.info("[%s] Posts on page: %d | published today: %d | older (ignored): %s",
                 source.name, len(posts), len(today_posts),
                 ", ".join(f"{d}: {n}" for d, n in sorted(older_counts.items())) or "none")

        new_posts = [p for p in today_posts if p.link not in processed]
        for p in new_posts:
            log.info("[%s]   NEW: [%s] %s (%s) -> %s",
                     source.name, p.category, p.title, p.display_date, p.link)

        reports.append(SourceReport(source=source, status="ok", new_posts=new_posts))

    messages = build_report_messages(reports)
    log.info("Built report: %d message(s), %d source block(s)",
             len(messages), len(reports))
    for i, msg in enumerate(messages, start=1):
        log.info("  message %d/%d: %d chars", i, len(messages), len(msg))

    write_notification_log(messages, reports)

    delivered = True
    for i, message in enumerate(messages, start=1):
        if not send_telegram_message(message):
            delivered = False
            log.error("Failed to send report message %d/%d", i, len(messages))

    if not delivered:
        log.warning("Report was not fully delivered; per-source memory NOT updated (will retry).")
        return 1

    # Persist memory only after the report was delivered successfully.
    for report in reports:
        if report.status == "ok" and report.new_posts:
            processed = load_processed_links(report.source)
            for post in report.new_posts:
                processed.add(post.link)
            save_processed_links(report.source, processed)

    failed_sources = [r.source.name for r in reports if r.status == "error"]
    ok_count = sum(1 for r in reports if r.status == "ok")
    log.info("Summary: sources=%d | ok=%d | failed=%d | new today=%d | elapsed=%.1fs",
             len(reports),
             ok_count,
             len(failed_sources),
             sum(len(r.new_posts) for r in reports),
             (datetime.now(timezone.utc) - started).total_seconds())
    log.info("=" * 72)

    # A delivered report already tells the user about partial failures, so a
    # single flaky source (e.g. bot-protected sites) keeps the Action green.
    # Fail hard only when nothing was fetched at all.
    if failed_sources and ok_count == 0:
        log.error("All sources failed to fetch: %s", ", ".join(failed_sources))
        return 1
    if failed_sources:
        log.warning("Partial fetch failures (shown in the Telegram report): %s",
                    ", ".join(failed_sources))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unhandled exception in main")
        sys.exit(1)
