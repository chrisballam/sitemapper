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
import csv
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import (parse_qsl, urldefrag, urlencode, urljoin, urlsplit,
                          urlunsplit)
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

__version__ = "1.1.0"

# Query params dropped during URL normalization by default: pure click/tracking
# junk that never identifies a distinct page. Any `utm_*` param is also dropped.
# This is intentionally conservative — it never touches params a site might use
# as a real page id (`?id=`, `?page=`, `?product=`). To collapse ALL query
# variants, use --ignore-query (opt-in); to disable stripping, --no-strip-params.
DEFAULT_STRIP_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "gclsrc", "yclid", "wbraid", "gbraid",
    "mc_cid", "mc_eid", "mkt_tok", "igshid", "_ga", "_gl", "vero_id", "oly_enc_id",
}

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
def _keep_param(name: str, strip_params) -> bool:
    n = name.lower()
    if n.startswith("utm_"):
        return False
    return n not in strip_params


def normalize_url(url: str, base: str | None = None, ignore_query: bool = False,
                  strip_params=None) -> str | None:
    """Resolve, strip fragment, lowercase host, drop default port. With
    ignore_query, drop the whole query; otherwise drop only tracking params in
    strip_params (plus utm_*). Returns a normalized absolute http(s) URL, or
    None if the scheme is not http(s)."""
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    # Guard against a malformed port (parts.port raises ValueError on junk).
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and not (
        (parts.scheme == "http" and port == 80)
        or (parts.scheme == "https" and port == 443)
    ):
        netloc = "%s:%d" % (host, port)
    else:
        netloc = host
    # Deliberately drop any "user:pass@" userinfo — credentials must never appear
    # in a sitemap or a link report.
    path = parts.path or "/"
    if ignore_query:
        query = ""
    else:
        query = parts.query
        if query and strip_params:
            kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                    if _keep_param(k, strip_params)]
            query = urlencode(kept)
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
# robots.txt (self-contained; deterministic across Python versions)
# --------------------------------------------------------------------------- #
# NOTE: we deliberately do NOT use urllib.robotparser. Some Python builds
# percent-encode '*' in Allow/Disallow paths (storing "/a/%2A/b" instead of
# "/a/*/b"), which silently breaks wildcard rules and lets a crawler fetch paths
# the site disallowed. This implements the widely-supported '*' / '$' wildcard
# semantics with longest-match-wins and Allow-beats-Disallow on ties.
class RobotsRules:
    def __init__(self):
        # list of groups: {"agents": set[str], "rules": [(allow, pat, rx, length)],
        #                   "delay": float|None}
        self.groups = []

    @staticmethod
    def _compile(pattern):
        end_anchor = pattern.endswith("$")
        core = pattern[:-1] if end_anchor else pattern
        out = ["^"]
        for ch in core:
            out.append(".*" if ch == "*" else re.escape(ch))
        if end_anchor:
            out.append("$")
        return re.compile("".join(out))

    def parse(self, lines):
        cur = None
        last_was_rule = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, val = line.partition(":")
            field = field.strip().lower()
            val = val.strip()
            if field == "user-agent":
                if cur is None or last_was_rule:
                    cur = {"agents": set(), "rules": [], "delay": None}
                    self.groups.append(cur)
                    last_was_rule = False
                cur["agents"].add(val.lower())
            elif field in ("allow", "disallow"):
                if cur is None:
                    continue
                last_was_rule = True
                if val == "":
                    continue  # empty Disallow = allow all; empty Allow = no-op
                cur["rules"].append((field == "allow", val,
                                     self._compile(val), len(val.rstrip("$"))))
            elif field == "crawl-delay":
                if cur is not None:
                    last_was_rule = True
                    try:
                        cur["delay"] = float(val)
                    except ValueError:
                        pass

    def _select(self, agent):
        agent = agent.lower()
        star = None
        best_sub = None
        best_len = -1
        for g in self.groups:
            if agent in g["agents"]:
                return g
            for a in g["agents"]:
                if a == "*":
                    star = g
                elif a and a in agent and len(a) > best_len:
                    best_sub, best_len = g, len(a)
        return best_sub or star

    def allowed(self, agent, path):
        g = self._select(agent)
        if g is None:
            return True
        best_len, best_allow = -1, True
        for allow, _pat, rx, length in g["rules"]:
            if rx.match(path) and (length > best_len
                                   or (length == best_len and allow and not best_allow)):
                best_len, best_allow = length, allow
        return True if best_len == -1 else best_allow

    def crawl_delay(self, agent):
        g = self._select(agent)
        return g["delay"] if g else None


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class Crawler:
    def __init__(self, root: str, args) -> None:
        self.args = args
        # Query-param stripping: None disables it; otherwise a set of exact
        # param names (utm_* is always dropped when stripping is on).
        if getattr(args, "no_strip_params", False):
            self.strip_params = None
        else:
            raw = getattr(args, "strip_params", None)
            self.strip_params = {p.strip().lower()
                                 for p in (raw or "").split(",") if p.strip()}
        self.root = self._norm(root)
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

        # Link-report collectors (populated only when --report-dir is set).
        self.report = getattr(args, "report_dir", None) is not None
        self.fetch_status: dict[str, int] = {}       # url -> HTTP status (0=err)
        self.internal_edges: set = set()             # (source_page, target)
        self.external_edges: set = set()             # (source_page, target)
        self.referrers: dict[str, set] = {}          # target -> {source pages}

    def _norm(self, url: str, base: str | None = None):
        return normalize_url(url, base=base, ignore_query=self.args.ignore_query,
                             strip_params=self.strip_params)

    # ---- robots -----------------------------------------------------------
    def _load_robots(self):
        rules = RobotsRules()
        robots_url = urljoin(self.root, "/robots.txt")
        res = fetch(robots_url, self.user_agent, self.args.timeout, 1_000_000)
        if res.status == 200 and res.body:
            rules.parse(res.body.decode("utf-8", "replace").splitlines())
            log.info("loaded robots.txt (%d bytes)", len(res.body))
        else:
            # No robots.txt => everything allowed (protocol default).
            log.info("no robots.txt found (status %s); allowing all", res.status)
        return rules

    @staticmethod
    def _path_for_match(url: str) -> str:
        parts = urlsplit(url)
        path = parts.path or "/"
        return path + ("?" + parts.query if parts.query else "")

    def _allowed(self, url: str) -> bool:
        if not self.args.respect_robots:
            return True
        try:
            return self.robots.allowed(self.args.robots_agent,
                                       self._path_for_match(url))
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
            if self.report:
                self.fetch_status[url] = res.status if res.status else 0
            if self.args.progress and pages % self.args.progress == 0:
                log.info("progress: fetched=%d included=%d queued=%d",
                         pages, len(self.included), len(queue))
            if delay:
                time.sleep(delay)

            if res.error or res.status != 200:
                self._skip("non-200")
                log.debug("skip %s (%s)", url, res.error or res.status)
                continue

            # A redirect means this URL is not itself canonical content.
            final = self._norm(res.final_url)
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
            c = self._norm(p.canonical, base=url)
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

        # Record every outbound link for the reports (internal vs external),
        # independent of whether we will crawl it, then apply crawl rules.
        follow = not (nofollow or depth >= self.args.max_depth)
        for href, link_nofollow in p.links:
            nxt = self._norm(href, base=url)
            if not nxt:
                continue
            if self.report:
                self._record_link(url, nxt)
            if not follow or link_nofollow or nxt in self.seen:
                continue
            if not same_scope(nxt, self.root_host, self.args.include_subdomains):
                continue
            self.seen.add(nxt)
            # Skip robots-disallowed links before they enter the frontier, so a
            # disallowed section (e.g. millions of gallery pages) can't bloat the
            # queue. The root is still checked at pop time.
            if not self._allowed(nxt):
                self._skip("robots-disallowed")
                continue
            queue.append((nxt, depth + 1))

    def _record_link(self, source: str, target: str) -> None:
        t_host = urlsplit(target).hostname or ""
        if registrable_host(t_host) == registrable_host(self.root_host):
            self.internal_edges.add((source, target))
            self.referrers.setdefault(target, set()).add(source)
        else:
            self.external_edges.add((source, target))

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


# File extensions treated as non-HTML resources for the crawlable-only filter.
NON_HTML_EXTS = {
    "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "avif", "ico", "bmp",
    "tif", "tiff", "css", "js", "mjs", "json", "xml", "rss", "atom", "txt",
    "csv", "zip", "gz", "tgz", "tar", "rar", "7z", "mp4", "webm", "mov", "avi",
    "mkv", "mp3", "wav", "ogg", "flac", "m4a", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "woff", "woff2", "ttf", "eot", "otf", "dmg", "exe", "apk",
    "iso", "wasm", "map",
}


def _looks_non_html(url: str) -> bool:
    """True if the URL path ends in a known non-HTML file extension."""
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    return last.rsplit(".", 1)[-1].lower() in NON_HTML_EXTS


def _report_keep(crawler, target: str, internal: bool) -> bool:
    """Filter used when --report-crawlable-only is set: keep only HTML targets
    that robots.txt would let a crawler fetch (robots only applies to internal
    targets on your own domain)."""
    if _looks_non_html(target):
        return False
    if internal and not crawler._allowed(target):
        return False
    return True


def write_reports(crawler, report_dir: str, crawlable_only: bool = False) -> dict:
    """Write broken-links.csv, internal-links.csv, external-links.csv.

    - broken-links.csv: one row per (broken URL, referring page). "Broken" is any
      fetched URL that returned 4xx/5xx or a connection error. The referring page
      is where the link lives, so you know exactly where to fix it.
    - internal-links.csv / external-links.csv: every discovered link edge
      (source page -> target), classified by whether the target is on your own
      registrable domain.
    """
    os.makedirs(report_dir, exist_ok=True)

    internal = crawler.internal_edges
    external = crawler.external_edges
    if crawlable_only:
        internal = {(s, t) for s, t in internal if _report_keep(crawler, t, True)}
        external = {(s, t) for s, t in external if _report_keep(crawler, t, False)}
    counts = {"broken": 0, "internal": len(internal), "external": len(external)}

    broken = sorted((u, s) for u, s in crawler.fetch_status.items()
                    if s == 0 or s >= 400)
    counts["broken"] = len(broken)
    with open(os.path.join(report_dir, "broken-links.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["broken_url", "status", "referring_page"])
        for u, s in broken:
            status = "error" if s == 0 else s
            refs = sorted(crawler.referrers.get(u, ())) or ["(start URL / no recorded referrer)"]
            for r in refs:
                w.writerow([u, status, r])

    for name, edges in (("internal-links.csv", internal),
                        ("external-links.csv", external)):
        with open(os.path.join(report_dir, name), "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["source_page", "target_url"])
            for src, tgt in sorted(edges):
                w.writerow([src, tgt])
    return counts


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
                   help="Treat URLs that differ only by query string as one "
                        "(drops the ENTIRE query; opt-in — can merge genuinely "
                        "distinct ?id=/?page= pages, so off by default)")
    p.add_argument("--strip-params", default=",".join(sorted(DEFAULT_STRIP_PARAMS)),
                   help="Comma-separated query params to drop during normalization "
                        "(tracking junk). utm_* is always dropped. Default: a "
                        "conservative tracking list. Has no effect with --ignore-query.")
    p.add_argument("--no-strip-params", action="store_true",
                   help="Disable query-param stripping entirely (keep every param)")
    p.add_argument("--report-dir", default=None,
                   help="Also write broken-links.csv, internal-links.csv and "
                        "external-links.csv into this directory")
    p.add_argument("--report-crawlable-only", action="store_true",
                   help="In the internal/external link reports, keep only HTML "
                        "targets a crawler could fetch: drop robots-disallowed "
                        "targets and non-HTML files (PDF/image/JS/...). "
                        "broken-links.csv is never filtered.")
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
    p.add_argument("--progress", type=int, default=25,
                   help="Log a progress line every N pages (0 = off)")
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
             "gzip", "respect_robots", "no_strip_params", "report_crawlable_only"}
    ints = {"max_pages", "max_depth", "max_bytes", "progress"}
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
    log.warning("done: %d URLs in sitemap, %d discovered, %.1fs. skipped: %s",
                len(urls), len(crawler.seen), elapsed, skip_summary or "none")
    for w in written:
        log.warning("wrote %s", w)

    if args.report_dir is not None:
        c = write_reports(crawler, args.report_dir, args.report_crawlable_only)
        log.warning("reports: %d broken link(s), %d internal, %d external -> %s",
                    c["broken"], c["internal"], c["external"], args.report_dir)

    if not urls:
        log.warning("no indexable URLs found — check the start URL and robots.txt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
