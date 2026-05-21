"""Ollama-powered recommendation and insight features.

Uses a local Ollama LLM to generate:
- Natural language taste profile summaries
- Per-album recommendation explanations
- Weekly listening digests
- Diary entry starters
- Album comparison narratives
- Mood/vibe labels from tags
"""

import json
import logging
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


def _call_ollama(url, model, prompt, max_tokens=200):
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": max_tokens}
    }).encode("utf-8")
    req = Request(
        f"{url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urlopen(req, timeout=300) as resp:
            return json.loads(resp.read()).get("response", "")
    except Exception as e:
        log.warning("Ollama call failed: %s", e)
        return None


def check_connection(ollama_url):
    """Verify Ollama is reachable and return list of available models."""
    try:
        req = Request(f"{ollama_url}/api/tags", method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        log.warning("Ollama connection check failed: %s", e)
        return None


def generate_taste_summary(profile, ollama_url, model="llama3:8b"):
    """Generate a natural language summary of the user's taste profile."""
    if not profile or not profile.get("album_count"):
        return None

    top_tags = sorted(profile["tag_counts"].items(), key=lambda x: x[1], reverse=True)[:15]
    top_genres = sorted(profile["genre_counts"].items(), key=lambda x: x[1], reverse=True)[:10]
    avg_rating = sum(profile["ratings"]) / len(profile["ratings"]) if profile["ratings"] else 0

    prompt = f"""You are a music taste analyst. Based on the following listening data, write a 2-3 sentence personality sketch of this listener's taste. Be specific about genres and vibes, not generic. Write in second person ("You").

Rated albums: {profile['album_count']}
Average rating: {avg_rating:.1f}/5.0
Top genres: {', '.join(f'{g} ({c})' for g, c in top_genres)}
Top descriptors/tags: {', '.join(f'{t} ({c})' for t, c in top_tags)}

Write ONLY the taste summary, nothing else. No bullet points, no headers."""

    return _call_ollama(ollama_url, model, prompt, max_tokens=150)


def generate_explanation(album_info, scores, profile, ollama_url, model="llama3:8b"):
    """Generate a natural language explanation for why an album was recommended."""
    top_user_tags = sorted(profile["tag_counts"].items(), key=lambda x: x[1], reverse=True)[:8]
    top_user_genres = sorted(profile["genre_counts"].items(), key=lambda x: x[1], reverse=True)[:5]

    prompt = f"""In ONE sentence, explain why "{album_info['title']}" by {album_info['artist']} was recommended to this listener.

Album info: genre={album_info.get('genre_bucket', 'unknown')}, tags={album_info.get('genre_tags', '')}, RYM rating={album_info.get('rym_rating', 'N/A')}
Match scores: descriptors={scores.get('descriptors', 0):.0%}, genre={scores.get('primary_genres', 0):.0%}, rating={scores.get('rating', 0):.0%}
Listener's top genres: {', '.join(g for g, _ in top_user_genres)}
Listener's top tags: {', '.join(t for t, _ in top_user_tags)}

Write ONLY one concise sentence. Example: "Strong match on your love of atmospheric shoegaze with melancholic undertones." """

    return _call_ollama(ollama_url, model, prompt, max_tokens=80)


def generate_weekly_digest(listening_data, profile, ollama_url, model="llama3:8b"):
    """Generate a weekly listening digest summarizing recent activity.

    listening_data should contain:
      albums_listened: list of {artist, title, rating, genre_bucket}
      total_listens: int
      new_genres: list of str
      top_rated: {artist, title, rating}
      most_listened_genre: str
    """
    if not listening_data or not listening_data.get("albums_listened"):
        return None

    albums = listening_data["albums_listened"]
    album_list = "\n".join(
        f"- {a['artist']} - {a['title']} ({a.get('rating', 'unrated')}/5, {a.get('genre_bucket', '?')})"
        for a in albums[:15]
    )

    top = listening_data.get("top_rated")
    top_str = f"{top['artist']} - {top['title']} ({top['rating']}/5)" if top else "none rated yet"

    prompt = f"""You are a music journalist writing a brief weekly listening recap for a personal music diary. Write 3-4 sentences in second person ("You") summarizing what this listener heard this week. Be specific about moods, genres, and patterns. Be conversational, not clinical.

This week's albums ({len(albums)} total):
{album_list}

Highest rated: {top_str}
Most listened genre: {listening_data.get('most_listened_genre', 'mixed')}
New genres explored: {', '.join(listening_data.get('new_genres', [])) or 'none'}

Write ONLY the recap paragraph. No headers, no bullet points."""

    return _call_ollama(ollama_url, model, prompt, max_tokens=200)


def generate_diary_starter(album_info, ollama_url, model="llama3:8b"):
    """Generate a diary entry starter/prompt for an album.

    album_info should contain: title, artist, genre_bucket, genre_tags, rym_rating, year
    """
    tags = album_info.get("genre_tags", "") or ""
    rating = album_info.get("rym_rating")
    rating_str = f"{rating}/5" if rating else "not yet rated"

    prompt = f"""Write a 1-2 sentence diary entry starter for someone who just listened to "{album_info['title']}" by {album_info['artist']}. The starter should prompt personal reflection about the listening experience — how it made them feel, what it reminded them of, or what stood out.

Album context: {album_info.get('genre_bucket', 'unknown')} genre, tags: {tags}, year: {album_info.get('year', '?')}, rating: {rating_str}

Write ONLY the starter text. It should feel like the opening of a personal journal entry, not a review. Example: "First time hearing this after years of knowing the name — the opening track immediately transported me to..." """

    return _call_ollama(ollama_url, model, prompt, max_tokens=100)


def generate_album_comparison(album_a, album_b, ollama_url, model="llama3:8b"):
    """Generate a narrative comparison between two albums.

    Each album dict should contain: title, artist, genre_bucket, genre_tags, rym_rating
    """
    prompt = f"""In 2-3 sentences, compare these two albums from a listener's perspective. Focus on how they relate in terms of mood, sound, and what kind of listener would gravitate toward each.

Album A: "{album_a['title']}" by {album_a['artist']} — {album_a.get('genre_bucket', '?')}, tags: {album_a.get('genre_tags', '')}, rating: {album_a.get('rym_rating', 'N/A')}/5
Album B: "{album_b['title']}" by {album_b['artist']} — {album_b.get('genre_bucket', '?')}, tags: {album_b.get('genre_tags', '')}, rating: {album_b.get('rym_rating', 'N/A')}/5

Write ONLY the comparison. No headers or bullet points."""

    return _call_ollama(ollama_url, model, prompt, max_tokens=150)


def generate_vibe_labels(tags, ollama_url, model="llama3:8b"):
    """Generate 2-3 human-friendly vibe/mood labels from a list of genre tags.

    Returns a list of short labels like ["dreamy late-night", "angular post-punk", "warm analog"].
    """
    if not tags:
        return []

    prompt = f"""Given these music tags, generate exactly 3 short vibe labels (2-3 words each) that capture the mood and feeling. Tags: {', '.join(tags[:20])}

Return ONLY a JSON array of strings. Example: ["dreamy late-night", "angular post-punk", "warm analog"]"""

    result = _call_ollama(ollama_url, model, prompt, max_tokens=60)
    if not result:
        return []
    try:
        start = result.find("[")
        end = result.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(result[start:end])
    except (json.JSONDecodeError, ValueError):
        pass
    return []
