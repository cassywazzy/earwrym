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

import re as _re
import unicodedata as _ud


def _rym_slugify(text):
    for src, dst in [("Æ", "AE"), ("æ", "ae"), ("Ø", "O"), ("ø", "o"), ("ß", "ss"), ("Œ", "OE"), ("œ", "oe")]:
        text = text.replace(src, dst)
    text = _ud.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = text.replace("&", "and").replace("/", "_")
    text = _re.sub(r"[''`]", "", text)
    text = _re.sub(r"[^\w\s-]", "", text)
    text = _re.sub(r"[\s]+", "-", text).strip("-")
    return text


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("earwrym")

app = Flask(__name__, template_folder="templates", static_folder="../static")
app.jinja_env.filters["rym_slug"] = _rym_slugify
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
        all_time_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM albums WHERE state = 'rated'"
        ).fetchone()["cnt"]

        total_albums = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]

        state_counts = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM albums GROUP BY state ORDER BY cnt DESC"
        ).fetchall()

        genre_counts = conn.execute(
            "SELECT genre_bucket, COUNT(*) as cnt FROM albums "
            "WHERE state IN ('rated', 'listening', 'listened-unrated') "
            "GROUP BY genre_bucket ORDER BY cnt DESC"
        ).fetchall()

        genre_ratings = conn.execute(
            "SELECT genre_bucket, AVG(rym_rating) as avg_r, COUNT(*) as cnt "
            "FROM albums WHERE rym_rating IS NOT NULL "
            "GROUP BY genre_bucket HAVING cnt >= 2 ORDER BY avg_r DESC"
        ).fetchall()

        rating_dist = conn.execute(
            "SELECT CAST(rym_rating * 2 AS INT) / 2.0 AS bucket, COUNT(*) as cnt "
            "FROM albums WHERE rym_rating IS NOT NULL GROUP BY bucket ORDER BY bucket"
        ).fetchall()

        rym_rating_dist = conn.execute(
            "SELECT CAST(rating * 2 AS INT) / 2.0 AS bucket, COUNT(*) as cnt "
            "FROM rym_ratings_cache GROUP BY bucket ORDER BY bucket"
        ).fetchall()

        avg_rating = conn.execute(
            "SELECT AVG(rym_rating) FROM albums WHERE rym_rating IS NOT NULL"
        ).fetchone()[0]

        rym_total = conn.execute("SELECT COUNT(*) FROM rym_ratings_cache").fetchone()[0]
        rym_avg = conn.execute("SELECT AVG(rating) FROM rym_ratings_cache").fetchone()[0]

        decade_dist = conn.execute(
            "SELECT (year / 10) * 10 as decade, COUNT(*) as cnt "
            "FROM albums WHERE year IS NOT NULL GROUP BY decade ORDER BY decade"
        ).fetchall()

        rym_decade_dist = conn.execute(
            "SELECT (CAST(substr(rym_slug, -2, 2) AS INT)) as x FROM rym_ratings_cache LIMIT 0"
        ).fetchall()

        top_artists = conn.execute(
            "SELECT artist, COUNT(*) as cnt, AVG(rym_rating) as avg_r "
            "FROM albums WHERE state IN ('rated', 'listening', 'listened-unrated') "
            "GROUP BY artist ORDER BY cnt DESC LIMIT 15"
        ).fetchall()

        highest_rated = conn.execute(
            "SELECT artist, title, rym_rating, cover_art_url FROM albums "
            "WHERE rym_rating IS NOT NULL ORDER BY rym_rating DESC, title LIMIT 10"
        ).fetchall()

        dismissed_by_genre = conn.execute(
            "SELECT genre_bucket, COUNT(*) as cnt FROM albums "
            "WHERE state = 'dismissed' GROUP BY genre_bucket ORDER BY cnt DESC"
        ).fetchall()

        disco_leaders = conn.execute(
            "SELECT name, completed_releases, total_releases, "
            "CAST(completed_releases AS REAL) / total_releases as pct "
            "FROM discography_artists WHERE total_releases > 0 "
            "ORDER BY pct DESC, completed_releases DESC"
        ).fetchall()

        pipeline = {}
        for row in state_counts:
            pipeline[row["state"]] = row["cnt"]

    lb_client = get_tracker().lb
    lb_stats = lb_client.get_stats_artists(range_="this_year", count=10)
    lb_activity = lb_client.get_stats_listening_activity(range_="this_year")
    lb_listen_count = lb_client.get_listen_count()

    return render_template("stats.html",
                           stats=stats, year=year,
                           all_time_count=all_time_count,
                           total_albums=total_albums,
                           state_counts=state_counts,
                           genre_counts=genre_counts,
                           genre_ratings=genre_ratings,
                           rating_dist=rating_dist,
                           rym_rating_dist=rym_rating_dist,
                           avg_rating=avg_rating,
                           rym_total=rym_total,
                           rym_avg=rym_avg,
                           decade_dist=decade_dist,
                           top_artists=top_artists,
                           highest_rated=highest_rated,
                           dismissed_by_genre=dismissed_by_genre,
                           disco_leaders=disco_leaders,
                           pipeline=pipeline,
                           lb_top_artists=lb_stats,
                           lb_activity=lb_activity,
                           lb_listen_count=lb_listen_count,
                           config=config)


@app.route("/playlists")
def playlists_page():
    with db.get_db() as conn:
        playlists = conn.execute("SELECT * FROM playlists ORDER BY name").fetchall()
        bucket_counts = conn.execute(
            "SELECT genre_bucket, COUNT(*) as cnt FROM albums "
            "WHERE state IN ('to-listen', 'listening') GROUP BY genre_bucket"
        ).fetchall()
        bucket_albums = {}
        for bc in bucket_counts:
            albums = conn.execute(
                "SELECT id, title, artist, state, cover_art_url FROM albums "
                "WHERE genre_bucket = ? AND state IN ('to-listen', 'listening') "
                "ORDER BY state, sort_order LIMIT 20",
                (bc["genre_bucket"],)
            ).fetchall()
            bucket_albums[bc["genre_bucket"]] = albums
    nd_url = config.get("navidrome", {}).get("url", "")
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("playlists.html", playlists=playlists,
                           bucket_counts=bucket_counts, bucket_albums=bucket_albums,
                           nd_url=nd_url, config=config, stats=stats, year=year)


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

    nd_url = config.get("navidrome", {}).get("url", "").rstrip("/")
    jf_url = config.get("jellyfin", {}).get("url", "").rstrip("/")

    return render_template("to_listen.html", albums=albums, stats=stats, year=year,
                           config=config, genre_buckets=genre_buckets, genre_counts=genre_counts,
                           navidrome_url=nd_url, jellyfin_url=jf_url)


@app.route("/settings")
def settings_page():
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    cfg = {
        "listenbrainz": config.get("listenbrainz", {}),
        "rym": config.get("rym", {}),
        "navidrome": config.get("navidrome", {}),
        "jellyfin": config.get("jellyfin", {}),
        "lidarr": config.get("lidarr", {}),
        "one_thousand_one_albums": config.get("one_thousand_one_albums", {}),
        "ollama": config.get("ollama", {}),
        "lastfm": config.get("lastfm", {}),
        "essentia": config.get("essentia", {}),
        "qbittorrent": config.get("qbittorrent", {}),
        "healthchecks": config.get("healthchecks", {}),
        "ui": config.get("ui", {}),
        "album_filter": config.get("album_filter", {}),
        "poll_interval_seconds": config.get("poll_interval_seconds", 300),
    }
    return render_template("settings.html", cfg=cfg, stats=stats, year=year)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Save settings to config.yaml."""
    import yaml
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "no data"}), 400

    config_path = os.environ.get("EARWRYM_CONFIG", "/data/config.yaml")

    for key, value in data.items():
        parts = key.split(".", 1)
        if len(parts) == 2:
            section, field = parts
            config.setdefault(section, {})[field] = value
        else:
            config[key] = value

    try:
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/<int:album_id>/request-download", methods=["POST"])
def api_request_download(album_id):
    """Request an album download via Lidarr, or link to Navidrome if already there."""
    t = get_tracker()
    with db.get_db() as conn:
        album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not album:
        return jsonify({"error": "Album not found"}), 404

    nd_album = t.is_in_library(album["artist"], album["title"])
    if nd_album:
        nd_id = nd_album.get("id", "")
        with db.get_db() as conn:
            conn.execute("UPDATE albums SET navidrome_id = ?, state = 'listening' WHERE id = ?", (nd_id, album_id))
        return jsonify({"status": "already_downloaded", "navidrome_id": nd_id})

    if not t.lidarr:
        return jsonify({"error": "Lidarr not configured"}), 400
    success = t._request_via_lidarr(album["artist"], album["title"], source="Manual request")
    if success:
        return jsonify({"status": "requested", "artist": album["artist"], "title": album["title"]})
    return jsonify({"status": "not_found", "message": "Album not found in Lidarr search"}), 404


@app.route("/api/webhook/lidarr", methods=["POST"])
def webhook_lidarr():
    """Receive real-time events from Lidarr (grab, import, failure)."""
    t = get_tracker()
    payload = request.get_json(silent=True) or {}
    event_type = payload.get("eventType", "")
    artist_name = payload.get("artist", {}).get("name", "")
    albums = payload.get("albums", [])
    log.info("Lidarr webhook received: %s — %s (%d albums)", event_type, artist_name, len(albums))
    for album in albums:
        t.handle_lidarr_event(
            event_type,
            artist_name,
            album.get("title", ""),
            album_mbid=album.get("mbId", ""),
        )
    return jsonify({"status": "ok"})


@app.route("/api/download-queue")
def api_download_queue():
    t = get_tracker()
    lookup = {}
    if t.qbt:
        torrents = t.qbt.get_music_torrents()
        with db.get_db() as conn:
            unlinked = conn.execute(
                "SELECT id, artist, title FROM albums "
                "WHERE (navidrome_id IS NULL OR navidrome_id = '') "
                "AND state IN ('to-listen', 'listening')"
            ).fetchall()
        for album in unlinked:
            match = t.qbt.match_album(album["artist"], album["title"], torrents)
            if match:
                state = match["state"]
                if state in ("downloading", "stalledDL", "metaDL", "queuedDL", "forcedDL", "checkingDL"):
                    status = "downloading"
                elif state in ("uploading", "stalledUP", "stoppedUP", "forcedUP", "queuedUP", "checkingUP"):
                    status = "completed"
                elif state == "pausedDL":
                    status = "paused"
                else:
                    status = state
                eta = match["eta"]
                timeleft = f"{eta // 3600}h{(eta % 3600) // 60}m" if eta and eta < 8640000 else ""
                key = f"{album['artist'].lower()}|{album['title'].lower()}"
                lookup[key] = {
                    "status": status,
                    "progress": match["progress"],
                    "dlspeed": match["dlspeed"],
                    "seeds": match["seeds"],
                    "timeleft": timeleft,
                }
    elif t.lidarr:
        queue = t.lidarr.get_queue()
        for item in queue:
            key = f"{item['artist'].lower()}|{item['title'].lower()}"
            lookup[key] = {"status": item["status"], "progress": item["progress"], "timeleft": item["timeleft"]}
    return jsonify(lookup)


@app.route("/pending")
def pending_page():
    with db.get_db() as conn:
        items = conn.execute("SELECT * FROM lidarr_pending ORDER BY added_at DESC").fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("pending.html", items=items, stats=stats, year=year, config=config)


@app.route("/discography")
def discography_page():
    t = get_tracker()
    artists = t.get_discography_artists()
    suggestions = t.get_discography_suggestions()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("discography.html", artists=artists, suggestions=suggestions,
                           stats=stats, year=year, config=config)


@app.route("/discography/<int:artist_id>")
def discography_detail_page(artist_id):
    t = get_tracker()
    artist, releases = t.get_discography_detail(artist_id)
    if not artist:
        return "Artist not found", 404
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("discography_detail.html", artist=artist, releases=releases,
                           stats=stats, year=year, config=config)


@app.route("/album/<int:album_id>")
def album_detail(album_id):
    t = get_tracker()
    with db.get_db() as conn:
        album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album:
            return "Album not found", 404

        diary = conn.execute(
            "SELECT * FROM diary_entries WHERE album_id = ? ORDER BY listened_date DESC",
            (album_id,)
        ).fetchall()

        listens = conn.execute(
            "SELECT listened_at FROM listens WHERE album_id = ? ORDER BY listened_at DESC LIMIT 20",
            (album_id,)
        ).fetchall()

        disco_release = conn.execute(
            "SELECT dr.*, da.name as artist_name, da.id as da_id "
            "FROM discography_releases dr JOIN discography_artists da ON dr.artist_id = da.id "
            "WHERE dr.album_id = ?", (album_id,)
        ).fetchone()

        rym_entry = conn.execute(
            "SELECT * FROM rym_ratings_cache WHERE LOWER(artist) = ? AND LOWER(title) = ?",
            (album["artist"].lower(), album["title"].lower())
        ).fetchone()

    nd_url = config.get("navidrome", {}).get("url", "")
    nd_link = f"{nd_url}/app/#/album/{album['navidrome_id']}/show" if album["navidrome_id"] and nd_url else None

    jf_cfg = config.get("jellyfin", {})
    jf_link = None
    if jf_cfg.get("enabled") and jf_cfg.get("url"):
        jf_album = t.jellyfin.search_album(album["artist"], album["title"]) if t.jellyfin else None
        if jf_album:
            jf_link = f"{jf_cfg['url']}/web/index.html#!/details?id={jf_album['Id']}"

    rym_link = None
    rym_search = f"https://rateyourmusic.com/search?searchterm={album['artist']} {album['title']}&searchtype=l"
    if rym_entry and rym_entry["rym_slug"]:
        rym_link = f"https://rateyourmusic.com{rym_entry['rym_slug']}"
    else:
        rym_link = f"https://rateyourmusic.com/release/album/{_rym_slugify(album['artist'])}/{_rym_slugify(album['title'])}/"

    import re
    mbid = album["mbid"] or ""
    mb_link = None
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', mbid):
        mb_link = f"https://musicbrainz.org/release-group/{mbid}"

    ota_uuid = None
    ota_rating = None
    if album["source"] == "1001albums" and (album["mbid"] or "").startswith("1001-"):
        ota_uuid = album["mbid"][5:]
        if t.gen1001:
            project = t.gen1001.get_project()
            if project:
                for h in project.get("history", []):
                    if h.get("album", {}).get("uuid") == ota_uuid:
                        r = h.get("rating")
                        if r and r != "did-not-listen":
                            ota_rating = int(r)
                        break

    dl_status = None
    if not album["navidrome_id"]:
        if t.qbt:
            match = t.qbt.match_album(album["artist"], album["title"])
            if match:
                state = match["state"]
                if state in ("downloading", "stalledDL", "metaDL", "queuedDL", "forcedDL", "checkingDL"):
                    status = "downloading"
                elif state in ("uploading", "stalledUP", "stoppedUP", "forcedUP", "queuedUP", "checkingUP"):
                    status = "completed"
                else:
                    status = state
                eta = match["eta"]
                dl_status = {
                    "status": status,
                    "progress": match["progress"],
                    "dlspeed": match["dlspeed"],
                    "seeds": match["seeds"],
                    "timeleft": f"{eta // 3600}h{(eta % 3600) // 60}m" if eta and eta < 8640000 else "",
                }
        elif t.lidarr:
            queue = t.lidarr.get_queue()
            for qi in queue:
                if (album["artist"].lower() in qi["artist"].lower() or qi["artist"].lower() in album["artist"].lower()) and \
                   (album["title"].lower() in qi["title"].lower() or qi["title"].lower() in album["title"].lower()):
                    dl_status = qi
                    break

    tracks = []
    if album["navidrome_id"]:
        try:
            songs = t.nd.get_album_songs(album["navidrome_id"])
            for s in songs:
                dur = s.get("duration", 0)
                tracks.append({
                    "number": s.get("track", 0),
                    "disc": s.get("discNumber", 1),
                    "title": s.get("title", ""),
                    "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "",
                    "duration_seconds": dur,
                })
        except Exception:
            pass

    listen_dates = {}
    for l in listens:
        d = (l["listened_at"] or "")[:10]
        if d:
            listen_dates[d] = listen_dates.get(d, 0) + 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("album_detail.html", album=album, diary=diary,
                           listen_dates=listen_dates, tracks=tracks,
                           disco_release=disco_release, rym_entry=rym_entry,
                           nd_link=nd_link, jf_link=jf_link,
                           rym_link=rym_link, rym_search=rym_search, mb_link=mb_link,
                           ota_uuid=ota_uuid, ota_rating=ota_rating,
                           dl_status=dl_status,
                           today=today, stats=stats, year=year, config=config)


@app.route("/diary")
def diary_page():
    page = int(request.args.get("page", 1))
    per_page = 30
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0]
        entries = conn.execute(
            "SELECT d.*, a.title, a.artist, a.cover_art_url, a.rym_rating, a.genre_bucket, a.year as album_year "
            "FROM diary_entries d JOIN albums a ON d.album_id = a.id "
            "ORDER BY d.listened_date DESC, d.created_at DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("diary.html", entries=entries, stats=stats, year=year,
                           page=page, total=total, per_page=per_page, config=config)


@app.route("/search")
def search_page():
    q = request.args.get("q", "").strip()
    results = []
    if q:
        with db.get_db() as conn:
            results = conn.execute(
                "SELECT * FROM albums WHERE title LIKE ? OR artist LIKE ? "
                "ORDER BY CASE state WHEN 'rated' THEN 0 WHEN 'listening' THEN 1 "
                "WHEN 'listened-unrated' THEN 2 WHEN 'to-listen' THEN 3 ELSE 4 END, title "
                "LIMIT 50",
                (f"%{q}%", f"%{q}%")
            ).fetchall()
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("search.html", q=q, results=results, stats=stats, year=year, config=config)


@app.route("/1001")
def group_1001_page():
    t = get_tracker()
    group_data = t.gen1001.get_group() if t.gen1001 else None
    members_history = {}
    member_stats = {}
    if group_data:
        from .modules.one001albums import BASE_URL
        for member in group_data.get("members", []):
            name = member.get("name", "")
            proj = t.gen1001._get(f"{BASE_URL}/projects/{name}")
            if proj:
                hist = proj.get("history", [])
                members_history[name] = hist[:20]
                rated = [h for h in hist if h.get("rating") and h["rating"] != "did-not-listen"]
                member_stats[name] = {
                    "total": len(hist),
                    "rated": len(rated),
                    "avg": round(sum(h["rating"] for h in rated) / len(rated), 2) if rated else 0,
                    "current": proj.get("currentAlbum"),
                }
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("group_1001.html", group=group_data, members_history=members_history,
                           member_stats=member_stats, stats=stats, year=year, config=config)


@app.route("/essentia")
def essentia_page():
    with db.get_db() as conn:
        total_albums = conn.execute(
            "SELECT COUNT(*) FROM albums WHERE state NOT IN ('dismissed')"
        ).fetchone()[0]
        analyzed = conn.execute(
            "SELECT COUNT(DISTINCT album_id) FROM album_tags WHERE source = 'essentia'"
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT at.album_id, a.title, a.artist, a.cover_art_url, "
            "GROUP_CONCAT(at.tag || ':' || at.weight, '|') as tags, at.fetched_at "
            "FROM album_tags at JOIN albums a ON at.album_id = a.id "
            "WHERE at.source = 'essentia' "
            "GROUP BY at.album_id ORDER BY at.fetched_at DESC LIMIT 20"
        ).fetchall()
        source_counts = conn.execute(
            "SELECT source, COUNT(DISTINCT album_id) as albums, COUNT(*) as tags "
            "FROM album_tags GROUP BY source"
        ).fetchall()
    music_dir = config.get("essentia", {}).get("music_dir", "")
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)
    return render_template("essentia.html", total_albums=total_albums, analyzed=analyzed,
                           recent=recent, source_counts=source_counts,
                           music_dir=music_dir, stats=stats, year=year, config=config)


_essentia_status = {"running": False, "processed": 0, "tags": 0, "total": 0}


@app.route("/api/essentia/analyze", methods=["POST"])
def api_essentia_analyze():
    t = get_tracker()
    data = request.get_json() or {}
    album_id = data.get("album_id")
    if album_id:
        added = t.tag_aggregator.enrich_album(int(album_id))
        return jsonify({"status": "analyzed", "tags_added": added})

    if _essentia_status["running"]:
        return jsonify({"status": "already_running", **_essentia_status})

    batch = data.get("batch_size", 5)
    with db.get_db() as conn:
        albums = conn.execute(
            "SELECT a.id FROM albums a "
            "LEFT JOIN album_tags t ON a.id = t.album_id AND t.source = 'essentia' "
            "WHERE t.id IS NULL AND a.state NOT IN ('dismissed') "
            "ORDER BY CASE a.state "
            "  WHEN 'rated' THEN 0 WHEN 'listening' THEN 1 "
            "  WHEN 'listened-unrated' THEN 2 WHEN 'to-listen' THEN 3 ELSE 4 END "
            "LIMIT ?", (batch,)
        ).fetchall()
    album_ids = [a["id"] for a in albums]

    def _run_batch(ids):
        _essentia_status.update(running=True, processed=0, tags=0, total=len(ids))
        for aid in ids:
            try:
                added = t.tag_aggregator.enrich_album(aid)
                _essentia_status["processed"] += 1
                _essentia_status["tags"] += added
            except Exception as e:
                log.error("Essentia error on album %d: %s", aid, e)
                _essentia_status["processed"] += 1
        _essentia_status["running"] = False

    threading.Thread(target=_run_batch, args=(album_ids,), daemon=True).start()
    return jsonify({"status": "started", "queued": len(album_ids)})


@app.route("/api/essentia/status")
def api_essentia_status():
    return jsonify(_essentia_status)


@app.route("/api/album/<int:album_id>/rate-1001", methods=["POST"])
def api_rate_1001(album_id):
    t = get_tracker()
    if not t.gen1001:
        return jsonify({"error": "1001 Albums not configured"}), 400

    data = request.get_json()
    rating = data.get("rating")
    if not rating or not (1 <= int(rating) <= 5):
        return jsonify({"error": "rating must be 1-5"}), 400
    rating = int(rating)

    with db.get_db() as conn:
        album = conn.execute("SELECT mbid, source FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album:
            return jsonify({"error": "album not found"}), 404
        if album["source"] != "1001albums" or not (album["mbid"] or "").startswith("1001-"):
            return jsonify({"error": "not a 1001 Albums album"}), 400
        uuid = album["mbid"][5:]

    result_personal = t.gen1001.rate_album(uuid, rating)
    result_group = t.gen1001.rate_album_group(uuid, rating)

    return jsonify({
        "status": "rated",
        "rating": rating,
        "personal": bool(result_personal),
        "group": bool(result_group),
    })


@app.route("/api/diary/add", methods=["POST"])
def api_add_diary():
    data = request.get_json()
    album_id = data.get("album_id")
    listened_date = data.get("date", db.now_iso()[:10])
    notes = data.get("notes", "")
    if not album_id:
        return jsonify({"error": "album_id required"}), 400
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO diary_entries (album_id, listened_date, notes, created_at) VALUES (?, ?, ?, ?)",
            (album_id, listened_date, notes, db.now_iso())
        )
    return jsonify({"status": "added"})


@app.route("/api/diary/<int:entry_id>/delete", methods=["POST"])
def api_delete_diary(entry_id):
    with db.get_db() as conn:
        conn.execute("DELETE FROM diary_entries WHERE id = ?", (entry_id,))
    return jsonify({"status": "deleted"})


@app.route("/api/discography/add", methods=["POST"])
def api_add_discography():
    t = get_tracker()
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    artist_id = t.add_discography_artist(name)
    if not artist_id:
        return jsonify({"error": "artist not found on MusicBrainz"}), 404
    return jsonify({"status": "added", "artist_id": artist_id})


@app.route("/api/discography/<int:artist_id>/refresh", methods=["POST"])
def api_refresh_discography(artist_id):
    t = get_tracker()
    with db.get_db() as conn:
        artist = conn.execute("SELECT * FROM discography_artists WHERE id = ?", (artist_id,)).fetchone()
    if not artist:
        return jsonify({"error": "not found"}), 404
    t._refresh_discography(artist_id, artist["artist_mbid"])
    return jsonify({"status": "refreshed"})


@app.route("/api/discography/<int:artist_id>/pin", methods=["POST"])
def api_pin_discography(artist_id):
    with db.get_db() as conn:
        conn.execute("UPDATE discography_artists SET pinned = 1 - pinned WHERE id = ?", (artist_id,))
    return jsonify({"status": "toggled"})


@app.route("/api/discography/<int:artist_id>/delete", methods=["POST"])
def api_delete_discography(artist_id):
    with db.get_db() as conn:
        conn.execute("DELETE FROM discography_releases WHERE artist_id = ?", (artist_id,))
        conn.execute("DELETE FROM discography_artists WHERE id = ?", (artist_id,))
    return jsonify({"status": "deleted"})


@app.route("/api/discography/queue-release", methods=["POST"])
def api_queue_discography_release():
    """Add a missing discography release to listening (if downloaded) or to-listen, and request download."""
    t = get_tracker()
    data = request.get_json()
    artist_name = data.get("artist")
    title = data.get("title")
    rg_mbid = data.get("release_group_mbid")
    artist_id = data.get("artist_id")

    if not artist_name or not title:
        return jsonify({"error": "artist and title required"}), 400

    dupe = t._find_duplicate(artist_name, title)
    if dupe:
        return jsonify({"status": "exists", "album_id": dupe["id"]})

    nd_album = t.is_in_library(artist_name, title)
    state = "listening"
    navidrome_id = nd_album.get("id", "") if nd_album else ""

    from .modules.musicbrainz import match_genre_bucket
    mb_meta = t.mb.get_album_metadata(rg_mbid) if rg_mbid else None
    genres = mb_meta.get("genres", []) if mb_meta else []
    genre_bucket = match_genre_bucket(genres, config.get("genre_buckets", []))
    cover_url = mb_meta.get("cover_art_url", "") if mb_meta else ""
    if not cover_url and navidrome_id:
        cover_url = f"/api/cover/{navidrome_id}"
    year = mb_meta.get("year") if mb_meta else None

    album_id = db.upsert_album(
        mbid=rg_mbid or f"disco:{artist_name.lower()[:10]}:{title.lower()[:10]}",
        title=title, artist=artist_name, year=year,
        cover_art_url=cover_url, genre_bucket=genre_bucket,
        genre_tags=",".join(genres), state=state, source="discography",
        navidrome_id=navidrome_id,
    )

    if not nd_album and t.lidarr:
        t._request_via_lidarr(artist_name, title, source="Discography")

    if artist_id:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE discography_releases SET status = 'queued', album_id = ? "
                "WHERE artist_id = ? AND title = ?",
                (album_id, artist_id, title)
            )

    return jsonify({"status": "queued", "album_id": album_id})


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

    threading.Thread(target=t.sync_genre_playlists, daemon=True).start()
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
    if accepted:
        threading.Thread(target=t.sync_genre_playlists, daemon=True).start()
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


@app.route("/api/cover-ext/<mbid>")
def api_cover_ext(mbid):
    """Proxy and cache external Cover Art Archive images. Caches 404s too."""
    import re as re_mod
    from flask import Response, send_file

    if not re_mod.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', mbid):
        return "", 400

    fallback = request.args.get("fb", "")
    if fallback and not re_mod.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', fallback):
        fallback = ""

    cache_dir = os.path.join(os.environ.get("EARWRYM_DATA", "/data"), "cover_cache")
    os.makedirs(cache_dir, exist_ok=True)

    cached_path = os.path.join(cache_dir, mbid + ".jpg")
    miss_path = os.path.join(cache_dir, mbid + ".miss")
    fb_cached = os.path.join(cache_dir, fallback + ".jpg") if fallback else None
    fb_miss = os.path.join(cache_dir, fallback + ".miss") if fallback else None

    if os.path.exists(cached_path):
        return send_file(cached_path, mimetype="image/jpeg", max_age=604800)
    if fb_cached and os.path.exists(fb_cached):
        return send_file(fb_cached, mimetype="image/jpeg", max_age=604800)

    primary_missed = os.path.exists(miss_path)
    fb_missed = fb_miss and os.path.exists(fb_miss)
    if primary_missed and (not fallback or fb_missed):
        return "", 404

    from urllib.request import Request as Req, urlopen as uopen
    mbids_to_try = []
    if not primary_missed:
        mbids_to_try += [(mbid, "release"), (mbid, "release-group")]
    if fallback and not fb_missed:
        mbids_to_try += [(fallback, "release"), (fallback, "release-group")]

    for try_mbid, entity in mbids_to_try:
        url = f"https://coverartarchive.org/{entity}/{try_mbid}/front-500"
        try:
            req = Req(url, headers={"User-Agent": "Earwrym/1.0"})
            with uopen(req, timeout=10) as resp:
                img_data = resp.read()
                save_path = os.path.join(cache_dir, try_mbid + ".jpg")
                with open(save_path, "wb") as f:
                    f.write(img_data)
                return Response(img_data, mimetype=resp.headers.get("Content-Type", "image/jpeg"),
                                headers={"Cache-Control": "public, max-age=604800"})
        except Exception:
            continue

    if not primary_missed:
        with open(miss_path, "w") as f:
            f.write("")
    if fallback and not fb_missed:
        with open(fb_miss, "w") as f:
            f.write("")
    return "", 404


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


@app.route("/api/backfill-covers", methods=["POST"])
def api_backfill_covers():
    """Fix broken/missing cover art URLs (tries CAA release-group, then Navidrome proxy)."""
    t = get_tracker()
    threading.Thread(target=t.backfill_cover_art, daemon=True).start()
    return jsonify({"status": "cover art backfill started"})


@app.route("/api/taste/profile")
def api_taste_profile():
    from .modules.taste import load_profile, compute_profile
    profile = load_profile()
    if not profile:
        profile = compute_profile()
    if not profile:
        return jsonify({"error": "no rated albums to build profile from"}), 400
    top_tags = sorted(profile["tag_counts"].items(), key=lambda x: x[1], reverse=True)[:30]
    top_genres = sorted(profile["genre_counts"].items(), key=lambda x: x[1], reverse=True)[:15]
    return jsonify({
        "album_count": profile["album_count"],
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "top_genres": [{"genre": g, "count": c} for g, c in top_genres],
        "avg_rating": round(sum(profile["ratings"]) / len(profile["ratings"]), 2) if profile["ratings"] else 0,
    })


@app.route("/api/taste/recommendations")
def api_taste_recommendations():
    from .modules.taste import get_recommendations
    limit = int(request.args.get("limit", 20))
    ranked = get_recommendations(limit=limit)
    results = []
    with db.get_db() as conn:
        for r in ranked:
            album = conn.execute("SELECT id, title, artist, cover_art_url, genre_bucket, state "
                                 "FROM albums WHERE id = ?", (r["album_id"],)).fetchone()
            if album:
                results.append({
                    "album_id": album["id"],
                    "title": album["title"],
                    "artist": album["artist"],
                    "cover_art_url": album["cover_art_url"],
                    "genre_bucket": album["genre_bucket"],
                    "state": album["state"],
                    "score": r["final_score"],
                    "has_tags": r["has_tags"],
                    "scores": r["scores"],
                })
    return jsonify(results)


@app.route("/api/taste/recompute", methods=["POST"])
def api_taste_recompute():
    from .modules.taste import compute_profile
    profile = compute_profile()
    if not profile:
        return jsonify({"error": "no rated albums"}), 400
    return jsonify({"status": "recomputed", "album_count": profile["album_count"]})


@app.route("/recommendations")
def recommendations_page():
    from .modules.taste import load_profile
    from .modules.discovery import get_cached_taste_summary, get_cached_recommendations
    year = datetime.now(timezone.utc).year
    stats = db.get_year_stats(year)

    profile = load_profile()
    taste_summary = get_cached_taste_summary()
    profile_data = {"album_count": 0, "top_genres": [], "avg_rating": 0}
    all_genres = set()

    if profile and profile.get("album_count"):
        top_genres = sorted(profile["genre_counts"].items(), key=lambda x: x[1], reverse=True)[:15]
        avg_rating = sum(profile["ratings"]) / len(profile["ratings"]) if profile["ratings"] else 0
        profile_data = {
            "album_count": profile["album_count"],
            "top_genres": [{"genre": g, "count": c} for g, c in top_genres],
            "avg_rating": avg_rating,
        }

    recommendations = get_cached_recommendations(limit=50)

    # Collect all genres from ALL candidates for the genre picker (not just displayed recs)
    with db.get_db() as conn:
        genre_rows = conn.execute("""
            SELECT DISTINCT genre_tags FROM recommendation_candidates
            WHERE genre_tags IS NOT NULL AND genre_tags != ''
            AND dismissed = 0 AND in_library = 0
        """).fetchall()
    for row in genre_rows:
        for g in row["genre_tags"].split(","):
            g = g.strip()
            if g:
                all_genres.add(g)

    # If no cached candidates yet, show a message
    if not recommendations and not taste_summary:
        first_run = True
    else:
        first_run = False

    nd_cfg = config.get("navidrome", {})
    return render_template("recommendations.html",
        recommendations=recommendations, profile=profile_data,
        taste_summary=taste_summary, all_genres=sorted(all_genres),
        navidrome_url=nd_cfg.get("url", ""), auto_dl_count=5,
        stats=stats, year=year, first_run=first_run)


@app.route("/api/recommendations/explain/<int:album_id>", methods=["POST"])
def api_explain_recommendation(album_id):
    from .modules.taste import load_profile, score_album
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503

    profile = load_profile()
    if not profile:
        return jsonify({"error": "no taste profile"}), 400

    result = score_album(album_id, profile=profile)
    if not result:
        return jsonify({"error": "could not score album"}), 404

    with db.get_db() as conn:
        album = conn.execute(
            "SELECT title, artist, genre_bucket, genre_tags, rym_rating FROM albums WHERE id = ?",
            (album_id,)
        ).fetchone()
    if not album:
        return jsonify({"error": "album not found"}), 404

    from .modules.ollama_recs import generate_explanation
    explanation = generate_explanation(
        dict(album), result["scores"], profile,
        ollama_cfg.get("url", "http://10.1.10.67:11434"),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    return jsonify({"album_id": album_id, "explanation": explanation})


@app.route("/api/recommendations/explain-candidate", methods=["POST"])
def api_explain_candidate():
    """Generate AI explanation for an external recommendation candidate."""
    data = request.get_json(silent=True) or {}
    artist = data.get("artist", "")
    album = data.get("album", "")
    candidate_id = data.get("candidate_id")

    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503

    from .modules.taste import load_profile
    profile = load_profile()
    if not profile:
        return jsonify({"error": "no taste profile"}), 400

    # Get candidate info from DB
    candidate = None
    if candidate_id:
        with db.get_db() as conn:
            candidate = conn.execute(
                "SELECT * FROM recommendation_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()

    album_info = {
        "title": album or (candidate["album_title"] if candidate else ""),
        "artist": artist or (candidate["artist_name"] if candidate else ""),
        "genre_tags": candidate["genre_tags"] if candidate else "",
        "genre_bucket": "",
        "rym_rating": None,
    }
    scores = {}
    if candidate and candidate["taste_scores_json"]:
        import json as _json
        scores = _json.loads(candidate["taste_scores_json"])

    from .modules.ollama_recs import generate_explanation
    explanation = generate_explanation(
        album_info, scores, profile,
        ollama_cfg.get("url", "http://10.1.10.67:11434"),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    return jsonify({"candidate_id": candidate_id, "explanation": explanation})


@app.route("/api/ollama/status")
def api_ollama_status():
    """Check Ollama connection and list available models."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"enabled": False})
    from .modules.ollama_recs import check_connection
    models = check_connection(ollama_cfg.get("url", ""))
    return jsonify({
        "enabled": True,
        "connected": models is not None,
        "models": models or [],
        "text_model": ollama_cfg.get("text_model", "llama3:8b"),
        "vision_model": ollama_cfg.get("model", "llava:7b"),
    })


@app.route("/api/ollama/diary-starter", methods=["POST"])
def api_diary_starter():
    """Generate an AI diary entry starter for an album."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503
    data = request.get_json(silent=True) or {}
    album_id = data.get("album_id")
    if not album_id:
        return jsonify({"error": "album_id required"}), 400
    with db.get_db() as conn:
        album = conn.execute(
            "SELECT title, artist, genre_bucket, genre_tags, rym_rating, year FROM albums WHERE id = ?",
            (album_id,)
        ).fetchone()
    if not album:
        return jsonify({"error": "album not found"}), 404
    from .modules.ollama_recs import generate_diary_starter
    starter = generate_diary_starter(
        dict(album),
        ollama_cfg.get("url", ""),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    return jsonify({"album_id": album_id, "starter": starter})


@app.route("/api/ollama/compare", methods=["POST"])
def api_compare_albums():
    """Generate an AI narrative comparison between two albums."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503
    data = request.get_json(silent=True) or {}
    id_a = data.get("album_id_a")
    id_b = data.get("album_id_b")
    if not id_a or not id_b:
        return jsonify({"error": "album_id_a and album_id_b required"}), 400
    with db.get_db() as conn:
        a = conn.execute(
            "SELECT title, artist, genre_bucket, genre_tags, rym_rating FROM albums WHERE id = ?",
            (id_a,)
        ).fetchone()
        b = conn.execute(
            "SELECT title, artist, genre_bucket, genre_tags, rym_rating FROM albums WHERE id = ?",
            (id_b,)
        ).fetchone()
    if not a or not b:
        return jsonify({"error": "album not found"}), 404
    from .modules.ollama_recs import generate_album_comparison
    comparison = generate_album_comparison(
        dict(a), dict(b),
        ollama_cfg.get("url", ""),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    return jsonify({"comparison": comparison})


@app.route("/api/ollama/weekly-digest")
def api_weekly_digest():
    """Get the most recent weekly listening digest."""
    with db.get_db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = 'weekly_digest'").fetchone()
    if not row:
        return jsonify({"digest": None})
    import json as _json
    data = _json.loads(row["value"])
    return jsonify(data)


@app.route("/api/ollama/weekly-digest/generate", methods=["POST"])
def api_generate_weekly_digest():
    """Manually trigger weekly digest generation."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503
    from .modules.ollama_recs import generate_weekly_digest
    from .modules.taste import load_profile
    from datetime import timedelta
    profile = load_profile()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db.get_db() as conn:
        recent = conn.execute(
            "SELECT a.title, a.artist, a.rym_rating, a.genre_bucket "
            "FROM diary_entries d JOIN albums a ON d.album_id = a.id "
            "WHERE d.listened_date >= ? ORDER BY d.listened_date DESC",
            (week_ago[:10],)
        ).fetchall()
    if not recent:
        return jsonify({"error": "no listening data this week"}), 400
    albums_listened = [dict(r) for r in recent]
    rated = [a for a in albums_listened if a.get("rym_rating")]
    top_rated = max(rated, key=lambda x: x["rym_rating"]) if rated else None
    genre_counts = {}
    for a in albums_listened:
        g = a.get("genre_bucket", "Other")
        genre_counts[g] = genre_counts.get(g, 0) + 1
    most_genre = max(genre_counts, key=genre_counts.get) if genre_counts else "mixed"
    listening_data = {
        "albums_listened": albums_listened,
        "total_listens": len(albums_listened),
        "new_genres": [],
        "top_rated": {"artist": top_rated["artist"], "title": top_rated["title"],
                      "rating": top_rated["rym_rating"]} if top_rated else None,
        "most_listened_genre": most_genre,
    }
    digest = generate_weekly_digest(
        listening_data, profile,
        ollama_cfg.get("url", ""),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    if digest:
        import json as _json
        result = {"digest": digest, "generated_at": db.now_iso(),
                  "album_count": len(albums_listened)}
        with db.get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                ("weekly_digest", _json.dumps(result))
            )
        return jsonify(result)
    return jsonify({"error": "generation failed"}), 500


@app.route("/api/ollama/vibe-labels", methods=["POST"])
def api_vibe_labels():
    """Generate mood/vibe labels for an album's tags."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return jsonify({"error": "ollama not configured"}), 503
    data = request.get_json(silent=True) or {}
    album_id = data.get("album_id")
    if not album_id:
        return jsonify({"error": "album_id required"}), 400
    with db.get_db() as conn:
        album = conn.execute(
            "SELECT genre_tags FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
    if not album or not album["genre_tags"]:
        return jsonify({"error": "no tags for this album"}), 404
    tags = [t.strip() for t in album["genre_tags"].split(",") if t.strip()]
    from .modules.ollama_recs import generate_vibe_labels
    labels = generate_vibe_labels(
        tags,
        ollama_cfg.get("url", ""),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    return jsonify({"album_id": album_id, "vibe_labels": labels})


@app.route("/api/recommendations/request-download", methods=["POST"])
def api_request_rec_download():
    """Request a recommended album via Lidarr."""
    data = request.get_json(silent=True) or {}
    artist = data.get("artist", "")
    album = data.get("album", "")
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    t = get_tracker()
    success = t._request_via_lidarr(artist, album, source="rec-manual")
    return jsonify({"status": "requested" if success else "failed"})


@app.route("/api/recommendations/sync-playlist", methods=["POST"])
def api_sync_rec_playlist():
    t = get_tracker()
    result = t.sync_recommendation_playlist()
    return jsonify(result)


@app.route("/api/recommendations/auto-download", methods=["POST"])
def api_auto_download_recs():
    t = get_tracker()
    data = request.get_json(silent=True) or {}
    count = data.get("count", 5)
    result = t.auto_download_recommendations(count=count)
    return jsonify(result)


@app.route("/api/recommendations/discover", methods=["POST"])
def api_run_discovery():
    """Manually trigger the discovery pipeline."""
    import threading
    from .modules.discovery import run_discovery
    from .modules.musicbrainz import MusicBrainzClient
    t = get_tracker()
    mb = MusicBrainzClient()
    def _run():
        try:
            run_discovery(t.lb, mb, config)
        except Exception as e:
            log.error("Manual discovery failed: %s", e, exc_info=True)
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"status": "discovery_started"})


@app.route("/api/recommendations/enrich-genres", methods=["POST"])
def api_enrich_rec_genres():
    """Manually trigger genre enrichment + re-scoring for candidates missing tags."""
    import threading
    from .modules.discovery import _enrich_existing_candidates, _rescore_candidates
    from .modules.taste import load_profile
    from .modules.musicbrainz import MusicBrainzClient
    mb = MusicBrainzClient()
    def _run():
        try:
            _enrich_existing_candidates(mb)
            profile = load_profile()
            if profile:
                _rescore_candidates(profile)
        except Exception as e:
            log.error("Genre enrichment failed: %s", e, exc_info=True)
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"status": "enrichment_started"})


@app.route("/api/recommendations/by-genre")
def api_recs_by_genre():
    """Get recommendations filtered by genre (server-side, not limited to 50)."""
    from .modules.discovery import get_cached_recommendations
    genre = request.args.get("genre", "")
    if not genre:
        return jsonify([])
    recs = get_cached_recommendations(limit=100, genre=genre)
    results = []
    for rec in recs:
        results.append({
            "id": rec["id"],
            "artist_name": rec["artist_name"],
            "album_title": rec["album_title"],
            "year": rec.get("year"),
            "genre_tags": rec.get("genre_tags", ""),
            "taste_score": rec.get("taste_score", 0),
            "cover_art_url": rec.get("cover_art_url", ""),
            "release_mbid": rec.get("release_mbid", ""),
            "release_group_mbid": rec.get("release_group_mbid", ""),
            "source": rec.get("source", ""),
            "has_tags": rec.get("has_tags", 0),
            "release_type": rec.get("release_type", ""),
            "scores": rec.get("scores", {}),
        })
    return jsonify(results)


def _wiki_url_from_rels(relations):
    """Extract Wikipedia URL from MB relations, resolving Wikidata if needed."""
    from urllib.request import Request as Req, urlopen as uopen
    for rel in (relations or []):
        if rel.get("type") == "wikipedia":
            return rel.get("url", {}).get("resource", "")
    for rel in (relations or []):
        if rel.get("type") == "wikidata":
            wd_url = rel.get("url", {}).get("resource", "")
            qid = wd_url.rstrip("/").split("/")[-1] if wd_url else ""
            if qid.startswith("Q"):
                try:
                    req = Req(
                        f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=sitelinks&sitefilter=enwiki&format=json",
                        headers={"User-Agent": "Earwrym/1.0"}
                    )
                    with uopen(req, timeout=5) as resp:
                        data = __import__("json").loads(resp.read())
                        title = data.get("entities", {}).get(qid, {}).get("sitelinks", {}).get("enwiki", {}).get("title", "")
                        if title:
                            return f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
                except Exception:
                    pass
    return ""


@app.route("/api/recommendations/detail/<int:candidate_id>")
def api_rec_detail(candidate_id):
    """Get detailed info for a recommendation candidate (Wikipedia blurb, tags, etc.)."""
    from .modules.musicbrainz import MusicBrainzClient, get_wikipedia_blurb
    with db.get_db() as conn:
        rec = conn.execute(
            "SELECT * FROM recommendation_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    if not rec:
        return jsonify({"error": "not found"}), 404

    result = {
        "id": rec["id"],
        "artist_name": rec["artist_name"],
        "album_title": rec["album_title"],
        "year": rec["year"],
        "genre_tags": rec["genre_tags"] or "",
        "release_type": rec["release_type"] or "",
        "taste_score": rec["taste_score"],
        "source": rec["source"],
    }

    rg_mbid = rec["release_group_mbid"]
    result["wikipedia_blurb"] = ""
    result["artist_blurb"] = ""

    if rg_mbid:
        try:
            mb = MusicBrainzClient()
            rg = mb.get_release_group(rg_mbid, include_rels=True)
            if rg:
                wiki_url = _wiki_url_from_rels(rg.get("relations"))
                if wiki_url:
                    blurb = get_wikipedia_blurb(wiki_url, max_sentences=4)
                    if blurb:
                        result["wikipedia_blurb"] = blurb

                all_tags = []
                for tag in (rg.get("tags") or []):
                    all_tags.append(tag.get("name", ""))
                for tag in (rg.get("genres") or []):
                    all_tags.append(tag.get("name", ""))
                if all_tags:
                    result["all_tags"] = ", ".join(t for t in all_tags if t)

            if not result["wikipedia_blurb"]:
                artist_mbid = rec["artist_mbid"] or ""
                if not artist_mbid and rg:
                    for ac in (rg.get("artist-credit") or []):
                        a = ac.get("artist", {})
                        if a.get("id"):
                            artist_mbid = a["id"]
                            break
                if artist_mbid:
                    artist = mb.get_artist(artist_mbid, include_rels=True)
                    if artist:
                        wiki_url = _wiki_url_from_rels(artist.get("relations"))
                        if wiki_url:
                            blurb = get_wikipedia_blurb(wiki_url, max_sentences=3)
                            if blurb:
                                result["artist_blurb"] = blurb
                        artist_tags = []
                        for tag in (artist.get("genres") or []):
                            artist_tags.append(tag.get("name", ""))
                        if artist_tags and not result.get("all_tags"):
                            result["all_tags"] = ", ".join(t for t in artist_tags if t)
        except Exception:
            pass

    return jsonify(result)


@app.route("/api/recommendations/deep-cuts")
def api_recs_deep_cuts():
    """Get deep-cut recommendations: lower-scored Albums/EPs (the non-obvious picks)."""
    from .modules.discovery import get_cached_recommendations
    recs = get_cached_recommendations(limit=50, deep_cuts=True)
    results = []
    for rec in recs:
        results.append({
            "id": rec["id"],
            "artist_name": rec["artist_name"],
            "album_title": rec["album_title"],
            "year": rec.get("year"),
            "genre_tags": rec.get("genre_tags", ""),
            "taste_score": rec.get("taste_score", 0),
            "cover_art_url": rec.get("cover_art_url", ""),
            "release_mbid": rec.get("release_mbid", ""),
            "release_group_mbid": rec.get("release_group_mbid", ""),
            "source": rec.get("source", ""),
            "has_tags": rec.get("has_tags", 0),
            "release_type": rec.get("release_type", ""),
            "scores": rec.get("scores", {}),
        })
    return jsonify(results)


@app.route("/api/recommendations/dismiss/<int:candidate_id>", methods=["POST"])
def api_dismiss_candidate(candidate_id):
    """Dismiss a recommendation candidate."""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE recommendation_candidates SET dismissed = 1 WHERE id = ?",
            (candidate_id,)
        )
    return jsonify({"status": "dismissed"})


@app.route("/api/tags/enrich", methods=["POST"])
def api_enrich_tags():
    t = get_tracker()
    data = request.get_json() or {}
    album_id = data.get("album_id")
    if album_id:
        added = t.tag_aggregator.enrich_album(int(album_id))
        return jsonify({"status": "enriched", "tags_added": added})
    batch = data.get("batch_size", 20)
    added = t.tag_aggregator.backfill_all(batch_size=batch)
    return jsonify({"status": "batch enriched", "tags_added": added})


@app.route("/api/backfill-blurbs", methods=["POST"])
def api_backfill_blurbs():
    t = get_tracker()
    data = request.get_json() or {}
    batch = data.get("batch_size", 20)
    filled = t.backfill_wikipedia_blurbs(batch_size=batch)
    return jsonify({"status": "ok", "blurbs_added": filled})


@app.route("/api/healthcheck")
def healthcheck():
    return jsonify({"status": "ok", "timestamp": db.now_iso()})


@app.route("/api/healthchecks/setup", methods=["POST"])
def setup_healthchecks():
    """Create per-task Healthchecks checks via the HC Management API."""
    import json as json_mod
    hc = config.get("healthchecks", {})
    api_url = hc.get("api_url", "")
    api_key = hc.get("api_key", "")
    if not api_url or not api_key:
        return jsonify({"error": "Set Healthchecks API URL and API key in settings first"}), 400

    existing = hc.get("checks", {})
    created = {}
    errors = {}
    for slug, defn in HC_TASK_DEFS.items():
        if slug in existing and existing[slug]:
            continue
        try:
            body = json_mod.dumps({
                "name": defn["name"],
                "timeout": defn["timeout"],
                "grace": defn["grace"],
                "tags": "earwrym",
                "unique": ["name"],
            }).encode()
            req = Request(
                api_url.rstrip("/") + "/api/v1/checks/",
                data=body,
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            )
            resp = urlopen(req, timeout=15)
            check = json_mod.loads(resp.read())
            uuid = check["ping_url"].rsplit("/", 1)[-1]
            created[slug] = uuid
        except Exception as e:
            errors[slug] = str(e)

    if created:
        config.setdefault("healthchecks", {}).setdefault("checks", {}).update(created)
        if not config["healthchecks"].get("ping_url"):
            config["healthchecks"]["ping_url"] = api_url.rstrip("/") + "/ping/"
        import yaml
        config_path = os.environ.get("EARWRYM_CONFIG", "/data/config.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return jsonify({
        "created": created,
        "existing": {s: u for s, u in existing.items() if u},
        "errors": errors,
        "total_tasks": len(HC_TASK_DEFS),
    })


@app.route("/api/healthchecks/status")
def healthchecks_status():
    """Return HC configuration status for each task."""
    hc = config.get("healthchecks", {})
    checks = hc.get("checks", {})
    tasks = {}
    for slug, defn in HC_TASK_DEFS.items():
        tasks[slug] = {
            "name": defn["name"],
            "configured": bool(checks.get(slug)),
            "period": defn["timeout"],
            "grace": defn["grace"],
        }
    return jsonify({
        "ping_url": hc.get("ping_url", ""),
        "api_url": hc.get("api_url", ""),
        "has_api_key": bool(hc.get("api_key")),
        "tasks": tasks,
    })


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


HC_TASK_DEFS = {
    "poll-listens": {"name": "Earwrym: Poll Listens", "timeout": 300, "grace": 900},
    "rym-ratings": {"name": "Earwrym: RYM Ratings", "timeout": 7200, "grace": 21600},
    "rym-wishlist": {"name": "Earwrym: RYM Wishlist", "timeout": 21600, "grace": 64800},
    "1001-albums": {"name": "Earwrym: 1001 Albums", "timeout": 86400, "grace": 172800},
    "lidarr-imports": {"name": "Earwrym: Lidarr Imports", "timeout": 3600, "grace": 10800},
    "navidrome-sync": {"name": "Earwrym: Navidrome Sync", "timeout": 600, "grace": 1800},
    "navidrome-discover": {"name": "Earwrym: Navidrome Discovery", "timeout": 3600, "grace": 10800},
    "lidarr-retry": {"name": "Earwrym: Lidarr Retry", "timeout": 3600, "grace": 10800},
    "playlist-sync": {"name": "Earwrym: Playlist Sync", "timeout": 3600, "grace": 10800},
    "genre-backfill": {"name": "Earwrym: Genre Backfill", "timeout": 86400, "grace": 172800},
    "tag-enrichment": {"name": "Earwrym: Tag Enrichment", "timeout": 3600, "grace": 10800},
    "discovery": {"name": "Earwrym: Discovery Pipeline", "timeout": 21600, "grace": 64800},
    "auto-download": {"name": "Earwrym: Auto Download", "timeout": 86400, "grace": 172800},
    "weekly-digest": {"name": "Earwrym: Weekly Digest", "timeout": 604800, "grace": 1209600},
}


def _ping_healthcheck(task_slug, suffix=""):
    """Ping Healthchecks dead-man's switch for a specific task."""
    hc = config.get("healthchecks", {})
    checks = hc.get("checks", {})
    uuid = checks.get(task_slug)
    ping_base = hc.get("ping_url", "")

    if uuid and ping_base:
        url = ping_base.rstrip("/") + "/" + uuid + suffix
    elif not checks and ping_base and task_slug == "poll-listens":
        url = ping_base + suffix
    else:
        return

    try:
        req = Request(url, method="POST")
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
    last_jf_poll = 0
    last_rym_ratings = 0
    last_rym_wishlist = 0
    last_1001 = 0
    last_lidarr = 0
    last_nd_link = 0
    last_playlist_sync = 0
    last_genre_backfill = 0
    last_tag_enrichment = 0
    last_profile_compute = 0
    last_blurb_backfill = 0
    last_lidarr_retry = 0
    last_nd_discover = 0
    last_auto_download = 0
    last_weekly_digest = 0
    weekly_digest_interval = 604800
    lidarr_retry_interval = 3600
    nd_discover_interval = 3600
    nd_link_interval = 600
    lidarr_interval = config.get("lidarr", {}).get("check_interval_seconds", 3600)
    playlist_sync_interval = config.get("playlist_sync_interval_seconds", 3600)
    genre_backfill_interval = 86400
    tag_enrichment_interval = 3600
    profile_compute_interval = 21600

    while _scheduler_running:
        now = time.time()

        if now - last_poll >= poll_interval:
            try:
                t.poll_listens()
                t.poll_jellyfin_listens()
                last_poll = now
                _ping_healthcheck("poll-listens")
            except Exception as e:
                log.error("Poll listens error: %s", e, exc_info=True)
                _ping_healthcheck("poll-listens", "/fail")

        if now - last_rym_ratings >= rym_ratings_interval:
            try:
                t.match_cached_ratings()
                last_rym_ratings = now
                _ping_healthcheck("rym-ratings")
            except Exception as e:
                log.error("RYM ratings error: %s", e, exc_info=True)
                _ping_healthcheck("rym-ratings", "/fail")

        if t.rym and now - last_rym_wishlist >= rym_wishlist_interval:
            try:
                t.check_rym_wishlist()
                last_rym_wishlist = now
                _ping_healthcheck("rym-wishlist")
            except Exception as e:
                log.error("RYM wishlist error: %s", e, exc_info=True)
                _ping_healthcheck("rym-wishlist", "/fail")

        if t.gen1001 and now - last_1001 >= gen1001_interval:
            try:
                t.check_1001_albums()
                last_1001 = now
                _ping_healthcheck("1001-albums")
            except Exception as e:
                log.error("1001 Albums error: %s", e, exc_info=True)
                _ping_healthcheck("1001-albums", "/fail")

        if t.lidarr and now - last_lidarr >= lidarr_interval:
            try:
                t.check_lidarr_imports()
                last_lidarr = now
                _ping_healthcheck("lidarr-imports")
            except Exception as e:
                log.error("Lidarr imports error: %s", e, exc_info=True)
                _ping_healthcheck("lidarr-imports", "/fail")

        if now - last_nd_link >= nd_link_interval:
            try:
                t.sync_navidrome_links()
                last_nd_link = now
                _ping_healthcheck("navidrome-sync")
            except Exception as e:
                log.error("Navidrome sync error: %s", e, exc_info=True)
                _ping_healthcheck("navidrome-sync", "/fail")

        if now - last_nd_discover >= nd_discover_interval:
            try:
                t.discover_navidrome_albums()
                last_nd_discover = now
                _ping_healthcheck("navidrome-discover")
            except Exception as e:
                log.error("Navidrome discover error: %s", e, exc_info=True)
                _ping_healthcheck("navidrome-discover", "/fail")

        if t.lidarr and now - last_lidarr_retry >= lidarr_retry_interval:
            try:
                t.retry_stale_lidarr_grabs()
                last_lidarr_retry = now
                _ping_healthcheck("lidarr-retry")
            except Exception as e:
                log.error("Lidarr retry error: %s", e, exc_info=True)
                _ping_healthcheck("lidarr-retry", "/fail")

        if now - last_playlist_sync >= playlist_sync_interval:
            try:
                t.sync_genre_playlists()
                t.sync_1001_playlist()
                t.sync_recommendation_playlist()
                last_playlist_sync = now
                _ping_healthcheck("playlist-sync")
            except Exception as e:
                log.error("Playlist sync error: %s", e, exc_info=True)
                _ping_healthcheck("playlist-sync", "/fail")

        if now - last_genre_backfill >= genre_backfill_interval:
            try:
                t.backfill_genres()
                t.backfill_wikipedia_blurbs(batch_size=50)
                last_genre_backfill = now
                _ping_healthcheck("genre-backfill")
            except Exception as e:
                log.error("Genre backfill error: %s", e, exc_info=True)
                _ping_healthcheck("genre-backfill", "/fail")

        if now - last_tag_enrichment >= tag_enrichment_interval:
            try:
                t.tag_aggregator.backfill_all(batch_size=100)
                last_tag_enrichment = now
                _ping_healthcheck("tag-enrichment")
            except Exception as e:
                log.error("Tag enrichment error: %s", e, exc_info=True)
                _ping_healthcheck("tag-enrichment", "/fail")

        if now - last_profile_compute >= profile_compute_interval:
            discovery_ok = True
            try:
                from .modules.taste import compute_profile
                compute_profile()
            except Exception as e:
                log.error("Profile compute error: %s", e, exc_info=True)
                discovery_ok = False
            try:
                from .modules.discovery import run_discovery
                from .modules.musicbrainz import MusicBrainzClient
                mb = MusicBrainzClient()
                run_discovery(t.lb, mb, config)
            except Exception as e:
                log.error("Discovery error: %s", e, exc_info=True)
                discovery_ok = False
            last_profile_compute = now
            _ping_healthcheck("discovery", "" if discovery_ok else "/fail")

        if now - last_auto_download >= 86400:
            try:
                t.auto_download_recommendations(count=5, min_score=0.55)
                last_auto_download = now
                _ping_healthcheck("auto-download")
            except Exception as e:
                log.error("Auto download error: %s", e, exc_info=True)
                _ping_healthcheck("auto-download", "/fail")

        ollama_cfg = config.get("ollama", {})
        if ollama_cfg.get("enabled") and now - last_weekly_digest >= weekly_digest_interval:
            try:
                from .modules.ollama_recs import generate_weekly_digest
                from .modules.taste import load_profile
                from datetime import timedelta
                import json as _json_mod
                profile = load_profile()
                week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                with db.get_db() as conn:
                    recent = conn.execute(
                        "SELECT a.title, a.artist, a.rym_rating, a.genre_bucket "
                        "FROM diary_entries d JOIN albums a ON d.album_id = a.id "
                        "WHERE d.listened_date >= ? ORDER BY d.listened_date DESC",
                        (week_ago[:10],)
                    ).fetchall()
                if recent:
                    albums_listened = [dict(r) for r in recent]
                    rated = [a for a in albums_listened if a.get("rym_rating")]
                    top_rated = max(rated, key=lambda x: x["rym_rating"]) if rated else None
                    genre_counts = {}
                    for a in albums_listened:
                        g = a.get("genre_bucket", "Other")
                        genre_counts[g] = genre_counts.get(g, 0) + 1
                    most_genre = max(genre_counts, key=genre_counts.get) if genre_counts else "mixed"
                    listening_data = {
                        "albums_listened": albums_listened,
                        "total_listens": len(albums_listened),
                        "new_genres": [],
                        "top_rated": {"artist": top_rated["artist"], "title": top_rated["title"],
                                      "rating": top_rated["rym_rating"]} if top_rated else None,
                        "most_listened_genre": most_genre,
                    }
                    digest = generate_weekly_digest(
                        listening_data, profile,
                        ollama_cfg.get("url", ""),
                        model=ollama_cfg.get("text_model", "llama3:8b")
                    )
                    if digest:
                        result = {"digest": digest, "generated_at": db.now_iso(),
                                  "album_count": len(albums_listened)}
                        with db.get_db() as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                                ("weekly_digest", _json_mod.dumps(result))
                            )
                        log.info("Weekly digest generated: %d albums", len(albums_listened))
                last_weekly_digest = now
                _ping_healthcheck("weekly-digest")
            except Exception as e:
                log.error("Weekly digest error: %s", e, exc_info=True)
                _ping_healthcheck("weekly-digest", "/fail")

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
