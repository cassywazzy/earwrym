"""External album discovery pipeline.

Fetches recommendation candidates from ListenBrainz (CF recs, fresh releases,
similar artists) and scores them against the user's taste profile. Results
are cached in the recommendation_candidates table for instant page loads.
"""

import json
import logging
import time

from .. import db
from ..normalize import normalize, normalize_artist

log = logging.getLogger(__name__)


def _get_owned_albums(conn):
    """Get set of normalized (artist, title) tuples for all albums in library."""
    rows = conn.execute("SELECT artist, title FROM albums").fetchall()
    owned = set()
    for r in rows:
        a_norm = normalize_artist(r["artist"])
        owned.add((a_norm, normalize(r["title"])))
        owned.add((a_norm, normalize(r["title"], aggressive=True)))
    return owned


def _get_listened_from_lb(lb_client):
    """Get set of (artist_lower, title_lower) from LB top releases (all time + this year)."""
    listened = set()
    for range_ in ("all_time", "this_year"):
        releases = lb_client.get_stats_releases(range_=range_, count=250, offset=0)
        for rel in releases:
            artist = (rel.get("artist_name") or "").strip()
            title = (rel.get("release_name") or "").strip()
            if artist and title:
                a_norm = normalize_artist(artist)
                listened.add((a_norm, normalize(title)))
                listened.add((a_norm, normalize(title, aggressive=True)))
    log.info("LB listening history: %d unique albums to exclude", len(listened))
    return listened


def _get_dismissed_candidates(conn):
    """Get set of (artist_lower, title_lower) for dismissed candidates."""
    rows = conn.execute(
        "SELECT artist_name, album_title FROM recommendation_candidates WHERE dismissed = 1"
    ).fetchall()
    return {(r["artist_name"].lower().strip(), r["album_title"].lower().strip()) for r in rows}


def _is_known(artist, title, known_set, _unused=None):
    """Check if an album is already owned or previously listened to."""
    a = normalize_artist(artist)
    return (a, normalize(title)) in known_set or (a, normalize(title, aggressive=True)) in known_set


def discover_from_cf_recs(lb_client, known_set):
    """Fetch LB collaborative filtering recommendations, resolve to albums."""
    candidates = []
    rec_mbids = []
    for offset in (0, 100, 200):
        batch = lb_client.get_cf_recommendations(count=100, offset=offset)
        if batch:
            rec_mbids.extend(batch)
        time.sleep(0.3)
    if not rec_mbids:
        log.info("CF recs: no results from LB")
        return candidates

    mbid_list = [r["recording_mbid"] for r in rec_mbids if r.get("recording_mbid")]
    scores_by_mbid = {r["recording_mbid"]: r.get("score", 0) for r in rec_mbids}

    for batch_start in range(0, len(mbid_list), 25):
        batch = mbid_list[batch_start:batch_start + 25]
        metadata = lb_client.get_recording_metadata(batch)
        if not metadata:
            continue

        seen_albums = set()
        for mbid_key, meta in metadata.items():
            mbid = mbid_key if isinstance(mbid_key, str) else str(mbid_key)
            artist = meta.get("artist_credit_name") or (meta.get("artist", {}) or {}).get("name", "")
            release = meta.get("release", {}) or {}
            album_title = release.get("name", "")
            if not artist or not album_title:
                continue

            album_key = (artist.lower(), album_title.lower())
            if album_key in seen_albums or _is_known(artist, album_title, known_set, set()):
                continue
            seen_albums.add(album_key)

            release_mbid = release.get("mbid") or release.get("release_mbid", "")
            caa_id = release.get("caa_id")
            cover_url = f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else ""

            tags = meta.get("tag", {}) or {}
            tag_list = []
            if isinstance(tags, dict):
                for tag_info in (tags.get("artist", []) or []) + (tags.get("release_group", []) or []) + (tags.get("recording", []) or []):
                    if isinstance(tag_info, dict):
                        tag_list.append(tag_info.get("tag", ""))
                    elif isinstance(tag_info, str):
                        tag_list.append(tag_info)
            tag_str = ", ".join(t for t in tag_list if t)[:500]

            source_mbid = mbid if isinstance(mbid, str) else batch[0]
            candidates.append({
                "artist_name": artist,
                "album_title": album_title,
                "release_mbid": release_mbid,
                "genre_tags": tag_str,
                "cover_art_url": cover_url,
                "source": "lb_cf",
                "source_score": scores_by_mbid.get(source_mbid, 0),
            })
        time.sleep(0.5)

    log.info("CF recs: discovered %d candidate albums", len(candidates))
    return candidates


def discover_from_fresh_releases(lb_client, known_set):
    """Fetch personalized fresh releases from LB."""
    candidates = []
    releases = lb_client.get_fresh_releases(days=60, sort="confidence")
    if not releases:
        log.info("Fresh releases: no results from LB")
        return candidates

    for rel in releases:
        artist = rel.get("artist_credit_name", "")
        title = rel.get("release_name", "")
        if not artist or not title:
            continue
        if _is_known(artist, title, known_set):
            continue

        release_mbid = rel.get("release_mbid", "")
        rg_mbid = rel.get("release_group_mbid", "")
        caa_id = rel.get("caa_id")
        cover_url = ""
        if caa_id and release_mbid:
            cover_url = f"https://coverartarchive.org/release/{release_mbid}/front-500"
        elif release_mbid:
            cover_url = f"https://coverartarchive.org/release/{release_mbid}/front-500"

        candidates.append({
            "artist_name": artist,
            "album_title": title,
            "release_mbid": release_mbid,
            "release_group_mbid": rg_mbid,
            "genre_tags": "",
            "cover_art_url": cover_url,
            "source": "lb_fresh",
            "source_score": rel.get("confidence", 0),
            "year": _parse_year(rel.get("release_date", "")),
        })

    log.info("Fresh releases: discovered %d candidate albums", len(candidates))
    return candidates


def discover_from_similar_artists(lb_client, mb_client, top_artist_mbids, known_set, max_artists=15):
    """Find albums from artists similar to the user's top rated artists."""
    candidates = []
    if not top_artist_mbids:
        return candidates

    similar = lb_client.get_similar_artists(top_artist_mbids[:15])
    if not similar:
        log.info("Similar artists: no results from LB Labs")
        return candidates

    seen_artists = set()
    artist_list = similar if isinstance(similar, list) else []
    for item in artist_list[:max_artists]:
        if not isinstance(item, dict):
            continue
        artist_mbid = item.get("artist_mbid", "")
        artist_name = item.get("name", "")
        sim_score = item.get("score", 0)

        if not artist_mbid or not artist_name:
            continue
        if artist_mbid in seen_artists:
            continue
        seen_artists.add(artist_mbid)

        try:
            rgs = mb_client.get_artist_release_groups(artist_mbid)
            time.sleep(1.2)
        except Exception as e:
            log.debug("MB release-groups for %s failed: %s", artist_name, e)
            continue

        for rg in (rgs or [])[:5]:
            title = rg.get("title", "")
            if not title or _is_known(artist_name, title, known_set):
                continue

            rg_mbid = rg.get("id", "")
            cover_url = f"https://coverartarchive.org/release-group/{rg_mbid}/front-500" if rg_mbid else ""
            year = None
            fd = rg.get("first-release-date", "")
            if fd:
                year = _parse_year(fd)

            candidates.append({
                "artist_name": artist_name,
                "album_title": title,
                "artist_mbid": artist_mbid,
                "release_group_mbid": rg_mbid,
                "genre_tags": "",
                "cover_art_url": cover_url,
                "source": "lb_similar",
                "source_score": sim_score,
                "year": year,
            })

    log.info("Similar artists: discovered %d candidate albums from %d artists",
             len(candidates), len(seen_artists))
    return candidates


def discover_from_daily_jams(lb_client, known_set):
    """Fetch tracks from recent LB Daily Jams playlists."""
    candidates = []
    playlists = lb_client.get_recommendation_playlist()
    if not playlists:
        return candidates

    daily_jams = [p for p in playlists
                  if "daily" in (p.get("playlist", {}).get("title", "") or "").lower()
                  or "jams" in (p.get("playlist", {}).get("title", "") or "").lower()]

    for pl in daily_jams[:3]:
        pl_data = pl.get("playlist", {})
        identifier = pl_data.get("identifier", "")
        if not identifier:
            continue
        pl_mbid = identifier.rstrip("/").split("/")[-1]

        tracks = lb_client.get_playlist_tracks(pl_mbid)
        seen_albums = set()
        for track in (tracks or []):
            ext = track.get("extension", {})
            jspf_ext = ext.get("https://musicbrainz.org/doc/jspf#track", {})
            artist = track.get("creator", "") or jspf_ext.get("artist_credit_name", "")
            # Title from album, not track
            album_title = jspf_ext.get("release_name", "")
            if not artist or not album_title:
                continue
            key = (artist.lower(), album_title.lower())
            if key in seen_albums or _is_known(artist, album_title, known_set, set()):
                continue
            seen_albums.add(key)

            release_mbid = jspf_ext.get("release_mbid", "")
            cover_url = f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else ""

            candidates.append({
                "artist_name": artist,
                "album_title": album_title,
                "release_mbid": release_mbid,
                "genre_tags": "",
                "cover_art_url": cover_url,
                "source": "lb_daily_jams",
                "source_score": 0,
            })
        time.sleep(0.5)

    log.info("Daily Jams: discovered %d candidate albums", len(candidates))
    return candidates


def _parse_year(date_str):
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, IndexError):
        return None


def score_candidates(candidates, profile):
    """Score candidates against taste profile using lightweight matching."""
    from .taste import quantile_rank, NOVELTY_FACTOR, DEFAULT_WEIGHTS
    from statistics import mean

    if not profile or not profile.get("album_count"):
        return candidates

    for c in candidates:
        scores = {}

        # Genre matching from genre_tags
        album_genres = set()
        if c.get("genre_tags"):
            for g in c["genre_tags"].split(","):
                g = g.strip().lower()
                if g:
                    album_genres.add(g)

        if album_genres and profile.get("genre_values"):
            genre_scores = []
            for genre in album_genres:
                count = profile["genre_counts"].get(genre, 0)
                if count > 0:
                    genre_scores.append(quantile_rank(profile["genre_values"], count))
                else:
                    genre_scores.append(NOVELTY_FACTOR)
            scores["primary_genres"] = mean(genre_scores)
            scores["cross_genres"] = scores["primary_genres"]
            scores["descriptors"] = scores["primary_genres"]
        else:
            scores["descriptors"] = NOVELTY_FACTOR
            scores["primary_genres"] = NOVELTY_FACTOR
            scores["cross_genres"] = NOVELTY_FACTOR

        scores["rating"] = NOVELTY_FACTOR

        # Weighted final score
        flat = []
        for key, w in DEFAULT_WEIGHTS.items():
            if key in scores:
                flat.extend([scores[key]] * w)
        c["taste_score"] = round(mean(flat), 4) if flat else 0.0
        c["taste_scores_json"] = json.dumps(scores)
        c["has_tags"] = bool(album_genres)

    return candidates


def _extract_mb_tags(entity):
    """Extract genre/tag names from a MusicBrainz entity (release-group, artist, etc)."""
    tags = []
    for g in (entity.get("genres") or []):
        name = g.get("name", "")
        if name and name not in tags:
            tags.append(name)
    for t in (entity.get("tags") or []):
        name = t.get("name", "")
        if name and name not in tags:
            tags.append(name)
    return tags


def _enrich_genres(candidates, mb_client):
    """Fetch genre tags from MusicBrainz for candidates that have none."""
    need_enrichment = [c for c in candidates if not c.get("genre_tags") and c.get("release_group_mbid")]
    if not need_enrichment:
        log.info("Genre enrichment: all candidates already have tags or no RG MBIDs")
        return

    enriched = 0
    need_artist_fallback = []

    for c in need_enrichment[:80]:
        rg_mbid = c["release_group_mbid"]
        try:
            rg = mb_client.get_release_group(rg_mbid)
            if not rg:
                continue
            c["release_type"] = rg.get("primary-type", "")
            tags = _extract_mb_tags(rg)
            if tags:
                c["genre_tags"] = ", ".join(tags)[:500]
                enriched += 1
            else:
                for ac in (rg.get("artist-credit") or []):
                    a = ac.get("artist", {})
                    if a.get("id"):
                        need_artist_fallback.append((c, a["id"]))
                        break
        except Exception as e:
            log.debug("MB genre lookup for %s failed: %s", rg_mbid, e)

    # For candidates with release_mbid but no release_group_mbid, resolve via MB
    need_rg = [c for c in candidates
               if not c.get("genre_tags") and not c.get("release_group_mbid")
               and c.get("release_mbid")]
    for c in need_rg[:30]:
        try:
            rel = mb_client.get_release(c["release_mbid"])
            if rel and rel.get("release-group"):
                rg = rel["release-group"]
                c["release_group_mbid"] = rg.get("id", "")
                c["release_type"] = rg.get("primary-type", "")
                tags = _extract_mb_tags(rg)
                if tags:
                    c["genre_tags"] = ", ".join(tags)[:500]
                    enriched += 1
        except Exception as e:
            log.debug("MB release->RG lookup failed: %s", e)

    # Artist-level tag fallback
    seen_artists = {}
    for c, artist_mbid in need_artist_fallback[:50]:
        if c.get("genre_tags"):
            continue
        if artist_mbid in seen_artists:
            tags = seen_artists[artist_mbid]
        else:
            try:
                artist = mb_client.get_artist(artist_mbid)
                tags = _extract_mb_tags(artist) if artist else []
                seen_artists[artist_mbid] = tags
            except Exception:
                tags = []
                seen_artists[artist_mbid] = tags
        if tags:
            c["genre_tags"] = ", ".join(tags)[:500]
            enriched += 1

    # Also try stored artist_mbid for remaining
    need_stored = [c for c in candidates
                   if not c.get("genre_tags") and c.get("artist_mbid")
                   and c["artist_mbid"] not in seen_artists]
    for c in need_stored[:20]:
        try:
            artist = mb_client.get_artist(c["artist_mbid"])
            tags = _extract_mb_tags(artist) if artist else []
            if tags:
                c["genre_tags"] = ", ".join(tags)[:500]
                enriched += 1
        except Exception:
            pass

    log.info("Genre enrichment: enriched %d candidates with MB tags", enriched)


def run_discovery(lb_client, mb_client, config):
    """Run the full discovery pipeline and update the candidates table.

    Called by the scheduler every 6 hours.
    """
    from .taste import load_profile, compute_profile

    log.info("Starting recommendation discovery pipeline...")

    profile = load_profile()
    if not profile:
        profile = compute_profile()
    if not profile:
        log.warning("Discovery: no taste profile available")
        return {"status": "no_profile", "candidates": 0}

    with db.get_db() as conn:
        owned_set = _get_owned_albums(conn)
        dismissed = _get_dismissed_candidates(conn)

    try:
        listened_set = _get_listened_from_lb(lb_client)
    except Exception as e:
        log.warning("Failed to fetch LB listening history: %s", e)
        listened_set = set()

    known_set = owned_set | listened_set
    log.info("Known set: %d owned + %d listened = %d total exclusions",
             len(owned_set), len(listened_set), len(known_set))

    # Gather candidates from all sources
    all_candidates = []

    try:
        cf_recs = discover_from_cf_recs(lb_client, known_set)
        all_candidates.extend(cf_recs)
    except Exception as e:
        log.error("CF recs discovery failed: %s", e)

    try:
        fresh = discover_from_fresh_releases(lb_client, known_set)
        all_candidates.extend(fresh)
    except Exception as e:
        log.error("Fresh releases discovery failed: %s", e)

    try:
        top_artists = _get_top_rated_artist_mbids(profile, lb_client)
        similar = discover_from_similar_artists(lb_client, mb_client, top_artists, known_set)
        all_candidates.extend(similar)
    except Exception as e:
        log.error("Similar artists discovery failed: %s", e)

    try:
        jams = discover_from_daily_jams(lb_client, known_set)
        all_candidates.extend(jams)
    except Exception as e:
        log.error("Daily Jams discovery failed: %s", e)

    # Deduplicate
    seen = set()
    unique = []
    for c in all_candidates:
        key = (c["artist_name"].lower().strip(), c["album_title"].lower().strip())
        if key not in seen and key not in dismissed:
            seen.add(key)
            unique.append(c)

    # Enrich candidates missing genre tags via MusicBrainz
    try:
        _enrich_genres(unique, mb_client)
    except Exception as e:
        log.warning("Genre enrichment failed: %s", e)

    # Score
    scored = score_candidates(unique, profile)

    # Persist
    now = db.now_iso()
    inserted = 0
    with db.get_db() as conn:
        for c in scored:
            try:
                conn.execute("""
                    INSERT INTO recommendation_candidates
                    (artist_name, album_title, artist_mbid, release_group_mbid, release_mbid,
                     year, genre_tags, cover_art_url, source, source_score,
                     taste_score, taste_scores_json, has_tags, in_library, dismissed,
                     release_type, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    ON CONFLICT(artist_name, album_title) DO UPDATE SET
                        source_score = MAX(excluded.source_score, recommendation_candidates.source_score),
                        taste_score = excluded.taste_score,
                        taste_scores_json = excluded.taste_scores_json,
                        has_tags = excluded.has_tags,
                        cover_art_url = COALESCE(NULLIF(excluded.cover_art_url, ''), recommendation_candidates.cover_art_url),
                        genre_tags = COALESCE(NULLIF(excluded.genre_tags, ''), recommendation_candidates.genre_tags),
                        release_type = COALESCE(NULLIF(excluded.release_type, ''), recommendation_candidates.release_type)
                """, (
                    c["artist_name"], c["album_title"],
                    c.get("artist_mbid", ""), c.get("release_group_mbid", ""), c.get("release_mbid", ""),
                    c.get("year"), c.get("genre_tags", ""), c.get("cover_art_url", ""),
                    c["source"], c.get("source_score", 0),
                    c.get("taste_score", 0), c.get("taste_scores_json", "{}"),
                    1 if c.get("has_tags") else 0,
                    c.get("release_type", ""),
                    now,
                ))
                inserted += 1
            except Exception as e:
                log.debug("Failed to insert candidate %s - %s: %s",
                          c["artist_name"], c["album_title"], e)

    # Update in_library flag for any candidates that got downloaded since last run
    with db.get_db() as conn:
        conn.execute("""
            UPDATE recommendation_candidates SET in_library = 1
            WHERE EXISTS (
                SELECT 1 FROM albums a
                WHERE LOWER(a.artist) = LOWER(recommendation_candidates.artist_name)
                AND LOWER(a.title) = LOWER(recommendation_candidates.album_title)
            )
        """)

    # Also mark candidates as in_library if they appear in the user's LB listening history
    with db.get_db() as conn:
        existing = conn.execute("""
            SELECT id, artist_name, album_title FROM recommendation_candidates
            WHERE in_library = 0 AND dismissed = 0
        """).fetchall()
        marked = 0
        for row in existing:
            if _is_known(row["artist_name"], row["album_title"], known_set):
                conn.execute("UPDATE recommendation_candidates SET in_library = 1 WHERE id = ?", (row["id"],))
                marked += 1
        if marked:
            log.info("Marked %d existing candidates as known (from LB history)", marked)

    # Re-enrich existing candidates that still have no genre tags
    try:
        _enrich_existing_candidates(mb_client)
    except Exception as e:
        log.warning("Existing candidate enrichment failed: %s", e)

    # Re-score candidates that gained tags since last scoring
    try:
        _rescore_candidates(profile)
    except Exception as e:
        log.warning("Re-scoring failed: %s", e)

    # Cache taste summary
    _cache_taste_summary(profile, config)

    log.info("Discovery pipeline complete: %d candidates inserted/updated", inserted)
    return {"status": "ok", "candidates": inserted}


def _enrich_existing_candidates(mb_client):
    """Re-enrich ALL DB candidates that have no genre tags but do have MBIDs.

    Processes in batches of 100 to keep memory reasonable. Each batch does
    RG-level tag lookup, then artist-credit fallback for those with no RG tags.
    """
    total_enriched = 0
    artist_cache = {}

    while True:
        with db.get_db() as conn:
            rows = conn.execute("""
                SELECT id, release_group_mbid, release_mbid, artist_mbid
                FROM recommendation_candidates
                WHERE (genre_tags IS NULL OR genre_tags = '') AND dismissed = 0
                AND (release_group_mbid != '' OR release_mbid != '')
                LIMIT 100
            """).fetchall()

        if not rows:
            break

        enriched = 0
        need_artist_fallback = []

        for row in rows:
            rg_mbid = row["release_group_mbid"]
            tags = []
            release_type = ""

            if rg_mbid:
                try:
                    rg = mb_client.get_release_group(rg_mbid)
                    if rg:
                        release_type = rg.get("primary-type", "")
                        tags = _extract_mb_tags(rg)
                        if not tags:
                            for ac in (rg.get("artist-credit") or []):
                                a = ac.get("artist", {})
                                if a.get("id"):
                                    need_artist_fallback.append((row, a["id"], release_type))
                                    break
                except Exception:
                    pass

            if not tags and row["release_mbid"] and not rg_mbid:
                try:
                    rel = mb_client.get_release(row["release_mbid"])
                    if rel and rel.get("release-group"):
                        rg = rel["release-group"]
                        release_type = rg.get("primary-type", "")
                        tags = _extract_mb_tags(rg)
                        if not tags:
                            for ac in (rel.get("artist-credit") or []):
                                a = ac.get("artist", {})
                                if a.get("id"):
                                    need_artist_fallback.append((row, a["id"], release_type))
                                    break
                except Exception:
                    pass

            if tags:
                tag_str = ", ".join(tags)[:500]
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE recommendation_candidates SET genre_tags = ?, has_tags = 1, release_type = ? WHERE id = ?",
                        (tag_str, release_type, row["id"])
                    )
                    enriched += 1
            elif release_type:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE recommendation_candidates SET release_type = ? WHERE id = ?",
                        (release_type, row["id"])
                    )

        for row, artist_mbid, release_type in need_artist_fallback:
            if artist_mbid in artist_cache:
                tags = artist_cache[artist_mbid]
            else:
                try:
                    artist = mb_client.get_artist(artist_mbid)
                    tags = _extract_mb_tags(artist) if artist else []
                except Exception:
                    tags = []
                artist_cache[artist_mbid] = tags

            if tags:
                tag_str = ", ".join(tags)[:500]
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE recommendation_candidates SET genre_tags = ?, has_tags = 1, release_type = ? WHERE id = ?",
                        (tag_str, release_type, row["id"])
                    )
                    enriched += 1
            elif release_type:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE recommendation_candidates SET release_type = ? WHERE id = ?",
                        (release_type, row["id"])
                    )

        total_enriched += enriched
        log.info("Enrichment batch: %d enriched this batch, %d total so far", enriched, total_enriched)

        if enriched == 0:
            break

    log.info("Enriched %d existing candidates with genre tags (all batches)", total_enriched)

    # Backfill release_type for candidates that have RG MBIDs but no type yet
    _backfill_release_types(mb_client)


def _backfill_release_types(mb_client):
    """Fetch release_type from MB for candidates missing it. Loops until done."""
    total_updated = 0

    while True:
        with db.get_db() as conn:
            rows = conn.execute("""
                SELECT id, release_group_mbid FROM recommendation_candidates
                WHERE (release_type IS NULL OR release_type = '') AND dismissed = 0
                AND release_group_mbid IS NOT NULL AND release_group_mbid != ''
                LIMIT 200
            """).fetchall()

        if not rows:
            break

        batch_updated = 0
        for row in rows:
            try:
                rg = mb_client.get_release_group(row["release_group_mbid"])
                if rg:
                    rt = rg.get("primary-type", "")
                    if rt:
                        with db.get_db() as conn:
                            conn.execute(
                                "UPDATE recommendation_candidates SET release_type = ? WHERE id = ?",
                                (rt, row["id"])
                            )
                        batch_updated += 1
            except Exception:
                pass

        total_updated += batch_updated
        log.info("Backfill batch: %d typed this batch, %d total", batch_updated, total_updated)

        if batch_updated == 0:
            break

    if total_updated:
        log.info("Backfilled release_type for %d candidates total", total_updated)


def _rescore_candidates(profile):
    """Re-score all non-dismissed candidates using current taste profile."""
    from .taste import quantile_rank, NOVELTY_FACTOR, DEFAULT_WEIGHTS
    from statistics import mean

    if not profile or not profile.get("album_count"):
        return

    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT id, genre_tags, has_tags FROM recommendation_candidates
            WHERE dismissed = 0
        """).fetchall()

    updated = 0
    for row in rows:
        scores = {}
        album_genres = set()
        if row["genre_tags"]:
            for g in row["genre_tags"].split(","):
                g = g.strip().lower()
                if g:
                    album_genres.add(g)

        if album_genres and profile.get("genre_values"):
            genre_scores = []
            for genre in album_genres:
                count = profile["genre_counts"].get(genre, 0)
                if count > 0:
                    genre_scores.append(quantile_rank(profile["genre_values"], count))
                else:
                    genre_scores.append(NOVELTY_FACTOR)
            scores["primary_genres"] = mean(genre_scores)
            scores["cross_genres"] = scores["primary_genres"]
            scores["descriptors"] = scores["primary_genres"]
        else:
            scores["descriptors"] = NOVELTY_FACTOR
            scores["primary_genres"] = NOVELTY_FACTOR
            scores["cross_genres"] = NOVELTY_FACTOR

        scores["rating"] = NOVELTY_FACTOR

        flat = []
        for key, w in DEFAULT_WEIGHTS.items():
            if key in scores:
                flat.extend([scores[key]] * w)
        taste_score = round(mean(flat), 4) if flat else 0.0

        with db.get_db() as conn:
            conn.execute("""
                UPDATE recommendation_candidates
                SET taste_score = ?, taste_scores_json = ?, has_tags = ?
                WHERE id = ?
            """, (taste_score, json.dumps(scores), 1 if album_genres else 0, row["id"]))
            updated += 1

    log.info("Re-scored %d candidates", updated)


def _get_top_rated_artist_mbids(profile, lb_client=None):
    """Get artist MBIDs from LB listening stats + local discography table."""
    artist_mbids = []
    seen = set()

    if lb_client:
        try:
            stats = lb_client.get_stats_artists(range_="all_time", count=50)
            for a in (stats or []):
                mbid = a.get("artist_mbid") or (a.get("artist_mbids") or [None])[0]
                if mbid and len(mbid) == 36 and mbid not in seen:
                    artist_mbids.append(mbid)
                    seen.add(mbid)
        except Exception as e:
            log.debug("LB stats artists failed: %s", e)

    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT artist_mbid FROM discography_artists
            WHERE artist_mbid IS NOT NULL
        """).fetchall()
        for row in rows:
            mbid = row["artist_mbid"]
            if mbid and len(mbid) == 36 and mbid not in seen:
                artist_mbids.append(mbid)
                seen.add(mbid)

    return artist_mbids[:25]


def _cache_taste_summary(profile, config):
    """Generate and cache taste summary via Ollama."""
    ollama_cfg = config.get("ollama", {})
    if not ollama_cfg.get("enabled"):
        return

    from .ollama_recs import generate_taste_summary
    summary = generate_taste_summary(
        profile, ollama_cfg.get("url", "http://10.1.10.67:11434"),
        model=ollama_cfg.get("text_model", "llama3:8b")
    )
    if summary:
        with db.get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                ("taste_summary", json.dumps({
                    "text": summary,
                    "generated_at": db.now_iso(),
                    "album_count": profile.get("album_count", 0),
                }))
            )
        log.info("Cached taste summary (%d chars)", len(summary))


def get_cached_taste_summary():
    """Get cached taste summary from kv store."""
    with db.get_db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = 'taste_summary'").fetchone()
        if row:
            data = json.loads(row["value"])
            return data.get("text")
    return None


def get_cached_recommendations(limit=50, genre=None, deep_cuts=False):
    """Get cached recommendation candidates for display."""
    max_per_artist = 2
    with db.get_db() as conn:
        query = """
            SELECT * FROM recommendation_candidates
            WHERE in_library = 0 AND dismissed = 0
        """
        params = []

        if genre:
            query += " AND (genre_tags LIKE ? OR genre_tags LIKE ?)"
            params.extend([f"%{genre}%", f"%{genre.lower()}%"])
        elif deep_cuts:
            query += " AND release_type IN ('Album', 'EP') AND taste_score BETWEEN 0.15 AND 0.45"
        else:
            query += " AND taste_score >= 0.10"

        if deep_cuts:
            query += " ORDER BY taste_score DESC, source_score DESC LIMIT ?"
        else:
            query += """ ORDER BY
                CASE WHEN release_type IN ('Album', 'EP') THEN 0
                     WHEN release_type = '' OR release_type IS NULL THEN 1
                     ELSE 2 END,
                taste_score DESC, source_score DESC LIMIT ?"""
        params.append(limit * 3)

        rows = conn.execute(query, params).fetchall()

        results = []
        artist_counts = {}
        for row in rows:
            artist = row["artist_name"]
            if artist_counts.get(artist, 0) >= max_per_artist:
                continue
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
            r = dict(row)
            try:
                r["scores"] = json.loads(r.get("taste_scores_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                r["scores"] = {}
            results.append(r)
            if len(results) >= limit:
                break
        return results
