#!/usr/bin/env python3
"""Crawl a site and save article-like pages as cleaned text files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

import scrapy
from scrapy.crawler import CrawlerProcess


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

PAGINATION_TEXT_RE = re.compile(
    r"^(next|prev|previous|older|newer|more|page|suivant|precedent|[0-9]{1,4})$",
    re.IGNORECASE,
)

PAGINATION_URL_RE = re.compile(
    r"(?:\?|&)(?:page|paged|p)=\d+|/page/\d+/?|/p/\d+/?",
    re.IGNORECASE,
)

PAGE_NUMBER_RE = re.compile(r"/page/(\d+)/?$|(?:\?|&)page=(\d+)", re.IGNORECASE)


SKIP_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bmp",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".ttf",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


def clean_fragment(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def make_filename(url: str, index: int) -> str:
    parsed = urlparse(url)
    base = parsed.path.strip("/") or "index"
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-") or "page"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{index:04d}-{base}-{digest}.txt"


def same_site(candidate_url: str, root_domain: str) -> bool:
    host = (urlparse(candidate_url).hostname or "").lower()
    return host == root_domain or host.endswith(f".{root_domain}")


def normalize_candidate_url(base_url: str, href: str, root_domain: str) -> str | None:
    absolute = urljoin(base_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)

    if parsed.scheme not in {"http", "https"}:
        return None
    if not same_site(absolute, root_domain):
        return None
    if Path(parsed.path.lower()).suffix in SKIP_EXTENSIONS:
        return None

    return absolute


def extract_page_number(url: str) -> int | None:
    match = PAGE_NUMBER_RE.search(url)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class ProgressReporter:
    def __init__(self, jsonl_path: str | None):
        self.path = Path(jsonl_path).expanduser() if jsonl_path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, message: str, **data) -> None:
        if not self.path:
            return

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "message": message,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, default=str))
            handle.write("\n")


class ArticleSpider(scrapy.Spider):
    name = "article_spider"
    handle_httpstatus_list = [401, 403, 429]

    def __init__(
        self,
        start_url: str,
        output_dir: str,
        max_pages: int = 60,
        max_depth: int = 2,
        min_words: int = 180,
        container_id: str | None = None,
        article_selector: str | None = None,
        pagination_selector: str | None = None,
        max_pagination_pages: int | None = None,
        pagination_follow_next_only: bool = False,
        article_url_regex: str | None = None,
        progress_jsonl: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        parsed = urlparse(start_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("URL must start with http:// or https://")
        if not parsed.hostname:
            raise ValueError("URL must include a host.")

        self.start_urls = [start_url]
        self.root_domain = parsed.hostname.lower()
        self.allowed_domains = [self.root_domain]
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.max_pages = max_pages
        self.max_depth = max_depth
        self.min_words = min_words
        self.container_id = container_id.strip() if container_id else None
        self.article_selector = article_selector.strip() if article_selector else None
        self.pagination_selector = pagination_selector.strip() if pagination_selector else None
        if max_pagination_pages is not None and max_pagination_pages < 1:
            raise ValueError("--max-pagination-pages must be >= 1")
        self.max_pagination_pages = max_pagination_pages if max_pagination_pages else None
        self.pagination_follow_next_only = pagination_follow_next_only
        self.article_url_regex = article_url_regex.strip() if article_url_regex else None
        if self.article_url_regex:
            try:
                self._article_url_pattern = re.compile(self.article_url_regex, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid --article-url-regex: {exc}") from exc
        else:
            self._article_url_pattern = None
        self.progress = ProgressReporter(progress_jsonl)

        self._visited = 0
        self._saved = 0
        self._blocked = 0
        self._queued_articles = 0
        self._queued_pages = 0
        self._seen_listing_urls: set[str] = set()
        self._queued_listing_urls: set[str] = set()

    def start_requests(self):
        self.progress.emit(
            "started",
            "Crawler started",
            start_url=self.start_urls[0],
            max_pages=self.max_pages,
            max_depth=self.max_depth,
            max_pagination_pages=self.max_pagination_pages,
            min_words=self.min_words,
            mode="scoped" if self.container_id else "generic",
            container_id=self.container_id,
            pagination_follow_next_only=self.pagination_follow_next_only,
            article_url_regex=self.article_url_regex,
        )
        for url in self.start_urls:
            if self.container_id:
                self._queued_listing_urls.add(url)
                yield scrapy.Request(url, callback=self.parse, meta={"listing_depth": 0})
            else:
                yield scrapy.Request(url, callback=self.parse)

    def parse(self, response: scrapy.http.Response):
        if self._is_blocked(response):
            return

        if not self._reserve_visit(response.url, page_type="listing" if self.container_id else "page"):
            return

        if self.container_id:
            yield from self._parse_scoped_listing(response)
            return

        yield from self._parse_generic(response)

    def parse_article(self, response: scrapy.http.Response):
        if self._is_blocked(response):
            return
        if not self._reserve_visit(response.url, page_type="article"):
            return

        self._save_if_article_like(response)

    def _is_blocked(self, response: scrapy.http.Response) -> bool:
        if response.status in {401, 403, 429}:
            self._blocked += 1
            self.logger.warning("Blocked on %s (HTTP %s)", response.url, response.status)
            self.progress.emit(
                "blocked",
                f"Blocked on {response.url} (HTTP {response.status})",
                url=response.url,
                status=response.status,
                blocked=self._blocked,
                visited=self._visited,
                saved=self._saved,
            )
            return True
        return False

    def _reserve_visit(self, url: str, page_type: str) -> bool:
        if self._visited >= self.max_pages:
            self.progress.emit(
                "limit_reached",
                "Reached maximum page limit. Stopping crawl.",
                visited=self._visited,
                max_pages=self.max_pages,
            )
            self.crawler.engine.close_spider(self, reason="max_pages_reached")
            return False
        self._visited += 1
        self.progress.emit(
            "page_visited",
            f"Visited {url}",
            url=url,
            page_type=page_type,
            visited=self._visited,
            saved=self._saved,
            blocked=self._blocked,
            max_pages=self.max_pages,
        )
        return True

    def _parse_generic(self, response: scrapy.http.Response):
        self._save_if_article_like(response)

        depth = int(response.meta.get("depth", 0))
        if depth >= self.max_depth:
            self.progress.emit(
                "depth_limit",
                f"Depth limit reached at {response.url}",
                url=response.url,
                depth=depth,
                max_depth=self.max_depth,
            )
            return

        next_links = list(self._extract_links(response))
        self.progress.emit(
            "links_discovered",
            f"Discovered {len(next_links)} internal links",
            url=response.url,
            depth=depth,
            discovered=len(next_links),
        )
        for next_url in next_links:
            yield scrapy.Request(next_url, callback=self.parse)

    def _parse_scoped_listing(self, response: scrapy.http.Response):
        if not self._is_html(response):
            return

        listing_depth = int(response.meta.get("listing_depth", 0))
        self._seen_listing_urls.add(response.url)
        listing_pages_seen = len(self._seen_listing_urls)

        article_links, pagination_links = self._extract_scoped_links(response)
        article_links = self._filter_article_links(article_links)
        pagination_links = self._filter_pagination_links(response.url, pagination_links)

        self._queued_articles += len(article_links)
        self._queued_pages += len(pagination_links)
        self.progress.emit(
            "scoped_links",
            (
                f"Found {len(article_links)} article links and "
                f"{len(pagination_links)} pagination links in container"
            ),
            url=response.url,
            listing_depth=listing_depth,
            listing_pages_seen=listing_pages_seen,
            found_article_links=len(article_links),
            found_pagination_links=len(pagination_links),
            queued_article_links=self._queued_articles,
            queued_pagination_links=self._queued_pages,
        )

        for article_url in article_links:
            yield scrapy.Request(article_url, callback=self.parse_article)

        if listing_depth >= self.max_depth:
            self.progress.emit(
                "pagination_depth_limit",
                f"Pagination depth limit reached at {response.url}",
                url=response.url,
                listing_depth=listing_depth,
                max_depth=self.max_depth,
            )
            return

        if self.max_pagination_pages and listing_pages_seen >= self.max_pagination_pages:
            self.progress.emit(
                "pagination_page_limit",
                "Reached pagination page limit.",
                url=response.url,
                listing_pages_seen=listing_pages_seen,
                max_pagination_pages=self.max_pagination_pages,
            )
            return

        for next_url in pagination_links:
            if next_url == response.url:
                continue
            if next_url in self._queued_listing_urls:
                continue
            if self.max_pagination_pages and len(self._queued_listing_urls) >= self.max_pagination_pages:
                break
            self._queued_listing_urls.add(next_url)
            yield scrapy.Request(
                next_url,
                callback=self.parse,
                meta={"listing_depth": listing_depth + 1},
            )

    def _filter_article_links(self, article_links: list[str]) -> list[str]:
        if not self._article_url_pattern:
            return article_links
        return [url for url in article_links if self._article_url_pattern.search(url)]

    def _filter_pagination_links(self, current_url: str, pagination_links: list[str]) -> list[str]:
        if not pagination_links:
            return []
        if not self.pagination_follow_next_only:
            return pagination_links

        current_page_number = extract_page_number(current_url) or 1
        numeric_candidates: list[tuple[int, str]] = []
        fallback_candidates: list[str] = []

        for url in pagination_links:
            page_number = extract_page_number(url)
            if page_number is None:
                fallback_candidates.append(url)
                continue
            if page_number > current_page_number:
                numeric_candidates.append((page_number, url))

        if numeric_candidates:
            numeric_candidates.sort(key=lambda item: item[0])
            return [numeric_candidates[0][1]]

        if fallback_candidates:
            return [fallback_candidates[0]]

        return []

    def _save_if_article_like(self, response: scrapy.http.Response) -> None:
        if not self._is_html(response):
            return

        article = self._extract_article_like_content(response)
        if not article:
            return

        self._saved += 1
        filename = make_filename(response.url, self._saved)
        target = self.output_dir / filename
        target.write_text(
            f"Title: {article['title']}\n"
            f"URL: {response.url}\n\n"
            f"{article['text']}\n",
            encoding="utf-8",
        )
        self.logger.info("Saved %s", target)
        self.progress.emit(
            "article_saved",
            f"Saved article from {response.url}",
            url=response.url,
            file=str(target),
            saved=self._saved,
            visited=self._visited,
            blocked=self._blocked,
        )

    def _is_html(self, response: scrapy.http.Response) -> bool:
        content_type = response.headers.get("Content-Type", b"").decode("latin-1").lower()
        return "text/html" in content_type or "application/xhtml+xml" in content_type

    def _extract_links(self, response: scrapy.http.Response) -> Iterable[str]:
        for href in response.css("a::attr(href)").getall():
            absolute = normalize_candidate_url(response.url, href, self.root_domain)
            if absolute:
                yield absolute

    def _extract_scoped_links(self, response: scrapy.http.Response) -> tuple[list[str], list[str]]:
        if not self.container_id:
            return [], []

        scope = response.css(f"#{self.container_id}")
        if not scope:
            self.logger.warning(
                "Container id '%s' not found on %s",
                self.container_id,
                response.url,
            )
            self.progress.emit(
                "container_missing",
                f"Container '{self.container_id}' not found on {response.url}",
                url=response.url,
                container_id=self.container_id,
            )
            return [], []

        article_links: list[str] = []
        pagination_links: list[str] = []
        scoped_root = scope[0]

        if self.pagination_selector:
            for href in self._extract_hrefs_by_selector(scoped_root, self.pagination_selector):
                url = normalize_candidate_url(response.url, href, self.root_domain)
                if url:
                    pagination_links.append(url)
        else:
            for anchor in scoped_root.css("a"):
                href = anchor.attrib.get("href", "")
                url = normalize_candidate_url(response.url, href, self.root_domain)
                if not url:
                    continue
                if self._looks_like_pagination_anchor(anchor, url):
                    pagination_links.append(url)

        if self.article_selector:
            for href in self._extract_hrefs_by_selector(scoped_root, self.article_selector):
                url = normalize_candidate_url(response.url, href, self.root_domain)
                if url and url not in pagination_links:
                    article_links.append(url)
        else:
            for href in scoped_root.css("a::attr(href)").getall():
                url = normalize_candidate_url(response.url, href, self.root_domain)
                if url and url not in pagination_links:
                    article_links.append(url)

        return self._dedupe_urls(article_links), self._dedupe_urls(pagination_links)

    def _extract_hrefs_by_selector(self, root, selector: str) -> Iterable[str]:
        if "::attr(" in selector:
            return root.css(selector).getall()
        return root.css(f"{selector}::attr(href)").getall()

    def _dedupe_urls(self, urls: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            output.append(url)
        return output

    def _looks_like_pagination_anchor(self, anchor, url: str) -> bool:
        text = clean_fragment(" ".join(anchor.css("::text").getall())).lower()
        rel = anchor.attrib.get("rel", "")
        rel_value = " ".join(rel) if isinstance(rel, list) else str(rel)
        rel_value = rel_value.lower()
        class_name = anchor.attrib.get("class", "").lower()
        anchor_id = anchor.attrib.get("id", "").lower()

        if any(token in rel_value for token in {"next", "prev", "previous"}):
            return True
        if PAGINATION_TEXT_RE.match(text):
            return True
        if "pagination" in class_name or "pager" in class_name or "pagination" in anchor_id:
            return True
        if "page=" in url.lower() and re.search(r"(?:\?|&)page=\d+", url.lower()):
            return True
        if PAGINATION_URL_RE.search(url):
            return True

        return False

    def _extract_article_like_content(self, response: scrapy.http.Response) -> dict[str, str] | None:
        title = clean_fragment(response.css("title::text").get("") or "")
        if not title:
            title = clean_fragment(" ".join(response.css("h1::text").getall()))
        if not title:
            title = "Untitled"

        paragraph_sources = [
            "//article//p//text()",
            "//main//p//text()",
            (
                "//section[contains(@class, 'article') or contains(@class, 'content')"
                " or contains(@id, 'article') or contains(@id, 'content')]//p//text()"
            ),
            "//p//text()",
        ]

        paragraphs: list[str] = []
        for source in paragraph_sources:
            chunks = [clean_fragment(t) for t in response.xpath(source).getall()]
            chunks = [t for t in chunks if word_count(t) >= 5]
            if chunks:
                paragraphs = chunks
                break

        if not paragraphs:
            return None

        body_text = "\n\n".join(paragraphs)
        content_words = word_count(body_text)
        link_words = word_count(" ".join(response.css("a ::text").getall()))
        page_words = word_count(" ".join(response.css("body ::text").getall()))
        link_density = link_words / page_words if page_words else 1.0

        has_article_tag = bool(response.xpath("//article"))
        long_enough = content_words >= self.min_words
        paragraphs_enough = len(paragraphs) >= 3
        navigation_heavy = link_density > 0.35

        if not ((long_enough and paragraphs_enough) or (has_article_tag and content_words >= 90)):
            return None
        if navigation_heavy:
            return None

        return {"title": title, "text": body_text}

    def closed(self, reason: str):
        self.progress.emit(
            "finished",
            "Crawler finished",
            reason=reason,
            visited=self._visited,
            saved=self._saved,
            blocked=self._blocked,
            queued_article_links=self._queued_articles,
            queued_pagination_links=self._queued_pages,
            output_dir=str(self.output_dir),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a website and save article-like pages as cleaned .txt files."
    )
    parser.add_argument("url", help="Starting URL (example: https://example.com)")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="output",
        help="Where text files should be written (default: ./output)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=60,
        help="Hard cap on visited pages to avoid runaway crawling.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help=(
            "How many levels deep to follow links. "
            "In --container-id mode this limits pagination depth."
        ),
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=180,
        help="Minimum body word count for a page to be treated as article-like.",
    )
    parser.add_argument(
        "--container-id",
        default=None,
        help=(
            "Only crawl links found inside this HTML id. "
            "Useful for listing pages where articles and pagination are in one block."
        ),
    )
    parser.add_argument(
        "--article-selector",
        default=None,
        help=(
            "CSS selector (inside --container-id) for article links. "
            "Example: 'h2 a'. If omitted, all non-pagination links in the container are used."
        ),
    )
    parser.add_argument(
        "--pagination-selector",
        default=None,
        help=(
            "CSS selector (inside --container-id) for pagination links. "
            "Example: '.pagination a'. If omitted, pagination links are auto-detected."
        ),
    )
    parser.add_argument(
        "--max-pagination-pages",
        type=int,
        default=None,
        help=(
            "Maximum number of listing pages to crawl in --container-id mode "
            "(includes the first page). Example: 5."
        ),
    )
    parser.add_argument(
        "--pagination-follow-next-only",
        action="store_true",
        help=(
            "In --container-id mode, follow only the next pagination page "
            "(prevents jumping to deep archive page numbers)."
        ),
    )
    parser.add_argument(
        "--article-url-regex",
        default=None,
        help=(
            "Only keep article links matching this regex. "
            "Useful to keep a single section/topic."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=BROWSER_USER_AGENT,
        help="User-Agent header to use for requests.",
    )
    parser.add_argument(
        "--obey-robots",
        action="store_true",
        help="Respect robots.txt rules (off by default for sites that block robots.txt with 403).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Concurrent requests (lower value can reduce blocking).",
    )
    parser.add_argument(
        "--download-delay",
        type=float,
        default=0.35,
        help="Base delay between requests in seconds.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Scrapy log verbosity.",
    )
    parser.add_argument(
        "--progress-jsonl",
        default=None,
        help="Write structured progress events as JSONL to this file.",
    )
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> dict[str, object]:
    return {
        "LOG_LEVEL": args.log_level,
        "ROBOTSTXT_OBEY": args.obey_robots,
        "AUTOTHROTTLE_ENABLED": True,
        "DOWNLOAD_TIMEOUT": 25,
        "CONCURRENT_REQUESTS": max(1, args.concurrency),
        "DOWNLOAD_DELAY": max(0.0, args.download_delay),
        "USER_AGENT": args.user_agent,
        "DEFAULT_REQUEST_HEADERS": DEFAULT_HEADERS,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [401, 403, 408, 429, 500, 502, 503, 504, 522, 524],
        "COOKIES_ENABLED": True,
        "TELNETCONSOLE_ENABLED": False,
    }


def main() -> None:
    args = parse_args()

    process = CrawlerProcess(settings=build_settings(args))
    process.crawl(
        ArticleSpider,
        start_url=args.url,
        output_dir=args.output_dir,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        min_words=args.min_words,
        container_id=args.container_id,
        article_selector=args.article_selector,
        pagination_selector=args.pagination_selector,
        max_pagination_pages=args.max_pagination_pages,
        pagination_follow_next_only=args.pagination_follow_next_only,
        article_url_regex=args.article_url_regex,
        progress_jsonl=args.progress_jsonl,
    )
    process.start()


if __name__ == "__main__":
    main()
