"""Lightweight, dependency-free web search + page fetch for the Researcher agent.

Uses DuckDuckGo's HTML endpoint (no API key needed) for search results, and a
stdlib HTMLParser to strip fetched pages down to plain text. Best-effort by
design: any network or parsing failure yields an empty result rather than
raising, since the Researcher can still fall back on the model's own
knowledge if the web is unreachable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ghost_writer-research/1.0"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "header", "footer", "nav"}

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", parser.text())


def _clean_ddg_url(url: str) -> str:
    """DuckDuckGo's HTML results wrap outbound links in a redirect; unwrap it."""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return url


def web_search(query: str, max_results: int = 5, timeout: float = 10.0) -> list[SearchResult]:
    """Searches DuckDuckGo's no-JS HTML endpoint. Returns [] on any failure."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    results: list[SearchResult] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for match in pattern.finditer(resp.text):
        raw_url, title_html, snippet_html = match.groups()
        url = _clean_ddg_url(raw_url)
        title = html_to_text(title_html).strip()
        snippet = html_to_text(snippet_html).strip()
        if url and title:
            results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


def wikipedia_lookup(title: str, timeout: float = 10.0) -> SearchResult | None:
    """Looks up a topic directly via Wikipedia's REST summary API, which
    gives a canonical, revision-stable URL - a more reliable citation target
    than a scraped search-result link. Returns None on any failure or if the
    topic has no article (e.g. a disambiguation-only or missing page)."""
    try:
        resp = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None
    content_urls = payload.get("content_urls", {}).get("desktop", {})
    url = content_urls.get("page")
    extract = payload.get("extract")
    if not url or not extract:
        return None
    return SearchResult(title=payload.get("title", title), url=url, snippet=extract)


def osm_geocode(query: str, timeout: float = 10.0) -> SearchResult | None:
    """Looks up a place via OpenStreetMap's Nominatim geocoder (no API key
    needed) - confirms a real place actually exists and returns its canonical
    address/coordinates/type, which a search-engine snippet alone won't
    reliably pin down for a specific street, building, or landmark. Returns
    None on any failure or if nothing matches."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not results:
        return None
    top = results[0]
    display_name = top.get("display_name")
    if not display_name:
        return None
    osm_type, osm_id = top.get("osm_type"), top.get("osm_id")
    url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_type and osm_id else "https://www.openstreetmap.org/"
    snippet = f"{display_name} (type: {top.get('type', 'unknown')}, lat {top.get('lat')}, lon {top.get('lon')})"
    return SearchResult(title=display_name, url=url, snippet=snippet)


def fetch_page_text(url: str, max_chars: int = 6000, timeout: float = 10.0) -> str:
    """Fetches a URL and returns plain text, truncated. Returns "" on any failure."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return ""
    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return ""
    return html_to_text(resp.text)[:max_chars]
