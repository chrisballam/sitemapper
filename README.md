# sitemapper

A small, dependency-free **validated** `sitemap.xml` generator.

Point it at a website's homepage. It crawls the site while respecting
`robots.txt`, then writes a standards-compliant `sitemap.xml` that contains
**only canonical, indexable, HTTP-200 URLs** — with honest `<lastmod>` values and
no `<priority>` or `<changefreq>` (search engines ignore those).

## Why "validated"?

Most sitemap generators list every URL they can reach. This one fetches each page
and **excludes**:

- `noindex` pages (both `<meta name="robots">` and the `X-Robots-Tag` header)
- non-canonical URLs (it honors `<link rel="canonical">` and collapses duplicates)
- redirects (3xx) and errors (4xx/5xx) — only final `200`s are listed
- anything `robots.txt` disallows
- links marked `rel="nofollow"` (not followed)

It also produces **honest `<lastmod>`**:

- Uses the HTTP `Last-Modified` header when the server sends one.
- Otherwise, with a `--state` cache, it hashes each page's content and records the
  date the content **actually changed** across runs. On the very first run every
  page is stamped with today's date; real change-dates accrue as you re-run it.
- Never fabricates a single generation timestamp for every URL (a common mistake
  that makes Google discount `lastmod` site-wide).

## Requirements

- **Python 3.8+**. That's it — the default crawl uses only the standard library.
- Optional: [Playwright](https://playwright.dev/python/) **only** if you use
  `--render-js` for JavaScript-rendered / single-page-app sites.

## Install

```bash
git clone https://github.com/chrisballam/sitemapper.git
cd sitemapper
# no build step, no pip install needed
python sitemapper.py --help
```

(Optionally copy `sitemapper.py` anywhere on your `PATH` and `chmod +x` it.)

## Usage

```bash
# Simplest: crawl a site and write ./sitemap.xml
python sitemapper.py https://example.com

# Choose an output path, keep a state cache for honest lastmod, show progress
python sitemapper.py https://example.com \
  -o /var/www/example.com/sitemap.xml \
  --state /var/lib/sitemapper/example.state.json \
  -v
```

Then place the resulting `sitemap.xml` at the **root** of your site
(`https://example.com/sitemap.xml`) and reference it in `robots.txt`:

```
Sitemap: https://example.com/sitemap.xml
```

### Common options

| Flag | Purpose |
|------|---------|
| `-o, --output PATH` | Output file (default `sitemap.xml`, or `sitemap_index.xml` when split) |
| `--state PATH` | JSON cache enabling honest, per-URL `lastmod` across runs (recommended for cron) |
| `--include-subdomains` | Also crawl subdomains of the registrable domain |
| `--no-docs` | HTML pages only; exclude PDFs (PDFs are included by default) |
| `--ignore-query` | Treat URLs differing only by query string as one |
| `--gzip` | Write gzipped `sitemap.xml.gz` |
| `--delay SECONDS` | Politeness delay between requests (defaults to robots `Crawl-delay`) |
| `--max-pages N` | Safety cap on pages fetched (`0` = unlimited; default 50000) |
| `--render-js` | Render pages with Playwright to find JS-injected links (see below) |
| `--config FILE` | Load defaults from an INI file (see `examples/config.example.ini`) |
| `-v`, `-vv` | Info / debug logging |

Full list: `python sitemapper.py --help`.

### Large sites

If the crawl exceeds **50,000 URLs** or **50 MB**, sitemapper automatically writes
multiple `sitemap-1.xml`, `sitemap-2.xml`, … shards plus a `sitemap_index.xml`.
Tell it where the shards will be hosted so the index links are correct:

```bash
python sitemapper.py https://example.com --public-path / -o sitemap_index.xml
```

### JavaScript-rendered sites (optional)

The default crawler reads server-rendered HTML. If a site builds its links with
client-side JavaScript, enable rendering:

```bash
pip install playwright
playwright install chromium
python sitemapper.py https://spa.example.com --render-js
```

If Playwright isn't installed, `--render-js` logs a warning and falls back to
static HTML — it never hard-fails.

## Automating with cron

Generate a fresh sitemap on a schedule. Edit your crontab with `crontab -e` and
add one line (see `examples/crontab.txt` for more):

```cron
# Daily at 03:15 — regenerate and keep an honest lastmod cache
15 3 * * * /usr/bin/python3 /opt/sitemapper/sitemapper.py https://example.com \
  -o /var/www/example.com/sitemap.xml \
  --state /var/lib/sitemapper/example.state.json >> /var/log/sitemapper.log 2>&1
```

Schedule cheatsheet (the five fields are `minute hour day-of-month month day-of-week`):

| Frequency | Schedule line prefix |
|-----------|----------------------|
| Every day at 03:15 | `15 3 * * *` |
| Every Monday at 03:15 | `15 3 * * 1` |
| 1st of each month at 03:15 | `15 3 1 * *` |

**Tips**
- Use an **absolute** Python path and script path (cron has a minimal `PATH`).
- Keep the same `--state` file between runs so `lastmod` reflects real changes.
- Write the sitemap somewhere your web server already serves, or add a deploy/copy
  step after generation.
- Redirect output to a log (`>> …log 2>&1`) so failures are visible.

Prefer systemd? See `examples/` for a timer + service unit equivalent.

## Exit codes

- `0` — a sitemap with at least one URL was written.
- `1` — no indexable URLs were found (check the start URL and `robots.txt`), or a
  usage error.

## Testing

```bash
python -m unittest discover -s tests -v
```

The tests run a local HTTP server with fixtures and assert the inclusion/exclusion
rules (noindex, canonical, redirects, robots, PDFs, scope).

## License

MIT — see [LICENSE](LICENSE).
