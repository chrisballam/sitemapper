"""Unit + integration tests for sitemapper (stdlib only).

Runs a real local HTTP server serving in-memory fixtures, crawls it, and
asserts which URLs land in the sitemap and why the others are excluded.
"""

import os
import sys
import threading
import unittest
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
        <a href="/nofollow-target" rel="nofollow">NF</a>
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

    def test_chunking_splits_on_url_count(self):
        urls = [("https://e.com/%d" % i, None) for i in range(120)]
        chunks = list(sitemapper.chunk_urls(urls, max_urls=50))
        self.assertEqual([len(c) for c in chunks], [50, 50, 20])

    def test_xml_escaping(self):
        xml = sitemapper.build_urlset([("https://e.com/?a=1&b=2", None)])
        self.assertIn("&amp;", xml)
        self.assertNotIn("a=1&b=2", xml)


if __name__ == "__main__":
    unittest.main()
