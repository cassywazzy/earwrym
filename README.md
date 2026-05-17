# Earwrym

Music listening tracker that bridges ListenBrainz scrobbles, RateYourMusic ratings, and Navidrome playlists.

**What it does:**
- Watches your ListenBrainz scrobbles and tracks album completion (% of tracks heard)
- Manages a review queue: fully-listened albums waiting for your RYM rating
- Auto-detects when you rate on RYM and archives the album
- Syncs your RYM wishlist to Lidarr (forward-only, like a watchlist auto-requester)
- Sorts new albums into genre playlists in Navidrome, prunes on rate
- Optional 1001 Albums Generator integration (daily album challenge)
- Year-in-review stats page

## Quick Start

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials

docker compose up -d
```

Open `http://localhost:8587`

## Configuration

All settings in `config.yaml`. See `config.example.yaml` for the full reference.

**Required:**
- `listenbrainz.username` — your ListenBrainz username
- `navidrome.url`, `.username`, `.password` — your Navidrome instance

**Optional modules (enable/disable in config):**
- `rym` — RateYourMusic profile scraping (ratings + wishlist)
- `lidarr` — auto-request wishlist albums
- `one_thousand_one_albums` — daily album challenge from 1001albumsgenerator.com

Environment variables can override config values for containerized deployments (see `.env.example`).

## Album Lifecycle

```
TO-LISTEN → LISTENING → LISTENED & UNRATED → RATED
```

| State | Entry | Exit |
|-------|-------|------|
| To-Listen | Wishlist sync, 1001 Albums, recommendation "Add" | First scrobble detected |
| Listening | LB scrobble of a tracked album | ≥80% tracks heard |
| Listened & Unrated | Completion threshold met | RYM rating detected |
| Rated | RYM scraper finds rating | Terminal |

## Album Filter

Only tracks albums that:
- Exist in your Navidrome library (downloaded, not just scrobbled)
- Have >3 tracks OR >8 minutes total runtime (filters out singles)

## Genre Playlists

Albums are auto-sorted into Navidrome playlists by genre using MusicBrainz tags mapped to user-defined buckets. Default buckets: Metal/Heavy, Electronic, Hip-Hop, Indie/Alt, Dreampop/Shoegaze, Experimental, Pop/R&B, Other.

Override per-album in the UI. Albums are pruned from playlists when rated.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/healthcheck` | GET | Health check |
| `/api/refresh` | POST | Trigger immediate RYM scrape |
| `/api/backfill` | POST | Run historical backfill from LB |
| `/api/album/<id>/delete` | POST | Remove album from queue |
| `/api/album/<id>/reorder` | POST | Change sort order (body: `{position: N}`) |
| `/api/album/<id>/move` | POST | Change state (body: `{state: "..."}`) |
| `/api/album/<id>/bucket` | POST | Change genre bucket (body: `{bucket: "..."}`) |

## Screenshots

*TODO: Add screenshots after deployment*

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export EARWRYM_CONFIG=config.yaml EARWRYM_DB=./earwrym.db
python -m earwrym
```

## License

MIT
