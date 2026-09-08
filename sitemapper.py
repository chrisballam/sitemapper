#!/usr/bin/env python3
"""
sitemapper — a validated sitemap.xml generator.

Start at a website's homepage, crawl same-host pages while respecting
robots.txt, and emit a standards-compliant sitemap.xml that contains only
canonical, indexable, HTTP-200 URLs. Produces honest <lastmod> values and never
writes <priority> or <changefreq> (search engines ignore them).

Core has ZERO third-party dependencies (Python 3.7+ standard library only).
Optional JavaScript rendering (--render-js) uses Playwright if it is installed;
it is never required for the default static-HTML crawl.

License: MIT. Project: https://github.com/chrisballam/sitemapper
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

__version__ = "1.0.0"

# Per-sitemap-file limits from the sitemaps.org protocol.
MAX_URLS_PER_FILE = 50000
MAX_BYTES_PER_FILE = 50 * 1024 * 1024  # 50 MiB uncompressed

# Non-HTML document types that may be listed in a sitemap when indexable.
DOC_CONTENT_TYPES = {
    "application/pdf",
}

DEFAULT_USER_AGENT = (
    "Sitemapper/%s (+https://github.com/chrisballam/sitemapper)" % __version__
)

log = logging.getLogger("sitemapper")


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def normalize_url(url: str, base: str | None = None, ignore_query: bool = False) -> str | None:
    """Resolve, strip fragment, lowercase host, drop default port. Returns a
    normalized absolute http(s) URL, or None if the scheme is not http(s)."""
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname or ""
    host = host.lower()
    # Drop the default port for the scheme.
    port = parts.port
    if port is not None and not (
        (parts.scheme == "http" and port == 80)
        or (parts.scheme == "https" and port == 443)
    ):
        netloc = "%s:%d" % (host, port)
    else:
        netloc = host
    if parts.username:  # preserve credentials if unusually present
        userinfo = parts.username + ((":" + parts.password) if parts.password else "")
        netloc = "%s@%s" % (userinfo, netloc)
    path = parts.path or "/"
    query = "" if ignore_query else parts.query
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def registrable_host(host: str) -> str:
    """A deliberately simple eTLD approximation: last two labels. Good enough to
    decide 'same site' for the common case; subdomains are handled by an
    explicit include_subdomains flag, not by this function."""
    labels = host.lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def same_scope(url: str, root_host: str, include_subdomains: bool) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if include_subdomains:
        return registrable_host(host) == registrable_host(root_host)
    return host == root_host.lower()


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
class PageParser(HTMLParser):
    """Extract links, rel=canonical, meta-robots directives and <base href>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, bool]] = []  # (href, is_nofollow)
        self.canonical: str | None = None
        self.base_href: str | None = None
        self.meta_noindex = False
        self.meta_nofollow = False

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and "href" in a:
            rel = a.get("rel", "").lower()
            self.links.append((a["href"], "nofollow" in rel.split()))
        elif tag == "base" and "href" in a and self.base_href is None:
            self.base_href = a["href"]
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.canonical = a["href"]
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name in ("robots", "googlebot") and a.get("content"):
                content = a["content"].lower()
                if "noindex" in content:
                    self.meta_noindex = True
                if "nofollow" in content:
                    self.meta_nofollow = True


def parse_x_robots_tag(header_value: str) -> tuple[bool, bool]:
    """Return (noindex, nofollow) parsed from an X-Robots-Tag header value.
    Handles optional 'ua:' prefixes and comma-separated directives."""
    noindex = nofollow = False
    for part in header_value.split(","):
        directive = part.split(":")[-1].strip().lower()
        if directive == "noindex" or directive == "none":
            noindex = True
        if directive == "nofollow" or directive == "none":
            nofollow = True
    return noindex, nofollow


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class FetchResult:
    __slots__ = ("url", "final_url", "status", "content_type", "body",
                 "last_modified", "x_robots", "redirected", "error")

    def __init__(self, url):
        self.url = url
        self.final_url = url
        self.status = 0
        self.content_type = ""
        self.body = b""
        self.last_modified = None  # datetime or None
        self.x_robots = ""
        self.redirected = False
        self.error = None


def fetch(url: str, user_agent: str, timeout: float, max_bytes: int) -> FetchResult:
    """Fetch a URL following redirects; record the final URL + headers."""
    r = FetchResult(url)
    req = Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            r.status = getattr(resp, "status", resp.getcode()) or resp.getcode()
            r.final_url = resp.geturl()
            r.redirected = normalize_url(r.final_url) != normalize_url(url)
            r.content_type = (resp.headers.get_content_type() or "").lower()
            lm = resp.headers.get("Last-Modified")
            if lm:
                try:
                    r.last_modified = parsedate_to_datetime(lm)
                except (TypeError, ValueError):
                    r.last_modified = None
            r.x_robots = resp.headers.get("X-Robots-Tag", "") or ""
            # Only read bodies we intend to parse (HTML) or hash cheaply.
            raw = resp.read(max_bytes + 1)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError):
                    pass
            r.body = raw[:max_bytes]
    except HTTPError as e:
        r.status = e.code
        r.error = "http %s" % e.code
    except (URLError, TimeoutError) as e:
        r.error = str(getattr(e, "reason", e))
    except Exception as e:  # never let one bad URL abort the crawl
        r.error = repr(e)
    return r


# --------------------------------------------------------------------------- #
# Optional JavaScript rendering (Playwright, imported lazily)
# --------------------------------------------------------------------------- #
def render_html_js(url: str, user_agent: str, timeout: float) -> str | None:
    """Return fully-rendered HTML using Playwright, or None if unavailable."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("--render-js requested but Playwright is not installed; "
                    "falling back to static HTML for %s", url)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.warning("JS render failed for %s: %s", url, e)
        return None


# --------------------------------------------------------------------------- #
# State cache for honest lastmod
# --------------------------------------------------------------------------- #
class StateCache:
    """Maps normalized URL -> {'hash': <sha256>, 'lastmod': 'YYYY-MM-DD'}.
    When a page's content hash is unchanged, its stored lastmod is preserved;
    when it changes (or is new), lastmod is set to today. This yields a real
    "content last changed" date even when the server sends no Last-Modified."""

    def __init__(self, path: str | None):
        self.path = path
        self.data: dict[str, dict] = {}
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                self.data = {}

    def lastmod_for(self, url: str, body: bytes, header_lm: datetime | None,
                    today: str) -> str | None:
        if header_lm is not None:
            return header_lm.astimezone(timezone.utc).date().isoformat()
        digest = hashlib.sha256(body).hexdigest()
        prev = self.data.get(url)
        if prev and prev.get("hash") == digest:
            return prev.get("lastmod")
        self.data[url] = {"hash": digest, "lastmod": today}
        return today

    def save(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=0, sort_keys=True)
        except OSError as e:
            log.warning("could not write state cache %s: %s", self.path, e)


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class Crawler:
    def __init__(self, root: str, args) -> None:
        self.args = args
        self.root = normalize_url(root, ignore_query=args.ignore_query)
        if not self.root:
            raise ValueError("start URL must be an http(s) URL")
        self.root_host = urlsplit(self.root).hostname or ""
        self.user_agent = args.user_agent
        self.robots = self._load_robots()
        self.state = StateCache(args.state)
        self.today = datetime.now(timezone.utc).date().isoformat()

        self.included: dict[str, str | None] = {}   # url -> lastmod (or None)
        self.seen: set[str] = set()                  # enqueued/visited
        self.canonical_map: dict[str, str] = {}      # variant -> canonical
        self.skips: dict[str, int] = {}              # reason -> count

    # ---- robots -----------------------------------------------------------
    def _load_robots(self):
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(self.root, "/robots.txt")
        res = fetch(robots_url, self.user_agent, self.args.timeout, 1_000_000)
        if res.status == 200 and res.body:
            rp.parse(res.body.decode("utf-8", "replace").splitlines())
            log.info("loaded robots.txt (%d bytes)", len(res.body))
        else:
            # No robots.txt => everything allowed (protocol default).
            rp.parse([])
            log.info("no robots.txt found (status %s); allowing all", res.status)
        return rp

    def _allowed(self, url: str) -> bool:
        if not self.args.respect_robots:
            return True
        try:
            return self.robots.can_fetch(self.args.robots_agent, url)
        except Exception:
            return True

    def _crawl_delay(self) -> float:
        if self.args.delay is not None:
            return self.args.delay
        try:
            d = self.robots.crawl_delay(self.args.robots_agent)
            return float(d) if d else 0.0
        except Exception:
            return 0.0

    def _skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    # ---- main loop --------------------------------------------------------
    def run(self) -> None:
        delay = self._crawl_delay()
        queue: deque[tuple[str, int]] = deque([(self.root, 0)])
        self.seen.add(self.root)
        pages = 0

        while queue:
            url, depth = queue.popleft()
            if self.args.max_pages and pages >= self.args.max_pages:
                log.info("reached --max-pages=%d; stopping crawl", self.args.max_pages)
                break
            if not self._allowed(url):
                self._skip("robots-disallowed")
                continue

            res = fetch(url, self.user_agent, self.args.timeout, self.args.max_bytes)
            pages += 1
            if delay:
                time.sleep(delay)

            if res.error or res.status != 200:
                self._skip("non-200")
                log.debug("skip %s (%s)", url, res.error or res.status)
                continue

            # A redirect means this URL is not itself canonical content.
            final = normalize_url(res.final_url, ignore_query=self.args.ignore_query)
            if res.redirected:
                self._skip("redirect")
                if final and final not in self.seen and same_scope(
                    final, self.root_host, self.args.include_subdomains
                ):
                    self.seen.add(final)
                    queue.append((final, depth))
                continue

            ctype = res.content_type
            x_noindex, x_nofollow = parse_x_robots_tag(res.x_robots)

            if ctype == "text/html":
                self._handle_html(final or url, res, depth, queue, x_noindex, x_nofollow)
            elif ctype in DOC_CONTENT_TYPES and self.args.include_docs:
                if x_noindex:
                    self._skip("noindex")
                else:
                    self._include(final or url, res)
            else:
                self._skip("unsupported-type")

        self.state.save()

    def _handle_html(self, url, res, depth, queue, x_noindex, x_nofollow) -> None:
        html = res.body.decode("utf-8", "replace")
        if self.args.render_js:
            rendered = render_html_js(url, self.user_agent, self.args.timeout)
            if rendered:
                html = rendered
        p = PageParser()
        try:
            p.feed(html)
        except Exception:
            pass

        noindex = x_noindex or p.meta_noindex
        nofollow = x_nofollow or p.meta_nofollow

        # Determine the canonical URL for this page.
        canonical = url
        if p.canonical:
            c = normalize_url(p.canonical, base=url, ignore_query=self.args.ignore_query)
            if c:
                canonical = c
        if canonical != url:
            self.canonical_map[url] = canonical

        # Include the canonical (if indexable + in scope).
        if noindex:
            self._skip("noindex")
        elif not same_scope(canonical, self.root_host, self.args.include_subdomains):
            self._skip("out-of-scope-canonical")
        else:
            self._include(canonical, res)

        # Enqueue outbound links unless this page says nofollow.
        if nofollow or depth >= self.args.max_depth:
            return
        for href, link_nofollow in p.links:
            if link_nofollow:
                continue
            nxt = normalize_url(href, base=url, ignore_query=self.args.ignore_query)
            if not nxt or nxt in self.seen:
                continue
            if not same_scope(nxt, self.root_host, self.args.include_subdomains):
                continue
            self.seen.add(nxt)
            queue.append((nxt, depth + 1))

    def _include(self, url: str, res: FetchResult) -> None:
        if url in self.included:
            return
        lastmod = self.state.lastmod_for(url, res.body, res.last_modified, self.today)
        self.included[url] = lastmod
        log.debug("include %s (lastmod=%s)", url, lastmod)

    # ---- results ----------------------------------------------------------
    def urls(self) -> list[tuple[str, str | None]]:
        return sorted(self.included.items())


# --------------------------------------------------------------------------- #
# XML output
# --------------------------------------------------------------------------- #
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _url_entry(loc: str, lastmod: str | None) -> str:
    out = ["  <url>", "    <loc>%s</loc>" % escape(loc)]
    if lastmod:
        out.append("    <lastmod>%s</lastmod>" % lastmod)
    out.append("  </url>")
    return "\n".join(out)


def build_urlset(urls: list[tuple[str, str | None]]) -> str:
    body = "\n".join(_url_entry(loc, lm) for loc, lm in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="%s">\n%s\n</urlset>\n' % (SITEMAP_NS, body))


def build_index(sitemap_urls: list[str], lastmod: str) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="%s">' % SITEMAP_NS]
    for u in sitemap_urls:
        parts.append("  <sitemap>")
        parts.append("    <loc>%s</loc>" % escape(u))
        parts.append("    <lastmod>%s</lastmod>" % lastmod)
        parts.append("  </sitemap>")
    parts.append("</sitemapindex>")
    return "\n".join(parts) + "\n"


def chunk_urls(urls, max_urls=MAX_URLS_PER_FILE, max_bytes=MAX_BYTES_PER_FILE):
    """Yield lists of (loc, lastmod) each within the per-file url and byte caps."""
    chunk, size = [], 0
    overhead = 200  # xml declaration + <urlset> wrapper
    for loc, lm in urls:
        entry_bytes = len(_url_entry(loc, lm).encode("utf-8")) + 1
        if chunk and (len(chunk) >= max_urls or size + entry_bytes + overhead > max_bytes):
            yield chunk
            chunk, size = [], 0
        chunk.append((loc, lm))
        size += entry_bytes
    if chunk:
        yield chunk


def write_text(path: str, text: str, do_gzip: bool) -> None:
    data = text.encode("utf-8")
    if do_gzip:
        with gzip.open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)


def write_output(urls, args, root) -> list[str]:
    """Write one sitemap.xml, or a sitemapindex + shards if over the limits."""
    ext = ".xml.gz" if args.gzip else ".xml"
    today = datetime.now(timezone.utc).date().isoformat()
    chunks = list(chunk_urls(urls))
    written: list[str] = []

    if len(chunks) <= 1:
        path = args.output if args.output else ("sitemap" + ext)
        write_text(path, build_urlset(chunks[0] if chunks else []), args.gzip)
        written.append(path)
        return written

    # Multiple shards -> sitemap-1.xml .. sitemap-N.xml + sitemap_index.xml
    base = urlsplit(root)
    base_url = "%s://%s" % (base.scheme, base.netloc)
    prefix = args.public_path.rstrip("/") if args.public_path else ""
    shard_locs = []
    for i, chunk in enumerate(chunks, 1):
        name = "sitemap-%d%s" % (i, ext)
        write_text(name, build_urlset(chunk), args.gzip)
        written.append(name)
        shard_locs.append("%s%s/%s" % (base_url, prefix, name))
    index_name = args.output if args.output else "sitemap_index.xml"
    write_text(index_name, build_index(shard_locs, today), False)
    written.append(index_name)
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sitemapper",
        description="Generate a validated sitemap.xml by crawling a site's "
                    "homepage while respecting robots.txt.")
    p.add_argument("url", nargs="?", help="Homepage/root URL, e.g. https://example.com")
    p.add_argument("--output", "-o", help="Output file (default sitemap.xml, or "
                                          "sitemap_index.xml when split)")
    p.add_argument("--config", help="INI config file; [sitemapper] section keys "
                                    "mirror these flags")
    p.add_argument("--state", help="JSON state cache path for honest lastmod "
                                   "across runs (recommended for cron)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                   help="User-Agent sent when crawling")
    p.add_argument("--robots-agent", default="Sitemapper",
                   help="Token matched against robots.txt groups")
    p.add_argument("--max-pages", type=int, default=50000,
                   help="Safety cap on pages fetched (0 = unlimited)")
    p.add_argument("--max-depth", type=int, default=100,
                   help="Maximum link depth from the homepage")
    p.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout (s)")
    p.add_argument("--delay", type=float, default=None,
                   help="Seconds between requests (default: robots Crawl-delay, else 0)")
    p.add_argument("--max-bytes", type=int, default=5_000_000,
                   help="Max bytes read per page")
    p.add_argument("--include-subdomains", action="store_true",
                   help="Also crawl subdomains of the registrable domain")
    p.add_argument("--include-docs", dest="include_docs", action="store_true",
                   default=True, help="Include indexable PDFs (default on)")
    p.add_argument("--no-docs", dest="include_docs", action="store_false",
                   help="HTML pages only; exclude PDFs")
    p.add_argument("--ignore-query", action="store_true",
                   help="Treat URLs that differ only by query string as one")
    p.add_argument("--render-js", action="store_true",
                   help="Render pages with Playwright to find JS-injected links "
                        "(optional; requires `pip install playwright`)")
    p.add_argument("--gzip", action="store_true", help="Write gzipped sitemap(s)")
    p.add_argument("--public-path", default="",
                   help="URL path prefix where shards will be hosted, for the "
                        "sitemapindex <loc> (e.g. /sitemaps)")
    p.add_argument("--no-respect-robots", dest="respect_robots",
                   action="store_false", default=True,
                   help="Crawl even paths robots.txt disallows (your own site only)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v info, -vv debug")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return p


def apply_config(args, parser) -> None:
    """Let an INI [sitemapper] section supply defaults for unset flags."""
    if not args.config:
        return
    cp = configparser.ConfigParser()
    if not cp.read(args.config):
        parser.error("config file not found: %s" % args.config)
    if not cp.has_section("sitemapper"):
        return
    sec = cp["sitemapper"]
    bools = {"include_subdomains", "include_docs", "ignore_query", "render_js",
             "gzip", "respect_robots"}
    ints = {"max_pages", "max_depth", "max_bytes"}
    floats = {"timeout", "delay"}
    for key in sec:
        attr = key.replace("-", "_")
        if not hasattr(args, attr):
            continue
        if attr in bools:
            setattr(args, attr, sec.getboolean(key))
        elif attr in ints:
            setattr(args, attr, sec.getint(key))
        elif attr in floats:
            setattr(args, attr, sec.getfloat(key))
        else:
            setattr(args, attr, sec.get(key))
    if not args.url and sec.get("url"):
        args.url = sec.get("url")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_config(args, parser)
    if not args.url:
        parser.error("a start URL is required (on the command line or in --config)")

    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    try:
        crawler = Crawler(args.url, args)
    except ValueError as e:
        parser.error(str(e))

    log.info("crawling %s", crawler.root)
    started = time.time()
    crawler.run()
    urls = crawler.urls()

    written = write_output(urls, args, crawler.root)

    elapsed = time.time() - started
    skip_summary = ", ".join("%s=%d" % (k, v) for k, v in sorted(crawler.skips.items()))
    log.warning("done: %d URLs in sitemap, %d fetched, %.1fs. skipped: %s",
                len(urls), len(crawler.seen), elapsed, skip_summary or "none")
    for w in written:
        log.warning("wrote %s", w)
    if not urls:
        log.warning("no indexable URLs found — check the start URL and robots.txt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
