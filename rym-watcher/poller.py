"""
RYM Poller — uses Pydoll (Chrome CDP) to fetch RYM pages past Cloudflare,
parses ratings + wishlist, and posts them to Earwrym.

Modes:
  1. Full crawl (first run) — pages through entire collection until exhausted
  2. Incremental — checks /collection/{user}/recent/ for new ratings (1 request)
  3. Wishlist — checks /collection/{user}/wishlist for new items, forwards to Earwrym
"""
import asyncio
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rym-poller")

EARWRYM_URL = os.environ.get("EARWRYM_URL", "http://earwrym:8587")
RYM_USERNAME = os.environ.get("RYM_USERNAME", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "7200"))
JITTER_SECONDS = int(os.environ.get("JITTER_SECONDS", "300"))
HC_PING_URL = os.environ.get("HC_PING_URL", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))
STATE_FILE = "/data/poller_state.json"
PROFILE_DIR = "/data/chrome_profile"


def _kill_chrome():
    """Kill any lingering Chrome processes."""
    try:
        subprocess.run(["pkill", "-9", "-f", "google-chrome"], capture_output=True)
    except Exception:
        pass


def _make_chrome_options():
    from pydoll.browser.options import ChromiumOptions
    opts = ChromiumOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    return opts


async def _check_page(tab, debug=False):
    """Check if page is past Cloudflare. Returns html or None."""
    html = await tab.page_source
    if debug and html:
        log.info("Page check: len=%d, has_challenge=%s, title=%s",
                 len(html), "Just a moment" in html,
                 re.search(r'<title>([^<]+)</title>', html[:1000], re.I))
    if html and "Just a moment" not in html and len(html) > 2000:
        return html
    return None


async def fetch_page(tab, url):
    """Navigate to a URL, handle Cloudflare, return page source."""
    try:
        await tab.go_to(url)
        await asyncio.sleep(random.uniform(4.0, 7.0))

        html = await _check_page(tab, debug=True)
        if html:
            log.info("Page loaded (no challenge): %s (%d bytes)", url[:80], len(html))
            return html

        log.info("Challenge detected for %s, attempting bypass...", url[:80])

        try:
            async with tab.expect_and_bypass_cloudflare_captcha(
                time_to_wait_captcha=20
            ):
                await tab.go_to(url)

            await asyncio.sleep(random.uniform(5.0, 10.0))
            html = await _check_page(tab)
            if html:
                log.info("Page loaded (after bypass): %s (%d bytes)", url[:80], len(html))
                return html
        except Exception as e:
            log.warning("Bypass method failed: %s", e)

        for wait in [10, 15, 20]:
            log.info("Waiting %ds for challenge to auto-resolve...", wait)
            await asyncio.sleep(wait)
            html = await _check_page(tab, debug=True)
            if html:
                log.info("Page loaded (auto-resolved after %ds): %s (%d bytes)", wait, url[:80], len(html))
                return html

        log.warning("Could not get past Cloudflare for %s", url[:80])
        return None
    except asyncio.TimeoutError:
        log.error("Timeout fetching %s", url[:80])
        return None
    except Exception as e:
        log.error("Error fetching %s: %s", url[:80], e)
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


async def crawl_ratings(tab, state):
    start_page = state.get("crawl_page", 1)
    all_ratings = []

    log.info("Crawling ratings from page %d", start_page)
    pg = start_page
    while True:
        url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/r0.5-5.0/{pg}"
        log.info("Fetching page %d: %s", pg, url)

        html = await fetch_page(tab, url)
        if not html:
            log.warning("No response for page %d — pausing crawl", pg)
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

        if pg - start_page >= MAX_PAGES:
            log.info("Hit max pages (%d) for this cycle", MAX_PAGES)
            state["crawl_page"] = pg
            break

        await asyncio.sleep(random.uniform(20, 60))

    if all_ratings:
        state["crawl_page"] = pg
        state["total_ratings_seen"] = state.get("total_ratings_seen", 0) + len(all_ratings)
        log.info("Crawl batch: %d ratings (total seen: %d)", len(all_ratings), state["total_ratings_seen"])
        post_ratings_to_earwrym(all_ratings)

    save_state(state)
    return all_ratings


async def check_recent(tab, state):
    url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/recent/"
    log.info("Checking recent ratings: %s", url)
    html = await fetch_page(tab, url)
    if html:
        ratings = parse_ratings_from_html(html)
        log.info("Recent page: %d ratings found", len(ratings))
        if ratings:
            post_ratings_to_earwrym(ratings)
        return ratings
    log.warning("Could not fetch recent page")
    return []


async def check_wishlist(tab, state):
    url = f"https://rateyourmusic.com/collection/{RYM_USERNAME}/wishlist,ss.dd"
    log.info("Checking wishlist: %s", url)

    await asyncio.sleep(random.uniform(5, 20))

    html = await fetch_page(tab, url)
    if not html:
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


async def run_check_cycle():
    if random.random() < 0.1:
        log.info("Skipping this cycle (random idle)")
        return

    await asyncio.sleep(random.uniform(3, 45))

    _kill_chrome()
    await asyncio.sleep(1)

    from pydoll.browser import Chrome
    log.info("Starting Chrome for this cycle...")
    try:
        async with Chrome(options=_make_chrome_options()) as browser:
            tab = await browser.start()
            log.info("Chrome ready, warming up with homepage...")

            warmup = await fetch_page(tab, "https://rateyourmusic.com/")
            if warmup:
                log.info("Homepage warmup succeeded (%d bytes)", len(warmup))
            else:
                log.warning("Homepage warmup failed — continuing anyway")
            await asyncio.sleep(random.uniform(5, 15))

            state = load_state()

            if state["full_crawl_done"]:
                recent = await check_recent(tab, state)
                if not recent:
                    log.info("Retrying recent page...")
                    await asyncio.sleep(random.uniform(10, 20))
                    await check_recent(tab, state)
                await asyncio.sleep(random.uniform(10, 30))
                await check_wishlist(tab, state)
            else:
                await crawl_ratings(tab, state)

            log.info("Scrape cycle complete, closing Chrome")
    except Exception as e:
        log.error("Chrome cycle failed: %s", e, exc_info=True)
    finally:
        _kill_chrome()


async def async_main():
    if not RYM_USERNAME:
        log.error("RYM_USERNAME env var is required")
        sys.exit(1)

    log.info("RYM Poller starting (Pydoll/Chrome)")
    log.info("  User: %s", RYM_USERNAME)
    log.info("  Interval: %ds (+/- %ds)", CHECK_INTERVAL, JITTER_SECONDS)
    log.info("  Earwrym: %s", EARWRYM_URL)
    log.info("  Profile: %s", PROFILE_DIR)

    while True:
        ping_healthcheck("/start")
        try:
            await run_check_cycle()
            ping_healthcheck()
        except Exception as e:
            log.error("Cycle failed: %s", e, exc_info=True)
            ping_healthcheck("/fail")
            _kill_chrome()

        jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS * 2)
        sleep_time = CHECK_INTERVAL + jitter
        log.info("Next check in %d seconds (~%.1f hours)", int(sleep_time), sleep_time / 3600)
        await asyncio.sleep(sleep_time)


def main():
    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        log.info("Shutting down (signal %s)...", sig)
        _kill_chrome()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        loop.run_until_complete(async_main())
    except KeyboardInterrupt:
        _kill_chrome()


if __name__ == "__main__":
    main()
