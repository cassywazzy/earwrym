import logging
import random
import re
import time

log = logging.getLogger(__name__)

BASE_URL = "https://rateyourmusic.com"


class RYMClient:
    def __init__(self, username, proxy_url=None):
        self.username = username
        self.proxy_url = proxy_url
        self._last_request = 0
        self._min_delay = 8.0
        self._max_delay = 15.0
        self._session = None

    def _get_session(self):
        if self._session is not None:
            return self._session
        try:
            from curl_cffi.requests import Session
            self._session = Session(
                impersonate="chrome124",
                proxies={"https": self.proxy_url, "http": self.proxy_url} if self.proxy_url else None,
            )
            return self._session
        except ImportError:
            log.warning("curl_cffi not available, falling back to urllib (will likely 403)")
            return None

    def _get(self, path):
        delay = random.uniform(self._min_delay, self._max_delay)
        elapsed = time.time() - self._last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)

        url = f"{BASE_URL}{path}"
        self._last_request = time.time()

        session = self._get_session()
        if session:
            return self._get_curl_cffi(session, url)
        return self._get_urllib(url)

    def _get_curl_cffi(self, session, url):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code == 403:
                log.warning("RYM 403 via curl_cffi (Cloudflare challenge): %s", url)
                return None
            else:
                log.error("RYM HTTP %s: %s", resp.status_code, url)
                return None
        except Exception as e:
            log.error("RYM request failed: %s — %s", url, e)
            return None

    def _get_urllib(self, url):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            log.error("RYM fetch error %s: %s", e.code, url)
            return None

    def scrape_ratings(self, page=1):
        """Scrape user's ratings page. Returns list of {artist, title, rating, rym_slug}."""
        html = self._get(f"/collection/{self.username}/r0.0-5.0/{page}")
        if not html:
            return []
        return _parse_ratings_page(html)

    def scrape_all_ratings(self):
        """Scrape all ratings pages."""
        all_ratings = []
        page = 1
        while True:
            ratings = self.scrape_ratings(page)
            if not ratings:
                break
            all_ratings.extend(ratings)
            page += 1
            if len(ratings) < 25:
                break
        return all_ratings

    def scrape_wishlist(self, page=1):
        """Scrape user's wishlist. Returns list of {artist, title, rym_slug}."""
        html = self._get(f"/collection/{self.username}/wishlist/{page}")
        if not html:
            return []
        return _parse_wishlist_page(html)

    def scrape_all_wishlist(self):
        """Scrape all wishlist pages."""
        all_items = []
        page = 1
        while True:
            items = self.scrape_wishlist(page)
            if not items:
                break
            all_items.extend(items)
            page += 1
            if len(items) < 25:
                break
        return all_items


def _parse_ratings_page(html):
    """Parse ratings from RYM collection page HTML."""
    ratings = []
    album_pattern = re.compile(
        r'class="or_q_albumartist_td"[^>]*>.*?<a[^>]*>([^<]+)</a>.*?'
        r'class="or_q_albumtitle_td"[^>]*>.*?<a[^>]*href="(/release/[^"]+)"[^>]*>([^<]+)</a>.*?'
        r'class="or_q_rating_td[^"]*"[^>]*>.*?(\d\.?\d*)',
        re.DOTALL
    )
    for match in album_pattern.finditer(html):
        artist, slug, title, rating = match.groups()
        ratings.append({
            "artist": _clean_html(artist),
            "title": _clean_html(title),
            "rating": float(rating),
            "rym_slug": slug,
        })

    if not ratings:
        ratings = _parse_ratings_fallback(html)

    return ratings


def _parse_ratings_fallback(html):
    """Fallback parser using simpler patterns."""
    ratings = []
    rows = re.findall(r'<tr[^>]*class="[^"]*or_q_[^"]*"[^>]*>(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        artist_m = re.search(r'class="or_q_albumartist_td"[^>]*>(.*?)</td>', row, re.DOTALL)
        title_m = re.search(r'class="or_q_albumtitle_td"[^>]*>(.*?)</td>', row, re.DOTALL)
        rating_m = re.search(r'(\d\.\d{2})\s*</span>', row)

        if artist_m and title_m:
            artist_text = re.sub(r'<[^>]+>', '', artist_m.group(1)).strip()
            title_link = re.search(r'href="(/release/[^"]+)"[^>]*>([^<]+)', title_m.group(1))
            if title_link:
                slug = title_link.group(1)
                title_text = _clean_html(title_link.group(2))
            else:
                slug = ""
                title_text = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()

            rating = float(rating_m.group(1)) if rating_m else None
            if artist_text and title_text:
                ratings.append({
                    "artist": artist_text,
                    "title": title_text,
                    "rating": rating,
                    "rym_slug": slug,
                })
    return ratings


def _parse_wishlist_page(html):
    """Parse wishlist entries from RYM collection/wishlist page."""
    items = []
    link_pattern = re.compile(
        r'<a[^>]*href="(/release/[^"]+)"[^>]*title="([^"]*)"[^>]*>',
        re.DOTALL
    )
    for match in link_pattern.finditer(html):
        slug, title = match.groups()
        artist_m = re.search(
            r'class="or_q_albumartist_td"[^>]*>.*?<a[^>]*>([^<]+)</a>',
            html[max(0, match.start()-500):match.start()],
            re.DOTALL
        )
        artist = _clean_html(artist_m.group(1)) if artist_m else ""
        items.append({
            "artist": artist,
            "title": _clean_html(title),
            "rym_slug": slug,
        })
    return items


def _clean_html(text):
    """Remove HTML entities and extra whitespace."""
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&\w+;', '', text)
    return text.strip()
