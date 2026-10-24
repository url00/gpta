"""Reader mode: fetch a public web page and reduce it to receipt blocks.

Two halves:

- :func:`fetch_page` downloads a URL for the operator. It only speaks
  http/https and refuses to connect to non-public addresses (loopback,
  private, link-local, ...), checked on the socket actually connected and
  again on every redirect, so a pasted URL cannot reach the local network,
  the printer's CUPS page, or cloud metadata endpoints.
- :func:`extract` turns HTML into blocks: it drops page chrome (navigation,
  headers, footers, sidebars, scripts, forms), picks the element that holds
  the article text, and maps headings, paragraphs, lists, quotes, code and
  rules to block kinds. Standard library only.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

from . import __version__
from .blocks import Element, Heading, Rule, Text

MAX_PAGE_BYTES = 3_000_000
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 15.0
HTML_TYPES = ("text/html", "application/xhtml+xml")


class ReaderError(Exception):
    """The page could not be fetched or holds no readable text."""


# -- fetching -----------------------------------------------------------
def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> str:
    """``url`` if it is an http(s) URL with a host, else raise ReaderError."""
    if not isinstance(url, str) or not url.strip():
        raise ReaderError("enter a URL")
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ReaderError("only http and https URLs can be fetched")
    if not parts.hostname:
        raise ReaderError("the URL has no host")
    if parts.username or parts.password:
        raise ReaderError("URLs with a user name or password are not fetched")
    return url


class _PublicOnly:
    """Connection mixin: after connecting, refuse a non-public peer."""

    def connect(self) -> None:
        super().connect()
        peer = self.sock.getpeername()[0]
        if not _public(peer):
            self.close()
            raise ReaderError(
                f"{self.host} resolves to {peer}, a local or private address; "
                "only public sites can be fetched"
            )


class _HTTPConnection(_PublicOnly, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_PublicOnly, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        _resolve_public(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    # No ProxyHandler: an environment proxy would hide the real peer address.
    opener = urllib.request.OpenerDirector()
    for handler in (
        _HTTPHandler(),
        _HTTPSHandler(),
        _Redirects(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
    ):
        opener.add_handler(handler)
    return opener


@dataclass(frozen=True)
class Page:
    url: str  # final URL, after redirects
    html: str


def _unwrap(exc: BaseException) -> ReaderError | None:
    """The ReaderError urllib wrapped in a URLError, if any."""
    while exc is not None:
        if isinstance(exc, ReaderError):
            return exc
        exc = getattr(exc, "reason", None) or exc.__cause__ or exc.__context__
        if not isinstance(exc, BaseException):
            return None
    return None


def _resolve_public(host: str) -> None:
    """Refuse up front a host whose every address is non-public.

    This gives a clear error instead of a connection timeout. The connection
    itself is checked again, which also covers DNS answers that change.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ReaderError(f"cannot find the site {host}") from exc
    addresses = {info[4][0] for info in infos}
    if addresses and not any(_public(address) for address in addresses):
        raise ReaderError(
            f"{host} is a local or private address; only public sites can be fetched"
        )


def fetch_page(url: str, *, timeout: float = FETCH_TIMEOUT) -> Page:
    """Download an HTML page from a public address."""
    url = check_url(url)
    _resolve_public(urllib.parse.urlsplit(url).hostname)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Mozilla/5.0 (compatible; gpta/{__version__} reader)",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        },
    )
    try:
        with _opener().open(request, timeout=timeout) as resp:
            ctype = resp.headers.get_content_type()
            if ctype not in HTML_TYPES:
                raise ReaderError(f"not a web page ({ctype})")
            raw = resp.read(MAX_PAGE_BYTES)
            charset = resp.headers.get_content_charset()
            final = resp.geturl()
    except ReaderError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReaderError(f"the site answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        inner = _unwrap(exc)
        if inner is not None:
            raise inner from exc
        reason = getattr(exc, "reason", exc)
        raise ReaderError(f"could not fetch the page: {reason}") from exc
    return Page(final, _decode(raw, charset))


def _decode(raw: bytes, charset: str | None) -> str:
    if not charset:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# -- parsing ------------------------------------------------------------
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}  # fmt: skip
# Never content: dropped with everything inside them.
DROP = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "form", "button", "select", "textarea", "nav", "aside", "footer",
    "header", "figure", "picture", "video", "audio", "dialog", "menu",
}  # fmt: skip
# Class or id names that mark page furniture rather than the article. A name
# matches when it is one of these words with at most one word either side
# ("site-footer", "share-buttons"), so long utility or state class names
# such as "vector-feature-main-menu-disabled" do not.
JUNK_WORDS = (
    "nav|navbar|navigation|menu|breadcrumbs?|sidebar|footer|masthead|comments?|"
    "share|sharing|social|related|recommended|promo|ads?|advert|advertisement|"
    "sponsored|newsletter|subscribe|signup|cookies?|consent|banner|popup|modal|"
    "toolbar|skip|visually-hidden|sr-only|print-only|noprint|navbox|reflist|"
    "references|infobox|toc|catlinks|mw-editsection|hatnote"
)
JUNK = re.compile(rf"^(?:[a-z0-9]+[-_])?(?:{JUNK_WORDS})(?:[-_][a-z0-9]+)?$", re.I)
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Never dropped, whatever their class says.
KEEP = {"html", "body", "main", "article"}
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "hr",
    "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr",
    "div", "section", "article",
    "main", "figcaption",
}  # fmt: skip


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None


class _TreeBuilder(HTMLParser):
    """A forgiving HTML parser that builds a small tree of :class:`Node`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and "content" in attrs:
                self.meta.setdefault(key, attrs["content"])
            return
        if tag == "title":
            self._in_title = True
        # Implicitly close an open <p> or <li> the way browsers do.
        if tag in BLOCK_TAGS and self.current.tag == "p":
            self._close("p")
        if tag == "li" and self.current.tag == "li":
            self._close("li")
        node = Node(tag, attrs, parent=self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.current.tag == tag:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        self._close(tag)

    def _close(self, tag: str) -> None:
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        self.current.children.append(data)


def _junk(node: Node) -> bool:
    if node.tag in DROP:
        return True
    attrs = node.attrs
    if "hidden" in attrs or attrs.get("aria-hidden") == "true":
        return True
    if attrs.get("role") in ("navigation", "banner", "contentinfo", "complementary"):
        return True
    if node.tag in KEEP:
        return False
    names = f"{attrs.get('class', '')} {attrs.get('id', '')}".split()
    return any(JUNK.match(name) for name in names)


def _prune(node: Node) -> None:
    node.children = [
        child for child in node.children if isinstance(child, str) or not _junk(child)
    ]
    for child in node.children:
        if isinstance(child, Node):
            _prune(child)


def _text(node: Node | str, *, raw: bool = False) -> str:
    """Text of ``node``. Source line breaks are spaces unless ``raw`` (``pre``);
    ``<br>`` and nested blocks always break the line."""
    if isinstance(node, str):
        return node if raw else re.sub(r"\s+", " ", node)
    if node.tag == "br":
        return "\n"
    inner = "".join(_text(child, raw=raw) for child in node.children)
    # A block inside inline markup (a card's headline and summary inside one
    # link) still starts a new line.
    return f"\n{inner}\n" if node.tag in BLOCK_TAGS else inner


def _squash(text: str) -> str:
    """Collapse whitespace but keep explicit line breaks."""
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _walk(node: Node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _walk(child)


def _content_root(root: Node) -> Node:
    """The element most likely to hold the article."""
    articles = [n for n in _walk(root) if n.tag == "article"]
    if articles:
        return max(articles, key=lambda n: len(_squash(_text(n))))
    # Readability-style scoring: each paragraph credits its parent fully and
    # its grandparent by half, so the container of the main text wins.
    scores: dict[int, float] = {}
    nodes: dict[int, Node] = {}
    for node in _walk(root):
        if node.tag not in ("p", "pre", "blockquote"):
            continue
        length = len(_squash(_text(node)))
        if length < 25:
            continue
        weight = 1 + min(length, 600) / 100 + _text(node).count(",")
        for depth, ancestor in enumerate(
            (node.parent, getattr(node.parent, "parent", None))
        ):
            if ancestor is None:
                break
            nodes[id(ancestor)] = ancestor
            scores[id(ancestor)] = scores.get(id(ancestor), 0) + weight / (depth + 1)
    if scores:
        best = nodes[max(scores, key=scores.get)]
        parent = best.parent
        if parent is None:
            return best
        # Like Readability, also keep sibling sections that hold article text
        # (e.g. an encyclopedia page split into one <section> per heading).
        floor = max(5.0, scores[id(best)] * 0.2)
        kept = [
            child
            for child in parent.children
            if child is best
            or isinstance(child, Node)
            and (scores.get(id(child), 0) >= floor or child.tag in HEADINGS)
        ]
        return Node("div", children=kept, parent=parent)
    mains = [n for n in _walk(root) if n.tag == "main"]
    if mains:
        return mains[0]
    bodies = [n for n in _walk(root) if n.tag == "body"]
    return bodies[0] if bodies else root


def _emit(node: Node, out: list[Element], title: str) -> None:
    tag = node.tag
    if tag in ("h1", "h2"):
        text = _squash(_text(node))
        if text and text.casefold() != title.casefold():
            out.append(Heading(text))
        return
    if tag in ("h3", "h4", "h5", "h6", "dt"):
        text = _squash(_text(node))
        if text:
            out.append(Text(text, bold=True))
        return
    if tag in ("p", "dd", "figcaption"):
        text = _squash(_text(node))
        if text:
            out.append(Text(text, size="small" if tag == "figcaption" else "body"))
        return
    if tag == "pre":
        text = _text(node, raw=True).strip("\n")
        if text.strip():
            out.append(Text(text, size="small"))
        return
    if tag == "blockquote":
        text = _squash(_text(node))
        if text:
            out.append(Text("“" + text + "”"))
        return
    if tag == "hr":
        out.append(Rule())
        return
    if tag in ("ul", "ol"):
        number = 0
        for child in node.children:
            if not isinstance(child, Node) or child.tag != "li":
                continue
            number += 1
            text = _squash(_text(child))
            if text:
                mark = f"{number}." if tag == "ol" else "•"
                out.append(Text(f"{mark} {text}"))
        return
    if tag == "tr":
        cells = [
            _squash(_text(child))
            for child in node.children
            if isinstance(child, Node) and child.tag in ("td", "th")
        ]
        if any(cells):
            out.append(Text(" | ".join(cells), size="small"))
        return
    # A container: emit loose text as a paragraph, recurse into elements.
    loose: list[str] = []
    for child in node.children:
        if isinstance(child, str) or child.tag not in BLOCK_TAGS:
            loose.append(_text(child))
            continue
        if _squash("".join(loose)):
            out.append(Text(_squash("".join(loose))))
        loose = []
        _emit(child, out, title)
    if _squash("".join(loose)):
        out.append(Text(_squash("".join(loose))))


@dataclass(frozen=True)
class Article:
    title: str
    site: str
    byline: str
    url: str
    body: tuple[Element, ...]

    def elements(self) -> list[Element]:
        """The article as receipt blocks: a header, then the body."""
        head: list[Element] = [Heading(self.title or self.site or self.url)]
        site = "" if self.site.casefold() == self.title.casefold() else self.site
        credit = " · ".join(part for part in (self.byline, site) if part)
        if credit:
            head.append(Text(credit, size="small"))
        head.append(Text(self.url, size="small"))
        head.append(Rule(heavy=True))
        return head + list(self.body)


TITLE_SEPARATORS = re.compile(r"\s+[|–—·:-]\s+")


def clean_title(title: str, site: str) -> str:
    """Drop a trailing or leading site name such as "Post title – Blog name"."""
    parts = TITLE_SEPARATORS.split(title)
    if len(parts) < 2:
        return title
    first, last = parts[0], parts[-1]
    if site and first.casefold() == site.casefold():
        return title[len(first) :].lstrip(" |–—·:-")
    if site and last.casefold() == site.casefold() or len(last.split()) <= 4:
        return title[: len(title) - len(last)].rstrip(" |–—·:-")
    return title


def extract(html: str, url: str) -> Article:
    """Reduce an HTML page to an :class:`Article`; raises ReaderError if empty."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    meta = builder.meta
    title = _squash(meta.get("og:title") or builder.title or "")
    site = _squash(meta.get("og:site_name") or "") or (
        urllib.parse.urlsplit(url).hostname or ""
    ).removeprefix("www.")
    title = clean_title(title, site)
    byline = _squash(meta.get("author") or meta.get("article:author") or "")
    if byline.startswith(("http://", "https://")):
        byline = ""
    root = builder.root
    _prune(root)
    body: list[Element] = []
    _emit(_content_root(root), body, title)
    if not any(isinstance(b, Text) for b in body):
        raise ReaderError("no readable text found on the page")
    return Article(title, site, byline, url, tuple(body))
