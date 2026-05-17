"""
RYM Poller — calls FlareSolverr to fetch RYM pages (solving Cloudflare),
parses ratings + wishlist, and posts them to Earwrym.

Modes:
  1. Full crawl (first run) — pages through entire collection until exhausted
  2. Incremental — checks /collection/{user}/recent/ for new ratings (1 request)
  3. Wishlist — checks /collection/{user}/wishlist for new items, forwards to Earwrym
"""
import json
import logging
import os
import random
import re
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rym-poller")

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://10.1.10.63:8191")
EARWRYM_URL = os.environ.get("EARWRYM_URL", "http://earwrym:8587")
RYM_USERNAME = os.environ.get("RYM_USERNAME", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "7200"))
JITTER_SECONDS = int(os.environ.get("JITTER_SECONDS", "300"))
HC_PING_URL = os.environ.get("HC_PING_URL", "")
STATE_FILE = "/data/poller_state.json"


def fetch_via_flaresolverr(url):
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    body = json.dumps(payload).encode()
    req = Request(
        f"{FLARESOLVERR_URL}/v1",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                return data.get("solution", {}).get("response", "")
            log.error("FlareSolverr error: %s — %s", data.get("status"), data.get("message", ""))
            return None
    except HTTPError as e:
        log.error("FlareSolverr HTTP error %s", e.code)
        return None
    except Exception as e:
        log.error("FlareSolverr request failed: %s", e)
        return None


def parse_ratings_from_html(html):
    ratings = []
    rows = re.findall(r'<tr id="page_catalog_item_\d+">(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        rating_m = re.search(r'(?:alt|title)="(\d+\.?\d*)\s*stars?"', row)
        artist_m = re.search(r'<a[^>]*class="artist"[^>]*>([^<]+)', row)
        title_m = re.search(r'<a[^>]*href="(/release/[^"]+)"[^>]*class="album"[^>]*>([^<]+)', row)
        if not title_m:
            title_m = re.search(r'<a[^>]*class="album"[^>]*href="(/release/[^"]+)"[^>]*>([^<]+)', row)
        if rating_m and artist_m and title_m:
            try:
                ratings.append({
                    "artist": clean_html(artist_m.group(1)),
                    "title": clean_html(title_m.group(2)),
                    "rating": float(rating_m.group(1)),
                    "rym_slug": title_m.group(1),
                })
            except (ValueError, TypeError):
                continue
    return ratings


def parse_wishlist_from_html(html):
    """Parse wishlist items — same table structure but no rating."""
    items = []
    rows = re.findall(r'<tr id="page_catalog_item_\d+">(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        artist_m = re.search(r'<a[^>]*class="artist"[^>]*>([^<]+)', row)
        title_m = re.search(r'<a[^>]*href="(/release/[^"]+)"[^>]*class="album"[^>]*>([^<]+)', row)
        if not title_m:
            title_m = re.search(r'<a[^>]*class="album"[^>]*href="(/release/[^"]+)"[^>]*>([^<]+)', row)
        if artist_m and title_m:
            items.append({
                "artist": clean_html(artist_m.group(1)),
                "title": clean_html(title_m.group(2)),
                "rym_slug": title_m.group(1),
            })
    return items


def clean_html(text):
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&\w+;', '', text)
    return text.strip()


def post_ratings_to_earwrym(ratings):
    if not ratings:
        return
    url = f"{EARWRYM_URL}/api/import-ratings"
    body = json.dumps(ratings).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            log.info("Posted %d ratings → Earwrym: %s", len(ratings), result)
    except Exception as e:
        log.error("Failed to post to Earwrym: %s", e)


def post_wishlist_to_earwrym(items):
    if not items:
        return
    url = f"{EARWRYM_URL}/api/import-wishlist"
    body = json.dumps(items).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            log.info("Posted %d wishlist items → Earwrym: %s", len(items), result)
    except Exception as e:
        log.error("Failed to post wishlist to Earwrym: %s", e)


def ping_healthcheck(suffix=""):
    if not HC_PING_URL:
        return
    try:
        req = Request(HC_PING_URL + suffix, method="POST")
        urlopen(req, timeout=10)
    except Exception:
        pass


def wait_for_flaresolverr():
    log.info("Checking FlareSolverr at %s ...", FLARESOLVERR_URL)
    for _ in range(12):
        try:
            with urlopen(f"{FLARESOLVERR_URL}/", timeout=10) as resp:
                if resp.status == 200:
                    log.info("FlareSolverr is ready")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log.error("FlareSolverr not reachable after 60 seconds")
    return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            if "known_wishlist_slugs" not in state:
                state["known_wishlist_slugs"] = []
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"full_crawl_done": False, "crawl_page": 1, "total_ratings_seen": 0, "last_wishlist_slug": "", "known_wishlist_slugs": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def crawl_ratings(state):
    """Full crawl — continues from where it left off."""
    start_page = state.get("crawl_page", 1)
    all_ratings = []

    log.info("Crawling ratings from page %d", start_page)
    pg = start_page
    while True:
        url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/r0.5-5.0/{pg}"
        log.info("Fetching page %d: %s", pg, url)

        html = fetch_via_flaresolverr(url)
        if not html:
            log.warning("No response for page %d — pausing crawl", pg)
            break

        if "Just a moment" in html or len(html) < 2000:
            log.warning("Challenge/empty on page %d — pausing crawl", pg)
            break

        ratings = parse_ratings_from_html(html)
        log.info("Page %d: %d ratings found", pg, len(ratings))

        if not ratings:
            log.info("No more ratings — full crawl complete at page %d", pg)
            state["full_crawl_done"] = True
            state["crawl_page"] = pg
            break

        all_ratings.extend(ratings)
        pg += 1

        if len(ratings) < 25:
            log.info("Last page (< 25 results) — full crawl complete")
            state["full_crawl_done"] = True
            state["crawl_page"] = pg
            break

        time.sleep(random.uniform(20, 60))

    if all_ratings:
        state["crawl_page"] = pg
        state["total_ratings_seen"] = state.get("total_ratings_seen", 0) + len(all_ratings)
        log.info("Crawl batch: %d ratings (total seen: %d)", len(all_ratings), state["total_ratings_seen"])
        post_ratings_to_earwrym(all_ratings)

    save_state(state)
    return all_ratings


def check_recent(state):
    """Incremental check — just the recent page."""
    url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/recent/"
    log.info("Checking recent ratings: %s", url)
    html = fetch_via_flaresolverr(url)
    if html and "Just a moment" not in html and len(html) > 2000:
        ratings = parse_ratings_from_html(html)
        log.info("Recent page: %d ratings found", len(ratings))
        if ratings:
            post_ratings_to_earwrym(ratings)
        return ratings
    log.warning("Could not fetch recent page")
    return []


def check_wishlist(state):
    """Check wishlist for new items not previously seen."""
    url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/wishlist,ss.dd"
    log.info("Checking wishlist: %s", url)

    time.sleep(random.uniform(5, 20))

    html = fetch_via_flaresolverr(url)
    if not html or "Just a moment" in html or len(html) < 2000:
        log.warning("Could not fetch wishlist")
        return

    items = parse_wishlist_from_html(html)
    if not items:
        log.info("No wishlist items found on page")
        return

    known = set(state.get("known_wishlist_slugs", []))
    current_slugs = [item["rym_slug"] for item in items]

    if not known:
        log.info("First wishlist check — recording %d items as baseline", len(items))
        state["known_wishlist_slugs"] = current_slugs
        save_state(state)
        return

    new_items = [item for item in items if item["rym_slug"] not in known]

    if new_items:
        log.info("Found %d new wishlist items", len(new_items))
        for item in new_items:
            log.info("  New: %s - %s", item["artist"], item["title"])
        post_wishlist_to_earwrym(new_items)

    state["known_wishlist_slugs"] = list(known | set(current_slugs))
    save_state(state)

    if not new_items:
        log.info("No new wishlist items since last check")


def run_check_cycle():
    if random.random() < 0.1:
        log.info("Skipping this cycle (random idle)")
        return

    time.sleep(random.uniform(3, 45))
    state = load_state()

    if state["full_crawl_done"]:
        check_recent(state)
        time.sleep(random.uniform(10, 30))
        check_wishlist(state)
    else:
        crawl_ratings(state)


def main():
    if not RYM_USERNAME:
        log.error("RYM_USERNAME env var is required")
        sys.exit(1)

    log.info("RYM Poller starting")
    log.info("  User: %s", RYM_USERNAME)
    log.info("  Interval: %ds (+/- %ds)", CHECK_INTERVAL, JITTER_SECONDS)
    log.info("  FlareSolverr: %s", FLARESOLVERR_URL)
    log.info("  Earwrym: %s", EARWRYM_URL)

    if not wait_for_flaresolverr():
        sys.exit(1)

    while True:
        ping_healthcheck("/start")
        try:
            run_check_cycle()
            ping_healthcheck()
        except Exception as e:
            log.error("Cycle failed: %s", e, exc_info=True)
            ping_healthcheck("/fail")

        jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS * 2)
        sleep_time = CHECK_INTERVAL + jitter
        log.info("Next check in %d seconds (~%.1f hours)", int(sleep_time), sleep_time / 3600)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
