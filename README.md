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
- **`--omit-lastmod`** drops `<lastmod>` entirely. Use it when pages are dynamic,
  send no `Last-Modified` header, **and** carry per-request-varying content (a CSRF
  token, a CSP nonce, a timestamp) — there the content hash would flip every run
  and stamp a misleading "changed today". A sitemap with no `lastmod` is fully
  valid; search engines just fall back to their own crawl signals, which is better
  than an unreliable date.

## Requirements

- **Python 3.8+**. That's it — the default crawl uses only the standard library.
- Optional: [Playwright](https://playwright.dev/python/) **only** if you use
  `--render-js` for JavaScript-rendered / single-page-app sites.

## Install

**Option A — install as a command (recommended for servers):**

```bash
pipx install git+https://github.com/chrisballam/sitemapper.git
sitemapper --help          # now on your PATH
```

(`pip install --user git+https://github.com/chrisballam/sitemapper.git` works too.)

**Option B — no install, just the single file:**

```bash
git clone https://github.com/chrisballam/sitemapper.git
cd sitemapper
python sitemapper.py --help   # no build step, no dependencies
```

Either way the core needs only the Python 3.8+ standard library. Examples below
show `python sitemapper.py …`; if you installed via Option A, use `sitemapper …`.

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

**Writing straight to your webroot is supported and safe.** Point `--output` at
the served path (e.g. `-o /var/www/example.com/sitemap.xml`); the file is written
**atomically** (to a temp file, then `os.replace`), so a crawler requesting the
sitemap mid-run never sees a half-written file. Just make sure the user running
the command can write to that directory.

### Common options

| Flag | Purpose |
|------|---------|
| `-o, --output PATH` | Output file (default `sitemap.xml`, or `sitemap_index.xml` when split) |
| `--state PATH` | JSON cache enabling honest, per-URL `lastmod` across runs (recommended for cron) |
| `--omit-lastmod` | Drop `<lastmod>` entirely (best for dynamic sites with per-request-varying HTML) |
| `--include-subdomains` | Also crawl subdomains of the registrable domain |
| `--no-docs` | HTML pages only; exclude PDFs (PDFs are included by default) |
| `--report-dir DIR` | Also write `broken-links.csv`, `internal-links.csv`, `external-links.csv` |
| `--report-crawlable-only` | In the link reports, keep only HTML page targets (drop robots-disallowed + non-HTML files) |
| `--ignore-query` | Drop the **entire** query string (opt-in; see [Query strings](#query-strings)) |
| `--strip-params LIST` | Query params to drop as tracking junk (default list; `utm_*` always dropped) |
| `--no-strip-params` | Keep every query param, including tracking ones |
| `--gzip` | Write gzipped `sitemap.xml.gz` |
| `--delay SECONDS` | Politeness delay between requests (defaults to robots `Crawl-delay`) |
| `--max-pages N` | Safety cap on pages fetched (`0` = unlimited; default 50000) |
| `--render-js` | Render pages with Playwright to find JS-injected links (see below) |
| `--indexnow KEY` | After writing, submit changed URLs to IndexNow (Bing/Yandex/…) |
| `--print-cron daily\|weekly\|monthly` | Print a ready crontab line for this invocation and exit |
| `--config FILE` | Load defaults from an INI file (see `examples/config.example.ini`) |
| `-v`, `-vv` | Info / debug logging |

Full list: `python sitemapper.py --help`.

### Link reports (broken / internal / external)

Pass `--report-dir DIR` to also emit three CSVs into `DIR`:

```bash
python sitemapper.py https://example.com --report-dir ./reports
```

> ⚠️ **Keep `--report-dir` OUTSIDE your public webroot.** These CSVs are for you,
> not your visitors — they list your internal link graph and any broken URLs, and
> you almost certainly don't want them fetchable on your live site. `--report-dir`
> is **independent** of `--output`: the sitemap goes where `-o` points (e.g. your
> webroot) while the reports go wherever `--report-dir` points. So you can, and
> should, write the sitemap to the webroot and the CSVs to a private directory:
>
> ```bash
> python sitemapper.py https://example.com \
>   -o /var/www/example.com/sitemap.xml \      # public: served at your root
>   --report-dir /var/log/sitemapper/reports    # private: outside the webroot
> ```
>
> Good spots for `--report-dir`: `/var/log/sitemapper/…`, `/var/lib/sitemapper/…`,
> or a folder in your home directory — anywhere your web server does **not** serve.
> If you omit `--report-dir`, no CSVs are written at all.

- **`broken-links.csv`** — every URL that failed to load (HTTP 4xx/5xx or a
  connection error), **with the page(s) that link to it**. Columns:
  `broken_url, status, referring_page` (one row per broken-URL/referrer pair).
  Broken links hurt user experience and waste crawl budget — open this file, go
  to each `referring_page`, and fix or remove the link.
- **`internal-links.csv`** — every discovered link that points to your own
  registrable domain (nav, cross-links, footers, pagination). Columns:
  `source_page, target_url`.
- **`external-links.csv`** — every link that points to a third-party domain
  (social profiles, references, etc.). Columns: `source_page, target_url`.

Add **`--report-crawlable-only`** to trim the internal/external reports down to
real HTML page targets — it drops:
- **robots-disallowed** targets (e.g. a gallery's per-photo URLs your `robots.txt`
  blocks), and
- **non-HTML files** by extension (`.pdf`, images, `.js`, `.css`, archives, media…).

`broken-links.csv` is **never** filtered — a dead PDF or blocked URL is still worth
knowing about.

```bash
# Full edge list (everything linked)
python sitemapper.py https://example.com --report-dir ./reports

# Just the HTML page-to-page graph
python sitemapper.py https://example.com --report-dir ./reports --report-crawlable-only
```

Notes:
- Broken-link detection covers **internal** URLs the crawler actually fetched;
  it does not fetch external URLs, so it won't flag a dead third-party link.
- Without `--report-crawlable-only`, `internal-links.csv` is a *full* edge list
  (every link on every crawled page) and can be large on big sites — that's what
  makes it complete. Use the flag for an actionable page-graph.
- Collection only happens when `--report-dir` is set, so normal runs stay lean.

### Query strings

By default sitemapper **keeps** query strings, because on many sites `?` carries a
real page identity (`?id=42`, `?page=2`, faceted catalogs). Blindly discarding
queries would silently drop real pages. Two mechanisms keep the output clean
without that risk:

1. **`rel="canonical"` is honored.** A `?utm=…`-tagged page that declares a
   canonical URL collapses to it automatically — the correct, standards-based
   dedup.
2. **Tracking params are stripped by default** (`--strip-params`). Pure
   click/tracking junk that never identifies a page is removed during
   normalization: `gclid`, `fbclid`, `msclkid`, `mc_cid`, `_ga`, and **any
   `utm_*`**, among others. Real params (`id`, `page`, `product`, …) are kept.
   - Customize: `--strip-params "utm_source,ref,sessionid"` (replaces the list;
     `utm_*` is still always dropped).
   - Disable entirely: `--no-strip-params`.

Only reach for **`--ignore-query`** — which drops the *whole* query string — when
you know a site's query params are all noise (e.g. a gallery that appends sort
params to every link). It's opt-in precisely because it can merge genuinely
distinct pages. If a crawl balloons because a section appends combinatorial query
params to links, prefer adding the offending names to `--strip-params`, or cap it
with `--max-pages`, before resorting to `--ignore-query`.

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

## Notifying search engines

Once a fresh `sitemap.xml` is live at your root, engines pick it up on their own —
you rarely need to "ping" anyone:

- **Reference it in `robots.txt`** — `Sitemap: https://example.com/sitemap.xml`.
  This is the durable, one-time step every crawler honors.
- **Submit it once** in Google Search Console and Bing Webmaster Tools. After
  that they recrawl the same URL automatically.
- **No Google/Bing ping option, on purpose.** The old `google.com/ping?sitemap=`
  and `bing.com/ping?sitemap=` endpoints were **retired** (Google in 2023, Bing in
  2023). A tool that "pinged" them would be doing nothing. This isn't an omission —
  those features are dead.

**For fast re-indexing of changed pages, use IndexNow** (Bing, Yandex, Seznam,
Naver — Google does not participate). sitemapper can submit **only the URLs whose
content changed this run** (it knows them from the `--state` cache):

```bash
# One-time: host your key file at the site root, e.g.
#   https://example.com/ab12cd….txt  containing the single line  ab12cd…
python sitemapper.py https://example.com \
  -o /var/www/example.com/sitemap.xml \
  --state /var/lib/sitemapper/example.state.json \
  --indexnow ab12cd…
```

On each run it POSTs the changed URLs to IndexNow and logs the result. With
`--state`, an unchanged site submits nothing; without it, every indexable URL is
treated as new. Override the key location or endpoint with
`--indexnow-key-location` / `--indexnow-endpoint`.

## Automating with cron

Let sitemapper write the crontab line for you — pick a frequency and paste the
output into `crontab -e`:

```bash
python sitemapper.py https://example.com \
  -o /var/www/example.com/sitemap.xml \
  --state /var/lib/sitemapper/example.state.json \
  --indexnow ab12cd… \
  --print-cron daily
```

That prints, e.g.:

```cron
15 3 * * * /usr/bin/python3 /opt/sitemapper/sitemapper.py https://example.com -o /var/www/example.com/sitemap.xml --state /var/lib/sitemapper/example.state.json --indexnow ab12cd… >> /var/log/sitemapper.log 2>&1
```

`--print-cron` accepts `daily` (03:15), `weekly` (Mondays 03:15), or `monthly`
(1st, 03:15); it only prints — it never edits your crontab. Full hand-written
examples are in `examples/crontab.txt`, and a systemd timer + service unit in
`examples/`.

**Tips**
- Use an **absolute** Python path and script path (cron has a minimal `PATH`);
  `--print-cron` fills these in for you. If you installed via `pipx`, replace the
  python + script path with just `sitemapper`.
- Keep the same `--state` file between runs so `lastmod` and `--indexnow` reflect
  real changes.
- The sitemap is written atomically, so pointing `-o` at your live webroot is safe.
- Redirect output to a log (`>> …log 2>&1`) so failures are visible.

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

## Crawl responsibly

Run sitemapper against sites you own or have permission to crawl. It respects
`robots.txt` by default, identifies itself with a descriptive User-Agent, honors
`Crawl-delay`, and offers `--delay` / `--max-pages` to stay polite on large or
shared hosts. `--no-respect-robots` exists for your *own* site only.

## License

MIT — see [LICENSE](LICENSE).
