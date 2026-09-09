"""Unit + integration tests for sitemapper (stdlib only).

Runs a real local HTTP server serving in-memory fixtures, crawls it, and
asserts which URLs land in the sitemap and why the others are excluded.
"""

import csv
import hashlib
import json
import os
import sys
import threading
import types
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sitemapper  # noqa: E402


# host -> {path: (status, content_type, headers, body)}
PAGES = {
    "/robots.txt": (200, "text/plain", {},
                    "User-agent: *\nDisallow: /private/\n"),
    "/": (200, "text/html", {}, """
        <html><head><link rel="canonical" href="/"></head><body>
        <a href="/about">About</a>
        <a href="/page1.html">Page 1</a>
        <a href="/noindex">Noindex</a>
        <a href="/private/secret">Secret</a>
        <a href="/old">Old</a>
        <a href="/dup?sid=1">Dup</a>
        <a href="/resume.pdf">Resume</a>
        <a href="https://other.example.org/x">External</a>
        <a href="https://cdn.example.net/logo.png">ExtImg</a>
        <a href="/nofollow-target" rel="nofollow">NF</a>
        <a href="/missing">Broken</a>
        </body></html>"""),
    "/about": (200, "text/html", {}, "<html><body>About us</body></html>"),
    "/page1.html": (200, "text/html", {}, "<html><body>Page one</body></html>"),
    "/noindex": (200, "text/html",
                 {"X-Robots-Tag": "noindex"},
                 "<html><body>hidden</body></html>"),
    "/private/secret": (200, "text/html", {}, "<html><body>secret</body></html>"),
    "/old": (301, "text/html", {"Location": "/page1.html"}, ""),
    "/dup?sid=1": (200, "text/html",
                   {},
                   '<html><head><link rel="canonical" href="/page1.html">'
                   "</head><body>dup of page1</body></html>"),
    "/resume.pdf": (200, "application/pdf", {}, "%PDF-1.4 fake"),
    "/nofollow-target": (200, "text/html", {}, "<html><body>nf target</body></html>"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        key = self.path
        if key not in PAGES and "?" in key:
            key = key  # exact match including query for /dup?sid=1
        status, ctype, headers, body = PAGES.get(
            self.path, (404, "text/html", {}, "not found"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k, v)
        data = body.encode("utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(data)


class Args:
    """Minimal args object mirroring the CLI namespace."""
    def __init__(self, url):
        self.url = url
        self.user_agent = sitemapper.DEFAULT_USER_AGENT
        self.robots_agent = "Sitemapper"
        self.max_pages = 1000
        self.max_depth = 50
        self.timeout = 10.0
        self.delay = 0.0
        self.max_bytes = 5_000_000
        self.include_subdomains = False
        self.include_docs = True
        self.ignore_query = False
        self.render_js = False
        self.respect_robots = True
        self.state = None
        self.progress = 0
        self.strip_params = ",".join(sorted(sitemapper.DEFAULT_STRIP_PARAMS))
        self.no_strip_params = False
        self.report_dir = None
        self.report_crawlable_only = False
        self.indexnow = None
        self.indexnow_key_location = None
        self.indexnow_endpoint = "https://api.indexnow.org/indexnow"
        self.print_cron = None
        self.gzip = False
        self.output = None


class CrawlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def crawl(self, **overrides):
        args = Args(self.base + "/")
        for k, v in overrides.items():
            setattr(args, k, v)
        c = sitemapper.Crawler(args.url, args)
        c.run()
        paths = {urlsplit(u).path for u, _ in c.urls()}
        return c, paths

    def test_includes_indexable_html(self):
        _, paths = self.crawl()
        self.assertIn("/", paths)
        self.assertIn("/about", paths)
        self.assertIn("/page1.html", paths)

    def test_excludes_noindex(self):
        _, paths = self.crawl()
        self.assertNotIn("/noindex", paths)

    def test_excludes_robots_disallowed(self):
        c, paths = self.crawl()
        self.assertNotIn("/private/secret", paths)
        self.assertIn("robots-disallowed", c.skips)

    def test_redirect_source_excluded_target_included(self):
        _, paths = self.crawl()
        self.assertNotIn("/old", paths)
        self.assertIn("/page1.html", paths)

    def test_canonical_collapses_duplicate(self):
        _, paths = self.crawl()
        # /dup?sid=1 declares canonical /page1.html, so /dup must not appear.
        self.assertNotIn("/dup", paths)

    def test_pdf_included_when_docs_on(self):
        _, paths = self.crawl(include_docs=True)
        self.assertIn("/resume.pdf", paths)

    def test_pdf_excluded_when_docs_off(self):
        _, paths = self.crawl(include_docs=False)
        self.assertNotIn("/resume.pdf", paths)

    def test_external_host_excluded(self):
        c, _ = self.crawl()
        hosts = {urlsplit(u).hostname for u in c.included}
        self.assertEqual(hosts, {"127.0.0.1"})

    def test_nofollow_link_not_crawled(self):
        _, paths = self.crawl()
        self.assertNotIn("/nofollow-target", paths)


class UnitTests(unittest.TestCase):
    def test_normalize_strips_fragment_and_lowercases_host(self):
        self.assertEqual(
            sitemapper.normalize_url("HTTP://Example.COM:80/a#frag"),
            "http://example.com/a")

    def test_normalize_rejects_non_http(self):
        self.assertIsNone(sitemapper.normalize_url("mailto:x@y.com"))

    def test_x_robots_parse(self):
        self.assertEqual(sitemapper.parse_x_robots_tag("noindex, nofollow"),
                         (True, True))
        self.assertEqual(sitemapper.parse_x_robots_tag("googlebot: noindex"),
                         (True, False))

    def test_urlset_has_no_priority_or_changefreq(self):
        xml = sitemapper.build_urlset([("https://e.com/", "2026-01-01")])
        self.assertIn("<loc>https://e.com/</loc>", xml)
        self.assertIn("<lastmod>2026-01-01</lastmod>", xml)
        self.assertNotIn("priority", xml)
        self.assertNotIn("changefreq", xml)

    def test_urlset_omits_lastmod_when_none(self):
        xml = sitemapper.build_urlset([("https://e.com/a", None),
                                       ("https://e.com/b", None)])
        self.assertIn("<loc>https://e.com/a</loc>", xml)
        self.assertNotIn("<lastmod>", xml)

    def test_chunking_splits_on_url_count(self):
        urls = [("https://e.com/%d" % i, None) for i in range(120)]
        chunks = list(sitemapper.chunk_urls(urls, max_urls=50))
        self.assertEqual([len(c) for c in chunks], [50, 50, 20])

    def test_xml_escaping(self):
        xml = sitemapper.build_urlset([("https://e.com/?a=1&b=2", None)])
        self.assertIn("&amp;", xml)
        self.assertNotIn("a=1&b=2", xml)


class QueryParamTests(unittest.TestCase):
    def test_strip_named_and_utm_keep_real(self):
        sp = sitemapper.DEFAULT_STRIP_PARAMS
        out = sitemapper.normalize_url(
            "https://e.com/p?utm_source=fb&gclid=xyz&id=5&page=2", strip_params=sp)
        self.assertEqual(out, "https://e.com/p?id=5&page=2")

    def test_no_strip_keeps_everything(self):
        out = sitemapper.normalize_url(
            "https://e.com/p?utm_source=fb&id=5", strip_params=None)
        self.assertEqual(out, "https://e.com/p?utm_source=fb&id=5")

    def test_ignore_query_drops_all(self):
        out = sitemapper.normalize_url("https://e.com/p?id=5", ignore_query=True)
        self.assertEqual(out, "https://e.com/p")


class ReportTests(unittest.TestCase):
    """broken / internal / external link reports (the --report-dir feature)."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _run_reports(self):
        import tempfile
        d = tempfile.mkdtemp()
        args = Args(self.base + "/")
        args.report_dir = d
        c = sitemapper.Crawler(args.url, args)
        c.run()
        sitemapper.write_reports(c, d)
        rows = {}
        for name in ("broken-links.csv", "internal-links.csv", "external-links.csv"):
            with open(os.path.join(d, name), newline="") as fh:
                rows[name] = list(csv.DictReader(fh))
        return rows

    def test_broken_link_and_referrer(self):
        rows = self._run_reports()["broken-links.csv"]
        broken = [(r["broken_url"], r["referring_page"]) for r in rows]
        self.assertTrue(any(u.endswith("/missing") for u, _ in broken))
        # the referrer must be the homepage that links to /missing
        ref = [r for u, r in broken if u.endswith("/missing")][0]
        self.assertTrue(ref.rstrip("/").endswith("127.0.0.1:%d"
                        % self.server.server_address[1]))

    def test_internal_and_external_split(self):
        rows = self._run_reports()
        internal = {r["target_url"] for r in rows["internal-links.csv"]}
        external = {r["target_url"] for r in rows["external-links.csv"]}
        self.assertTrue(any("/about" in u for u in internal))
        self.assertIn("https://other.example.org/x", external)
        self.assertFalse(any("other.example.org" in u for u in internal))

    def test_crawlable_only_filter(self):
        import tempfile
        d = tempfile.mkdtemp()
        args = Args(self.base + "/")
        args.report_dir = d
        args.report_crawlable_only = True
        c = sitemapper.Crawler(args.url, args)
        c.run()
        sitemapper.write_reports(c, d, crawlable_only=True)
        with open(os.path.join(d, "internal-links.csv"), newline="") as fh:
            internal = {r["target_url"] for r in csv.DictReader(fh)}
        with open(os.path.join(d, "external-links.csv"), newline="") as fh:
            external = {r["target_url"] for r in csv.DictReader(fh)}
        # robots-disallowed internal target dropped
        self.assertFalse(any(u.endswith("/private/secret") for u in internal))
        # non-HTML internal target (PDF) dropped
        self.assertFalse(any(u.endswith("/resume.pdf") for u in internal))
        # HTML internal target kept
        self.assertTrue(any(u.endswith("/about") for u in internal))
        # non-HTML external (png) dropped; HTML external kept
        self.assertFalse(any("logo.png" in u for u in external))
        self.assertIn("https://other.example.org/x", external)


class FeatureTests(unittest.TestCase):
    """cron printer, IndexNow submission, state-change tracking, atomic write."""

    def test_cron_line(self):
        ns = types.SimpleNamespace(
            url="https://e.com", output="/var/www/e/sitemap.xml",
            state="/var/lib/e.json", indexnow="KEY", gzip=False,
            print_cron="weekly")
        line = sitemapper.cron_line(ns)
        self.assertTrue(line.startswith("15 3 * * 1"))
        self.assertIn("https://e.com", line)
        self.assertIn("--state", line)
        self.assertIn("--indexnow", line)
        self.assertIn(">> /var/log/sitemapper.log 2>&1", line)

    def test_state_change_tracking(self):
        sc = sitemapper.StateCache(None)
        sc.data = {"u": {"hash": hashlib.sha256(b"x").hexdigest(),
                         "lastmod": "2026-01-01"}}
        # unchanged content -> keeps old lastmod, NOT flagged changed
        self.assertEqual(sc.lastmod_for("u", b"x", None, "2026-09-08"), "2026-01-01")
        self.assertNotIn("u", sc.changed)
        # new content -> today's date + flagged changed
        self.assertEqual(sc.lastmod_for("v", b"y", None, "2026-09-08"), "2026-09-08")
        self.assertIn("v", sc.changed)

    def test_indexnow_payload(self):
        captured = {}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def getcode(self): return 200

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data
            captured["url"] = req.full_url
            return FakeResp()

        with mock.patch.object(sitemapper, "urlopen", fake_urlopen):
            code = sitemapper.submit_indexnow(
                "example.com", "KEY123", ["https://example.com/a"],
                "https://example.com/KEY123.txt")
        self.assertEqual(code, 200)
        body = json.loads(captured["data"])
        self.assertEqual(body["host"], "example.com")
        self.assertEqual(body["key"], "KEY123")
        self.assertIn("https://example.com/a", body["urlList"])
        self.assertTrue(body["keyLocation"].endswith("KEY123.txt"))

    def test_indexnow_empty_is_noop(self):
        # No URLs -> no request attempted, returns None.
        with mock.patch.object(sitemapper, "urlopen",
                               side_effect=AssertionError("should not POST")):
            self.assertIsNone(sitemapper.submit_indexnow("e.com", "K", []))

    def test_atomic_write_leaves_no_tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "sitemap.xml")
        sitemapper.write_text(p, "<x/>", False)
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "<x/>")
        self.assertEqual([f for f in os.listdir(d) if ".tmp" in f], [])


class RobotsTests(unittest.TestCase):
    """Guards the reason we dropped urllib.robotparser: wildcard rules must
    match regardless of Python version (some builds encode '*' as %2A)."""

    def rules(self, text):
        r = sitemapper.RobotsRules()
        r.parse(text.splitlines())
        return r

    def test_mid_path_wildcard_blocks(self):
        r = self.rules("User-agent: *\nDisallow: /gallery/*/photo/\n")
        self.assertFalse(r.allowed("Sitemapper", "/gallery/123/photo/1/"))
        self.assertTrue(r.allowed("Sitemapper", "/gallery/123/"))

    def test_prefix_disallow(self):
        r = self.rules("User-agent: *\nDisallow: /private/\n")
        self.assertFalse(r.allowed("Bot", "/private/x"))
        self.assertTrue(r.allowed("Bot", "/public/x"))

    def test_allow_overrides_longer_match(self):
        r = self.rules("User-agent: *\nDisallow: /a/\nAllow: /a/keep/\n")
        self.assertTrue(r.allowed("Bot", "/a/keep/page"))
        self.assertFalse(r.allowed("Bot", "/a/other"))

    def test_end_anchor(self):
        r = self.rules("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(r.allowed("Bot", "/files/x.pdf"))
        self.assertTrue(r.allowed("Bot", "/files/x.pdf?v=1"))

    def test_empty_disallow_allows_all(self):
        r = self.rules("User-agent: *\nDisallow:\n")
        self.assertTrue(r.allowed("Bot", "/anything"))

    def test_specific_group_beats_star(self):
        r = self.rules("User-agent: *\nDisallow: /\n\n"
                       "User-agent: Sitemapper\nDisallow: /secret/\n")
        self.assertTrue(r.allowed("Sitemapper", "/public"))
        self.assertFalse(r.allowed("Sitemapper", "/secret/x"))
        self.assertFalse(r.allowed("OtherBot", "/public"))

    def test_crawl_delay(self):
        r = self.rules("User-agent: *\nCrawl-delay: 2.5\n")
        self.assertEqual(r.crawl_delay("Bot"), 2.5)


if __name__ == "__main__":
    unittest.main()
