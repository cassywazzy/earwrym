"""Taste profile computation and album scoring.

Builds a statistical taste profile from the user's rated albums,
then scores candidate albums using quantile-rank similarity
(adapted from Lute's quantile-rank model).

The profile captures distributions of:
- Tags/descriptors (the vibes layer: "melancholic", "atmospheric", "lo-fi")
- Genres (coarser categories from genre_bucket + genre_tags)
- Ratings (what rating range the user gravitates toward)
"""

import json
import logging
from collections import Counter
from statistics import mean

from .. import db

log = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    "descriptors": 100,
    "primary_genres": 30,
    "cross_genres": 20,
    "rating": 10,
    "rating_count": 5,
}

NOVELTY_FACTOR = 0.15


def quantile_rank(data, value):
    """Fraction of values in data that are <= value. Returns 0-1."""
    if not data:
        return NOVELTY_FACTOR
    count = sum(1 for d in data if d <= value)
    return count / len(data)


def compute_profile(profile_name="default"):
    """Build a taste profile from all rated albums.

    Returns a profile dict with tag, genre, and rating distributions,
    and persists it to the taste_profiles table.
    """
    with db.get_db() as conn:
        albums = conn.execute(
            "SELECT a.id, a.artist, a.title, a.rym_rating, a.genre_bucket, a.genre_tags "
            "FROM albums a WHERE a.state = 'rated' AND a.rym_rating IS NOT NULL"
        ).fetchall()

        if not albums:
            log.warning("No rated albums found for profile computation")
            return None

        tag_counter = Counter()
        genre_counter = Counter()
        ratings = []

        for album in albums:
            ratings.append(album["rym_rating"])

            if album["genre_bucket"] and album["genre_bucket"] != "Other":
                genre_counter[album["genre_bucket"]] += 1

            if album["genre_tags"]:
                for tag in album["genre_tags"].split(","):
                    tag = tag.strip().lower()
                    if tag:
                        genre_counter[tag] += 1

            tags = conn.execute(
                "SELECT tag, weight FROM album_tags WHERE album_id = ?",
                (album["id"],)
            ).fetchall()
            for t in tags:
                tag_counter[t["tag"].lower()] += t["weight"]

        profile = {
            "name": profile_name,
            "album_count": len(albums),
            "tag_counts": dict(tag_counter),
            "genre_counts": dict(genre_counter),
            "ratings": ratings,
            "tag_values": sorted(tag_counter.values()) if tag_counter else [],
            "genre_values": sorted(genre_counter.values()) if genre_counter else [],
        }

        conn.execute(
            "INSERT OR REPLACE INTO taste_profiles "
            "(name, tag_distribution, genre_distribution, rating_distribution, album_count, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                profile_name,
                json.dumps(profile["tag_counts"]),
                json.dumps(profile["genre_counts"]),
                json.dumps(profile["ratings"]),
                len(albums),
                db.now_iso(),
            )
        )

    log.info("Computed taste profile '%s': %d albums, %d tags, %d genres",
             profile_name, len(albums), len(tag_counter), len(genre_counter))
    return profile


def load_profile(profile_name="default"):
    """Load a persisted taste profile from the database."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM taste_profiles WHERE name = ?", (profile_name,)
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "album_count": row["album_count"],
            "tag_counts": json.loads(row["tag_distribution"] or "{}"),
            "genre_counts": json.loads(row["genre_distribution"] or "{}"),
            "ratings": json.loads(row["rating_distribution"] or "[]"),
            "tag_values": sorted(json.loads(row["tag_distribution"] or "{}").values()) if row["tag_distribution"] else [],
            "genre_values": sorted(json.loads(row["genre_distribution"] or "{}").values()) if row["genre_distribution"] else [],
        }


def score_album(album_id, profile=None, weights=None):
    """Score an album against a taste profile using quantile-rank similarity.

    Returns a dict with sub-scores and a final weighted score (0-1).
    Higher = better match to the user's taste.
    """
    if profile is None:
        profile = load_profile()
    if profile is None:
        return None
    if weights is None:
        weights = DEFAULT_WEIGHTS

    with db.get_db() as conn:
        album = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album:
            return None

        tags = conn.execute(
            "SELECT tag, weight FROM album_tags WHERE album_id = ?", (album_id,)
        ).fetchall()

    album_tags = {t["tag"].lower(): t["weight"] for t in tags}
    album_genres = set()
    if album["genre_bucket"] and album["genre_bucket"] != "Other":
        album_genres.add(album["genre_bucket"])
    if album["genre_tags"]:
        for g in album["genre_tags"].split(","):
            g = g.strip().lower()
            if g:
                album_genres.add(g)

    scores = {}

    # Descriptor/tag similarity (the big one — 52.6% default weight)
    if album_tags and profile["tag_values"]:
        tag_scores = []
        for tag, weight in album_tags.items():
            count = profile["tag_counts"].get(tag, 0)
            if count > 0:
                tag_scores.append(quantile_rank(profile["tag_values"], count))
            else:
                tag_scores.append(NOVELTY_FACTOR)
        scores["descriptors"] = mean(tag_scores) if tag_scores else NOVELTY_FACTOR
    else:
        scores["descriptors"] = NOVELTY_FACTOR

    # Genre similarity
    if album_genres and profile["genre_values"]:
        genre_scores = []
        for genre in album_genres:
            count = profile["genre_counts"].get(genre, 0)
            if count > 0:
                genre_scores.append(quantile_rank(profile["genre_values"], count))
            else:
                genre_scores.append(NOVELTY_FACTOR)
        scores["primary_genres"] = mean(genre_scores) if genre_scores else NOVELTY_FACTOR
    else:
        scores["primary_genres"] = NOVELTY_FACTOR

    # Cross-genre: album's tags looked up in genre distribution
    if album_tags and profile["genre_values"]:
        cross_scores = []
        for tag in album_tags:
            count = profile["genre_counts"].get(tag, 0)
            if count > 0:
                cross_scores.append(quantile_rank(profile["genre_values"], count))
            else:
                cross_scores.append(NOVELTY_FACTOR)
        scores["cross_genres"] = mean(cross_scores) if cross_scores else NOVELTY_FACTOR
    else:
        scores["cross_genres"] = NOVELTY_FACTOR

    # Rating quantile (how does this album's RYM rating compare to what the user rates?)
    if album["rym_rating"] and profile["ratings"]:
        scores["rating"] = quantile_rank(sorted(profile["ratings"]), album["rym_rating"])
    else:
        scores["rating"] = NOVELTY_FACTOR

    # Final weighted score using Lute's repeat trick
    flat = []
    for key, w in weights.items():
        if key in scores:
            flat.extend([scores[key]] * w)
    final_score = mean(flat) if flat else 0.0

    return {
        "album_id": album_id,
        "scores": scores,
        "final_score": round(final_score, 4),
        "has_tags": bool(album_tags),
    }


def rank_albums(album_ids, profile=None, weights=None):
    """Score and rank a list of albums. Returns sorted list, best first."""
    if profile is None:
        profile = load_profile()
        if profile is None:
            profile = compute_profile()
    if profile is None:
        return []

    results = []
    for aid in album_ids:
        result = score_album(aid, profile=profile, weights=weights)
        if result:
            results.append(result)

    results.sort(key=lambda r: r["final_score"], reverse=True)
    return results


def get_recommendations(limit=20, profile=None, exclude_states=None):
    """Get recommended albums from the to-listen queue, ranked by taste match."""
    if exclude_states is None:
        exclude_states = ("rated", "dismissed")

    with db.get_db() as conn:
        placeholders = ",".join("?" for _ in exclude_states)
        albums = conn.execute(
            f"SELECT id FROM albums WHERE state NOT IN ({placeholders})",
            exclude_states
        ).fetchall()

    album_ids = [a["id"] for a in albums]
    ranked = rank_albums(album_ids, profile=profile)
    return ranked[:limit]
