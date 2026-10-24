import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from gpta import reader
from gpta.blocks import Heading, Rule, Text, text_of

ARTICLE = """<!doctype html>
<html class="client-nojs vector-feature-main-menu-disabled"><head>
<meta charset="utf-8">
<title>How receipts work – The Paper Blog</title>
<meta property="og:site_name" content="The Paper Blog">
<meta name="author" content="Ada Lovelace">
<script>var tracking = "do not print";</script>
<style>p { color: red }</style>
</head><body>
<header><a href="/">The Paper Blog</a></header>
<nav><ul><li>Home</li><li>About</li></ul></nav>
<div class="overflow-hidden"><main>
<article>
  <h1>How receipts work</h1>
  <div class="share-buttons">Share on everything</div>
  <p>Thermal paper turns dark where it is heated, so a receipt printer
     needs no ink at all.</p>
  <h2>The print head</h2>
  <p>A row of tiny heaters &mdash; 576 of them on 80&nbsp;mm paper.<br>Each
  is a dot.</p>
  <ul><li>Cheap</li><li>Quiet</li></ul>
  <ol><li>Feed</li><li>Cut</li></ol>
  <blockquote>Paper is patient.</blockquote>
  <pre>GS v 0</pre>
  <hr>
  <p hidden>secret</p>
</article>
</main></div>
<aside>Popular posts</aside>
<footer>Copyright</footer>
</body></html>"""


class UrlTests(unittest.TestCase):
    def test_check_url(self):
        self.assertEqual(reader.check_url(" example.com/a "), "https://example.com/a")
        self.assertEqual(reader.check_url("http://example.com"), "http://example.com")
        bad_urls = ["", "   ", None, "ftp://example.com", "file:///etc/passwd"]
        bad_urls += ["https://user:pw@example.com/", "https://"]
        for bad in bad_urls:
            with self.subTest(bad=bad), self.assertRaises(reader.ReaderError):
                reader.check_url(bad)

    def test_public_addresses(self):
        private = ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1"]
        private += ["169.254.169.254", "100.64.0.1", "0.0.0.0", "::1"]
        private += ["fe80::1%eth0", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"]
        for address in private:
            self.assertFalse(reader._public(address), address)
        for address in ("93.184.215.14", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(reader._public(address), address)

    def test_local_hosts_are_refused_before_connecting(self):
        for url in ("http://localhost/", "http://127.0.0.1:631/", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(reader.ReaderError) as ctx:
                reader.fetch_page(url, timeout=2)
            self.assertIn("local or private", str(ctx.exception))

    def test_connection_to_local_peer_is_refused(self):
        """Even if DNS said "public", connecting to a local peer is refused."""
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(reader, "_resolve_public"):
                with self.assertRaises(reader.ReaderError) as ctx:
                    reader.fetch_page(
                        f"http://127.0.0.1:{server.server_address[1]}/", timeout=2
                    )
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("local or private", str(ctx.exception))
        self.assertEqual(hits, [])  # no request reached the local server

    def test_redirect_to_local_address_is_refused(self):
        handler = reader._Redirects()
        request = urllib.request.Request("https://example.com/")
        for target in ("http://127.0.0.1/admin", "file:///etc/passwd"):
            with self.subTest(target=target), self.assertRaises(reader.ReaderError):
                handler.redirect_request(request, None, 302, "Found", {}, target)


class WindowTests(unittest.TestCase):
    BODY = (
        Heading("Intro"),  # 0-5
        Text("alpha beta gamma delta"),  # 5-27
        Rule(),  # 27-28
        Text("epsilon zeta"),  # 28-40
    )

    def test_whole_body_fits(self):
        part = reader.window(self.BODY, 0, 100)
        self.assertEqual(part.body, self.BODY)
        self.assertEqual((part.start, part.end, part.total), (0, 40, 40))
        self.assertTrue(part.complete)

    def test_cut_on_word_boundaries(self):
        part = reader.window(self.BODY, 0, 15)  # limit falls inside "gamma"
        self.assertEqual(part.body, (Heading("Intro"), Text("alpha beta…")))
        self.assertEqual(part.end, 16)  # just past the space after "beta"
        self.assertFalse(part.complete)
        rest = reader.window(self.BODY, part.end, 100)
        self.assertEqual(rest.body[0], Text("…gamma delta"))
        self.assertEqual(rest.body[1:], (Rule(), Text("epsilon zeta")))
        self.assertEqual((rest.start, rest.end), (16, 40))
        self.assertEqual(part.end, rest.start)

    def test_start_inside_a_word_moves_to_the_next(self):
        part = reader.window(self.BODY, 7, 100)  # inside "alpha"
        self.assertEqual(part.body[0], Text("…beta gamma delta"))
        self.assertEqual(part.start, 11)

    def test_start_past_the_end(self):
        part = reader.window(self.BODY, 500, 100)
        self.assertEqual((part.body, part.start, part.end), ((), 40, 40))

    def test_part_header_and_footer(self):
        article = reader.Article("T", "site", "", "https://x.example/", self.BODY)
        blocks = article.elements(reader.window(self.BODY, 0, 15))
        texts = [text_of(block) for block in blocks]
        self.assertIn("characters 1–16 of 40", texts)
        self.assertEqual(texts[-1], "Continues: next part starts at character 17")
        full = article.elements(reader.window(self.BODY, 0, 100))
        self.assertNotIn("Continues", " ".join(text_of(block) for block in full))


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.article = reader.extract(ARTICLE, "https://paper.example/receipts")

    def test_metadata(self):
        self.assertEqual(self.article.title, "How receipts work")
        self.assertEqual(self.article.site, "The Paper Blog")
        self.assertEqual(self.article.byline, "Ada Lovelace")

    def test_body_blocks(self):
        body = list(self.article.body)
        first = "Thermal paper turns dark where it is heated, so a receipt printer"
        self.assertEqual(body[0], Text(first + " needs no ink at all."))
        self.assertEqual(body[1], Heading("The print head"))
        self.assertEqual(
            body[2],
            Text("A row of tiny heaters — 576 of them on 80 mm paper.\nEach is a dot."),
        )
        texts = [text_of(block) for block in body]
        for expected in (
            "• Cheap",
            "• Quiet",
            "1. Feed",
            "2. Cut",
            "“Paper is patient.”",
            "GS v 0",
        ):
            self.assertIn(expected, texts)
        self.assertIsInstance(body[-1], Rule)
        joined = " ".join(texts)
        for chrome in (
            "tracking",
            "color",
            "Home",
            "Share on",
            "Popular",
            "Copyright",
            "secret",
        ):
            self.assertNotIn(chrome, joined)
        # The h1 repeats the title, so it is not printed twice.
        self.assertNotIn(Heading("How receipts work"), body)

    def test_elements_header(self):
        head = self.article.elements()[:4]
        self.assertEqual(head[0], Heading("How receipts work"))
        self.assertEqual(head[1], Text("Ada Lovelace · The Paper Blog", size="small"))
        self.assertEqual(head[2], Text("https://paper.example/receipts", size="small"))
        self.assertEqual(head[3], Rule(heavy=True))

    def test_sections_are_merged_without_article_tag(self):
        para = "<p>" + "Words, more words, and still more words. " * 4 + "</p>"
        html = (
            "<html><body><div id='menu'>Menu</div><div id='content'>"
            f"<section><h2>One</h2>{para}{para}</section>"
            f"<section><h2>Two</h2>{para}</section>"
            "</div></body></html>"
        )
        body = reader.extract(html, "https://wiki.example/x").body
        self.assertIn(Heading("One"), body)
        self.assertIn(Heading("Two"), body)
        self.assertEqual(sum(isinstance(b, Text) for b in body), 3)

    def test_empty_page(self):
        with self.assertRaises(reader.ReaderError):
            reader.extract(
                "<html><body><nav>only nav</nav></body></html>", "https://x.example"
            )

    def test_clean_title(self):
        self.assertEqual(reader.clean_title("Post – Blog", "Blog"), "Post")
        self.assertEqual(reader.clean_title("Blog | Post title", "Blog"), "Post title")
        self.assertEqual(reader.clean_title("A - B", ""), "A")
        long = "Breaking - a very long subtitle that is clearly part of it"
        self.assertEqual(reader.clean_title(long, "news.example"), long)
        self.assertEqual(reader.clean_title("Plain", "x"), "Plain")


if __name__ == "__main__":
    unittest.main()
