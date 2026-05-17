import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from flask import Flask, render_template, request, jsonify, redirect, url_for

from . import db
from .config import load_config
from .tracker import EarwrymTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("earwrym")

app = Flask(__name__, template_folder="templates", static_folder="../static")
config = load_config()
tracker = None
_scheduler_running = False


def get_tracker():
    global tracker
    if tracker is None:
        tracker = EarwrymTracker(config)
    return tracker


@app.context_processor
def inject_pending_count():
    try:
        with db.get_db() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM lidarr_pending").fetchone()
            return {"pending_count": row["cnt"] if row else 0}
    except Exception:
        return {"pending_count": 0}


@app.route("/")
def dashboard():
    to_listen = db.get_albums_by_state("to-listen", limit=config.get("ui", {}).get("max_visible_per_section", 5))
    listening = db.get_albums_by_state("listening", limit=config.get("ui", {}).get("max_visible_per_section", 5))
    unrated = db.get_albums_by_state("listened-unrated", limit=config.get("ui", {}).get("max_visible_per_section", 5))

    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)

    to_listen_total = len(db.get_albums_by_state("to-listen"))
    listening_total = len(db.get_albums_by_state("listening"))
    unrated_total = len(db.get_albums_by_state("listened-unrated"))

    return render_template("dashboard.html",
                           to_listen=to_listen,
                           listening=listening,
                           unrated=unrated,
                           stats=stats,
                           year=year,
                           config=config,
                           to_listen_total=to_listen_total,
                           listening_total=listening_total,
                           unrated_total=unrated_total,
                           rym_user=config.get("rym", {}).get("username", ""))


@app.route("/listening")
def listening_full():
    albums = db.get_albums_by_state("listening")
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("listening.html", albums=albums, stats=stats, year=year,
                           config=config, total=len(albums))


@app.route("/rated")
def rated():
    page = int(request.args.get("page", 1))
    per_page = 25
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM albums WHERE state = 'rated'").fetchone()["cnt"]
        albums = conn.execute(
            "SELECT * FROM albums WHERE state = 'rated' ORDER BY rym_rated_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("rated.html", albums=albums, stats=stats, year=year,
                           page=page, total=total, per_page=per_page,
                           config=config, rym_user=config.get("rym", {}).get("username", ""))


@app.route("/stats")
def stats_page():
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)

    with db.get_db() as conn:
        rated_this_year = conn.execute(
            "SELECT * FROM albums WHERE state = 'rated' AND rym_rated_at LIKE ?",
            (f"{year}%",)
        ).fetchall()

        genre_counts = conn.execute(
            "SELECT genre_bucket, COUNT(*) as cnt FROM albums WHERE state = 'rated' "
            "AND rym_rated_at LIKE ? GROUP BY genre_bucket ORDER BY cnt DESC",
            (f"{year}%",)
        ).fetchall()

        monthly = conn.execute(
            "SELECT substr(rym_rated_at, 1, 7) as month, COUNT(*) as cnt "
            "FROM albums WHERE state = 'rated' AND rym_rated_at LIKE ? "
            "GROUP BY month ORDER BY month",
            (f"{year}%",)
        ).fetchall()

        all_time_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM albums WHERE state = 'rated'"
        ).fetchone()["cnt"]

    lb_client = get_tracker().lb
    lb_stats = lb_client.get_stats_artists(range_="this_year", count=10)
    lb_activity = lb_client.get_stats_listening_activity(range_="this_year")

    return render_template("stats.html",
                           stats=stats, year=year,
                           rated_this_year=rated_this_year,
                           genre_counts=genre_counts,
                           monthly=monthly,
                           all_time_count=all_time_count,
                           lb_top_artists=lb_stats,
                           lb_activity=lb_activity,
                           config=config)


@app.route("/playlists")
def playlists_page():
    with db.get_db() as conn:
        playlists = conn.execute("SELECT * FROM playlists ORDER BY name").fetchall()
        bucket_counts = conn.execute(
            "SELECT genre_bucket, COUNT(*) as cnt FROM albums "
            "WHERE state IN ('to-listen', 'listening') GROUP BY genre_bucket"
        ).fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("playlists.html", playlists=playlists,
                           bucket_counts=bucket_counts, config=config,
                           stats=stats, year=year)


@app.route("/unrated")
def unrated_full():
    page = int(request.args.get("page", 1))
    per_page = 25
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM albums WHERE state = 'listened-unrated'").fetchone()["cnt"]
        albums = conn.execute(
            "SELECT * FROM albums WHERE state = 'listened-unrated' ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("unrated.html", albums=albums, stats=stats, year=year,
                           page=page, total=total, per_page=per_page, config=config)


@app.route("/to-listen")
def to_listen_full():
    albums = db.get_albums_by_state("to-listen")
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    genre_buckets = [b["name"] for b in config.get("genre_buckets", [])]
    genre_counts = {}
    for album in albums:
        bucket = album["genre_bucket"] or "Other"
        genre_counts[bucket] = genre_counts.get(bucket, 0) + 1
    return render_template("to_listen.html", albums=albums, stats=stats, year=year,
                           config=config, genre_buckets=genre_buckets, genre_counts=genre_counts)


@app.route("/pending")
def pending_page():
    with db.get_db() as conn:
        items = conn.execute("SELECT * FROM lidarr_pending ORDER BY added_at DESC").fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("pending.html", items=items, stats=stats, year=year, config=config)


@app.route("/api/pending/<int:item_id>/accept", methods=["POST"])
def api_accept_pending(item_id):
    """Accept a pending Lidarr import into the to-listen queue."""
    t = get_tracker()
    with db.get_db() as conn:
        item = conn.execute("SELECT * FROM lidarr_pending WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return jsonify({"error": "not found"}), 404

        release_mbid = item["album_mbid"] or f"lidarr:{item['artist'].lower()[:10]}:{item['title'].lower()[:10]}"
        nd_album = t.is_in_library(item["artist"], item["title"])

        genre_bucket = item["genre_bucket"] or "Other"
        is_real_mbid = release_mbid and not release_mbid.startswith(("lidarr:", "nd:"))
        if is_real_mbid and genre_bucket == "Other":
            mb_meta = t.mb.get_album_metadata(release_mbid)
            if mb_meta:
                genres = mb_meta.get("genres", [])
                from .modules.musicbrainz import match_genre_bucket
                genre_bucket = match_genre_bucket(genres, config.get("genre_buckets", []))

        db.upsert_album(
            mbid=release_mbid,
            title=item["title"],
            artist=item["artist"],
            track_count=nd_album.get("songCount", 0) if nd_album else 0,
            duration_seconds=nd_album.get("duration", 0) if nd_album else 0,
            cover_art_url=item["cover_url"] or "",
            genre_bucket=genre_bucket,
            state="to-listen",
            source="lidarr",
        )
        conn.execute("DELETE FROM lidarr_pending WHERE id = ?", (item_id,))

    return jsonify({"status": "accepted"})


@app.route("/api/pending/<int:item_id>/deny", methods=["POST"])
def api_deny_pending(item_id):
    """Deny a pending Lidarr import (remove from queue)."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM lidarr_pending WHERE id = ?", (item_id,))
    return jsonify({"status": "denied"})


@app.route("/api/pending/accept-all", methods=["POST"])
def api_accept_all_pending():
    """Accept all pending items."""
    t = get_tracker()
    with db.get_db() as conn:
        items = conn.execute("SELECT * FROM lidarr_pending").fetchall()
    accepted = 0
    for item in items:
        release_mbid = item["album_mbid"] or f"lidarr:{item['artist'].lower()[:10]}:{item['title'].lower()[:10]}"
        nd_album = t.is_in_library(item["artist"], item["title"])
        db.upsert_album(
            mbid=release_mbid,
            title=item["title"],
            artist=item["artist"],
            track_count=nd_album.get("songCount", 0) if nd_album else 0,
            duration_seconds=nd_album.get("duration", 0) if nd_album else 0,
            cover_art_url=item["cover_url"] or "",
            genre_bucket=item["genre_bucket"] or "Other",
            state="to-listen",
            source="lidarr",
        )
        accepted += 1
    with db.get_db() as conn:
        conn.execute("DELETE FROM lidarr_pending")
    return jsonify({"status": "accepted", "count": accepted})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Manual refresh trigger."""
    t = get_tracker()
    threading.Thread(target=_run_rym_check, args=(t,), daemon=True).start()
    return jsonify({"status": "refresh started"})


@app.route("/api/sync-playlists", methods=["POST"])
def api_sync_playlists():
    """Trigger genre playlist sync to Navidrome."""
    t = get_tracker()
    threading.Thread(target=t.sync_genre_playlists, daemon=True).start()
    return jsonify({"status": "playlist sync started"})


@app.route("/api/cover/<album_id>")
def api_cover_proxy(album_id):
    """Proxy cover art from Navidrome for albums without CAA covers."""
    from urllib.request import Request as Req, urlopen as uopen
    from urllib.parse import urlencode
    import hashlib, random, string as string_mod
    nd_cfg = config["navidrome"]
    salt = "".join(random.choices(string_mod.ascii_lowercase + string_mod.digits, k=8))
    token = hashlib.md5((nd_cfg["password"] + salt).encode()).hexdigest()
    params = urlencode({"id": f"al-{album_id}", "size": 500, "u": nd_cfg["username"], "t": token, "s": salt, "v": "1.16.1", "c": "earwrym"})
    url = f"{nd_cfg['url'].rstrip('/')}/rest/getCoverArt?{params}"
    try:
        req = Req(url, headers={"User-Agent": "Earwrym/1.0"})
        with uopen(req, timeout=10) as resp:
            from flask import Response
            return Response(resp.read(), mimetype=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception:
        return "", 404


@app.route("/api/album/<int:album_id>/delete", methods=["POST"])
def api_delete_album(album_id):
    db.delete_album(album_id)
    return jsonify({"status": "deleted"})


@app.route("/api/album/<int:album_id>/reorder", methods=["POST"])
def api_reorder_album(album_id):
    new_pos = request.json.get("position", 0)
    db.reorder_album(album_id, new_pos)
    return jsonify({"status": "reordered"})


@app.route("/api/album/<int:album_id>/move", methods=["POST"])
def api_move_album(album_id):
    new_state = request.json.get("state")
    if new_state in ("to-listen", "listening", "listened-unrated", "rated", "dismissed"):
        db.update_album_state(album_id, new_state)
    return jsonify({"status": "moved"})


@app.route("/api/album/<int:album_id>/bucket", methods=["POST"])
def api_change_bucket(album_id):
    bucket = request.json.get("bucket", "Other")
    with db.get_db() as conn:
        conn.execute("UPDATE albums SET genre_bucket = ?, updated_at = ? WHERE id = ?",
                     (bucket, db.now_iso(), album_id))
    return jsonify({"status": "updated"})


@app.route("/api/backfill", methods=["POST"])
def api_backfill():
    """Trigger historical backfill. Accepts ?range=half_yearly (default: all_time)."""
    t = get_tracker()
    range_ = request.args.get("range", "all_time")
    threading.Thread(target=t.run_backfill, args=(range_,), daemon=True).start()
    return jsonify({"status": "backfill started", "range": range_})


@app.route("/api/backfill-navidrome", methods=["POST"])
def api_backfill_navidrome():
    """Backfill from Navidrome play history (catches albums without MBIDs)."""
    t = get_tracker()
    threading.Thread(target=t.backfill_from_navidrome, daemon=True).start()
    return jsonify({"status": "navidrome backfill started"})


@app.route("/api/backfill-genres", methods=["POST"])
def api_backfill_genres():
    """Re-fetch genres for albums stuck on Other with no tags (uses artist-level fallback)."""
    t = get_tracker()
    threading.Thread(target=t.backfill_genres, daemon=True).start()
    return jsonify({"status": "genre backfill started"})


@app.route("/api/healthcheck")
def healthcheck():
    return jsonify({"status": "ok", "timestamp": db.now_iso()})


@app.route("/api/import-ratings", methods=["POST"])
def api_import_ratings():
    """Import RYM ratings from CSV export (RYM Settings → Export).
    Accepts CSV with columns: RYM Album, First Name, Last Name, Title, Rating.
    Or JSON array of {artist, title, rating, rym_slug}."""
    import csv
    import io

    data = request.get_json(silent=True)
    if data and isinstance(data, list):
        ratings = data
    else:
        raw = request.get_data(as_text=True)
        if not raw:
            return jsonify({"error": "no data"}), 400
        reader = csv.DictReader(io.StringIO(raw))
        ratings = []
        for row in reader:
            artist = row.get("First Name", "") + " " + row.get("Last Name", "")
            artist = artist.strip() or row.get("Artist", "")
            title = row.get("Title", "")
            rating_str = row.get("Rating", "0")
            try:
                rating = float(rating_str)
            except ValueError:
                continue
            if artist and title and rating > 0:
                ratings.append({
                    "artist": artist,
                    "title": title,
                    "rating": rating,
                    "rym_slug": row.get("RYM Album", ""),
                })

    if not ratings:
        return jsonify({"error": "no valid ratings found"}), 400

    imported = 0
    matched = 0
    with db.get_db() as conn:
        for r in ratings:
            conn.execute(
                "INSERT OR REPLACE INTO rym_ratings_cache (rym_slug, artist, title, rating, scraped_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (r.get("rym_slug", ""), r["artist"], r["title"], r["rating"], db.now_iso())
            )
            imported += 1

    t = get_tracker()
    with db.get_db() as conn:
        non_rated = conn.execute(
            "SELECT * FROM albums WHERE state NOT IN ('rated', 'dismissed')"
        ).fetchall()
    for album in non_rated:
        if t._is_rated_on_rym(album["artist"], album["title"]):
            t._mark_as_rated(album["id"], album["artist"], album["title"])
            matched += 1

    return jsonify({"imported": imported, "matched_albums": matched})


@app.route("/api/import-wishlist", methods=["POST"])
def api_import_wishlist():
    """Import wishlist items from external poller. Caches them and sends new ones to Lidarr."""
    data = request.get_json(silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"error": "expected JSON array of {artist, title, rym_slug}"}), 400

    t = get_tracker()
    imported = 0
    sent_to_lidarr = 0

    for item in data:
        artist = item.get("artist", "").strip()
        title = item.get("title", "").strip()
        rym_slug = item.get("rym_slug", "").strip()
        if not artist or not title:
            continue

        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM rym_wishlist_cache WHERE rym_slug = ?", (rym_slug,)
            ).fetchone()
            if existing:
                continue

            conn.execute(
                "INSERT INTO rym_wishlist_cache (rym_slug, artist, title, added_to_cache_at) "
                "VALUES (?, ?, ?, ?)",
                (rym_slug, artist, title, db.now_iso())
            )
            imported += 1

        if not t.lidarr:
            continue

        if t.lidarr.is_album_in_library(artist, title):
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE rym_wishlist_cache SET sent_to_lidarr = 1 WHERE rym_slug = ?",
                    (rym_slug,)
                )
                existing_album = conn.execute(
                    "SELECT id FROM albums WHERE LOWER(artist) = ? AND LOWER(title) = ?",
                    (artist.lower(), title.lower())
                ).fetchone()
                if not existing_album:
                    db.upsert_album(
                        mbid=f"wishlist:{rym_slug}",
                        title=title, artist=artist,
                        state="to-listen", source="wishlist",
                    )
                    log.info("Wishlist already in Lidarr, added to to-listen: %s - %s", artist, title)
            continue

        results = t.lidarr.search_album(f"{artist} {title}")
        if results:
            top = results[0]
            artist_data = None
            if "artist" in top:
                artist_data = top["artist"]
            album_mbid = top.get("foreignAlbumId", "")

            if artist_data and album_mbid:
                if not t.lidarr.is_artist_in_library(artist):
                    t.lidarr.add_artist_with_album(artist_data, album_mbid)
                    log.info("Wishlist → Lidarr: %s - %s", artist, title)
                    sent_to_lidarr += 1
                else:
                    albums = t.lidarr._request("GET", f"album?artistId={artist_data.get('id', 0)}") or []
                    for a in albums:
                        if a.get("foreignAlbumId") == album_mbid:
                            a["monitored"] = True
                            t.lidarr._request("PUT", f"album/{a['id']}", a)
                            t.lidarr._request("POST", "command", {
                                "name": "AlbumSearch", "albumIds": [a["id"]]
                            })
                            sent_to_lidarr += 1
                            break

            with db.get_db() as conn:
                conn.execute(
                    "UPDATE rym_wishlist_cache SET sent_to_lidarr = 1 WHERE rym_slug = ?",
                    (rym_slug,)
                )

    log.info("Wishlist import: %d new items, %d sent to Lidarr", imported, sent_to_lidarr)
    return jsonify({"imported": imported, "sent_to_lidarr": sent_to_lidarr})


def _run_rym_check(t):
    try:
        t.check_rym_ratings()
    except Exception as e:
        log.error("RYM refresh failed: %s", e)


def _ping_healthcheck(suffix=""):
    """Ping Healthchecks dead-man's switch."""
    hc_url = config.get("healthchecks", {}).get("ping_url")
    if not hc_url:
        return
    try:
        req = Request(hc_url + suffix, method="POST")
        urlopen(req, timeout=10)
    except Exception:
        pass


def _scheduler_loop():
    """Background scheduler for periodic tasks."""
    global _scheduler_running
    _scheduler_running = True
    t = get_tracker()
    poll_interval = config.get("poll_interval_seconds", 300)
    rym_ratings_interval = config.get("rym", {}).get("ratings_interval_seconds", 7200)
    rym_wishlist_interval = config.get("rym", {}).get("wishlist_interval_seconds", 21600)
    gen1001_interval = 86400

    last_poll = 0
    last_rym_ratings = 0
    last_rym_wishlist = 0
    last_1001 = 0
    last_lidarr = 0
    last_playlist_sync = 0
    last_genre_backfill = 0
    lidarr_interval = config.get("lidarr", {}).get("check_interval_seconds", 1800)
    playlist_sync_interval = config.get("playlist_sync_interval_seconds", 3600)
    genre_backfill_interval = 86400

    while _scheduler_running:
        now = time.time()
        try:
            if now - last_poll >= poll_interval:
                t.poll_listens()
                last_poll = now
                _ping_healthcheck()

            if now - last_rym_ratings >= rym_ratings_interval:
                t.match_cached_ratings()
                last_rym_ratings = now

            if t.rym and now - last_rym_wishlist >= rym_wishlist_interval:
                t.check_rym_wishlist()
                last_rym_wishlist = now

            if t.gen1001 and now - last_1001 >= gen1001_interval:
                t.check_1001_albums()
                last_1001 = now

            if t.lidarr and now - last_lidarr >= lidarr_interval:
                t.check_lidarr_imports()
                last_lidarr = now

            if now - last_playlist_sync >= playlist_sync_interval:
                t.sync_genre_playlists()
                last_playlist_sync = now

            if now - last_genre_backfill >= genre_backfill_interval:
                t.backfill_genres()
                last_genre_backfill = now

        except Exception as e:
            log.error("Scheduler error: %s", e, exc_info=True)
            _ping_healthcheck("/fail")

        time.sleep(30)


def start_app():
    db.init_db()
    db.get_year_stats(datetime.now(timezone.utc).year)

    scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("EARWRYM_PORT", "8587"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    start_app()
