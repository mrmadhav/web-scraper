# Simple Scrapy Website Text Scraper

This project crawls a website from a starting URL, follows internal links with safety limits, and saves only article-like pages to `.txt` files.

## What it does

- Uses **Scrapy** for crawling.
- Restricts crawling to the same site/domain.
- Avoids runaway recursion using:
  - maximum crawl depth
  - maximum visited pages
- Skips obvious non-HTML/binary links.
- Tries to keep only pages with meaningful content by checking:
  - paragraph count
  - minimum word count
  - link-density (to avoid nav/listing pages)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Web UI (Recommended)

```bash
python ui.py
```

Then open: `http://127.0.0.1:8765`

The UI lets you:

- configure crawl options
- run the scraper without typing CLI commands
- watch step-by-step progress (visited pages, discovered links, saved articles, blocked pages)
- view live event logs until completion
- open a visual selector mode and click page elements to fill:
  - container id
  - article selector
  - pagination selector

Selector flow:

1. Enter the target URL.
2. Click **Open Visual Selector**.
3. Click elements in the preview page.
4. Use the side buttons to apply that element as container/article/pagination.
5. Run the crawl.

## CLI Usage

```bash
python scraper.py https://example.com -o output --max-pages 80 --max-depth 2 --min-words 180
```

### Important flags

- `--max-depth`: Link depth limit (in `--container-id` mode this limits pagination depth).
- `--max-pages`: Total visited page cap.
- `--min-words`: Minimum extracted words required to save a page as article-like.
- `--container-id`: Crawl links only inside this element id.
- `--article-selector`: CSS selector for article links inside `--container-id`.
- `--pagination-selector`: CSS selector for pagination links inside `--container-id`.
- `--max-pagination-pages`: In container mode, limit listing pages crawled (includes page 1).
- `--pagination-follow-next-only`: Follow only the next pagination page (prevents deep page jumps).
- `--article-url-regex`: Keep only article links matching this regex.
- `--obey-robots`: Respect robots.txt rules (disabled by default).
- `--concurrency`: Lower values reduce request burstiness and can reduce blocking.
- `--download-delay`: Delay between requests in seconds.
- `--progress-jsonl`: Emit structured progress events to a JSONL file.
- `--log-level`: Set Scrapy log verbosity.

## Scoped Container + Pagination Mode

If your listing page has one main block that contains article cards + pagination, use `--container-id`.

Example:

```bash
python scraper.py "https://example.com/blog" \
  -o output \
  --container-id "main-content" \
  --article-selector "h2 a" \
  --pagination-selector ".pagination a" \
  --max-depth 5 \
  --max-pages 120
```

Notes:

- `--article-selector` and `--pagination-selector` are relative to the container.
- If you skip `--article-selector`, scraper uses all non-pagination links in the container as article candidates.
- If you skip `--pagination-selector`, scraper auto-detects pagination-like links (next/prev/page=2/etc.).

### Example for ecommercemag.fr (first pages only)

```bash
python scraper.py "https://www.ecommercemag.fr/thematique/techno-ux-1226" \
  -o output \
  --container-id "listing" \
  --article-selector "article a[href]" \
  --pagination-selector ".pagination a[href]" \
  --article-url-regex "/Thematique/techno-ux-1226/" \
  --pagination-follow-next-only \
  --max-pagination-pages 5 \
  --max-pages 200
```

If `#listing` does not match the site markup in your browser, inspect the page and replace it with the real list wrapper id.

## If you hit HTTP 403

Some websites block non-browser traffic. This scraper now sends browser-like headers by default, but for stricter sites try:

```bash
python scraper.py "https://target-site.com" \
  -o output \
  --max-pages 40 \
  --max-depth 1 \
  --concurrency 1 \
  --download-delay 1.0
```

## Output format

Each saved file contains:

- page title
- source URL
- cleaned text body

Files are written to the output directory (`output/` by default).
