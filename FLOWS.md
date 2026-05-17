# Earwrym — Complete Flow Analysis

## Mermaid Diagram

```mermaid
flowchart TD
    subgraph triggers["TRIGGERS"]
        W["User adds to RYM Wishlist"]
        S["User scrobbles track (LB)"]
        R["User rates album on RYM"]
        F["Friend requests via Musicseerr"]
        D["1001 Albums daily"]
        L["User manually monitors in Lidarr"]
    end

    subgraph rym_watcher["RYM-WATCHER (poller, every 2h)"]
        RW1["Check /collection/cassywassy/recent/"]
        RW2["Check /collection/cassywassy/wishlist"]
        RW1 -->|new ratings| RW3["POST /api/import-ratings"]
        RW2 -->|new items since last_slug| RW4["POST /api/import-wishlist"]
    end

    subgraph earwrym["EARWRYM (scheduler)"]
        direction TB
        PL["poll_listens (5 min)"]
        MC["match_cached_ratings (2h)"]
        LI["check_lidarr_imports (30 min)"]
        AL["check_1001_albums (daily)"]
    end

    subgraph lidarr_sys["LIDARR"]
        LID_SEARCH["Search + Add artist"]
        LID_MONITOR["Monitor album + search"]
        LID_DL["Download completes"]
        LID_HISTORY["History: trackFileImported"]
    end

    subgraph approval["MUSIC-APPROVAL PROXY (LXC 111)"]
        AP_QUEUE["Pending queue"]
        AP_APPROVE["Admin approves"]
        AP_DENY["Admin denies"]
    end

    subgraph navidrome["NAVIDROME"]
        ND_INDEX["Album indexed in library"]
        ND_SCROBBLE["Scrobble sent to LB"]
    end

    subgraph states["EARWRYM ALBUM STATES"]
        TL["TO-LISTEN"]
        LIS["LISTENING"]
        UNR["LISTENED-UNRATED"]
        RAT["RATED"]
        DIS["DISMISSED"]
    end

    %% === WISHLIST FLOW ===
    W --> RW2
    RW4 -->|"Insert rym_wishlist_cache"| WL_CHECK{"Already in cache?"}
    WL_CHECK -->|yes| WL_SKIP["Skip"]
    WL_CHECK -->|no| WL_LIDARR{"In Lidarr library?"}
    WL_LIDARR -->|yes| WL_MARK["Mark sent_to_lidarr=1"]
    WL_LIDARR -->|no| LID_SEARCH
    LID_SEARCH --> LID_MONITOR --> LID_DL

    %% === FRIEND REQUEST FLOW ===
    F -->|"musicseerr.grayson.cat"| AP_QUEUE
    AP_QUEUE --> AP_APPROVE
    AP_APPROVE -->|"Monitor album in Lidarr"| LID_MONITOR
    AP_QUEUE --> AP_DENY

    %% === USER DIRECT LIDARR ===
    L -->|"admin direct"| LID_MONITOR

    %% === LIDARR DOWNLOAD → TO-LISTEN ===
    LID_DL --> LID_HISTORY
    LID_HISTORY --> LI
    LI --> LI_DUPE{"_find_duplicate?"}
    LI_DUPE -->|"exists"| LI_SKIP["Skip"]
    LI_DUPE -->|"no"| LI_RATED{"_is_rated_on_rym?"}
    LI_RATED -->|"yes"| LI_SKIP
    LI_RATED -->|"no"| LI_ND{"In Navidrome? Passes filter?"}
    LI_ND -->|"no"| LI_SKIP
    LI_ND -->|"yes"| TL

    %% === SCROBBLE FLOW ===
    S --> ND_SCROBBLE --> PL
    PL --> PL_ND{"In Navidrome? Passes filter?"}
    PL_ND -->|"no"| PL_SKIP["Skip"]
    PL_ND -->|"yes"| PL_EXIST{"Existing album?"}
    PL_EXIST -->|"rated/dismissed"| PL_SKIP
    PL_EXIST -->|"no, check RYM"| PL_RYM{"_is_rated_on_rym?"}
    PL_RYM -->|"yes"| RAT
    PL_RYM -->|"no"| PL_CREATE["Create as LISTENING"]
    PL_CREATE --> LIS
    PL_EXIST -->|"yes, active"| PL_UPDATE["Record listen, update completion"]
    PL_UPDATE --> PL_COMP{"Completion >= 80%?"}
    PL_COMP -->|"yes"| UNR
    PL_COMP -->|"no"| LIS

    %% === RATING FLOW ===
    R --> RW1
    RW3 -->|"Insert rym_ratings_cache"| MC
    MC --> MC_FIND{"Album in tracker?"}
    MC_FIND -->|"no"| MC_SKIP["Skip (not tracked)"]
    MC_FIND -->|"yes, not rated/dismissed"| RAT

    %% === 1001 ALBUMS ===
    D --> AL
    AL --> AL_EXIST{"Already exists?"}
    AL_EXIST -->|"yes"| AL_SKIP["Skip"]
    AL_EXIST -->|"no, not in ND"| AL_LIDARR["Request via Lidarr"]
    AL_EXIST -->|"no"| TL

    %% === STATE TRANSITIONS ===
    TL -.->|"user scrobbles"| LIS
    UNR -.->|"user rates on RYM"| RAT
    TL -.->|"user dismisses"| DIS
    UNR -.->|"user dismisses"| DIS
    LIS -.->|"user dismisses"| DIS

    %% === PROBLEM HIGHLIGHT ===
    AP_APPROVE -.->|"BUG: same Lidarr path"| LID_DL
```

## Problems Identified

### CRITICAL: Friend requests appear in to-listen queue

**Flow**: Friend requests on Musicseerr → approved → Lidarr monitors → downloads → `check_lidarr_imports()` picks it up → adds to to-listen.

**Why**: `check_lidarr_imports()` monitors ALL Lidarr history events. It cannot distinguish between:
- Albums the user requested (via RYM wishlist or direct Lidarr)  
- Albums friends requested (via Musicseerr → approval proxy)

**Current guards** (insufficient for this case):
- `_find_duplicate()` — only blocks if album already tracked
- `_is_rated_on_rym()` — only blocks if user already rated it
- `passes_filter()` — only blocks < 4 tracks / < 8 min

A friend requesting "Random Artist - Random Album" that the user has never heard of WILL appear in their to-listen queue.

### Proposed Fix

Only add Lidarr imports to to-listen if **at least one** of:
1. Album is in `rym_wishlist_cache` (user explicitly wishlisted it)
2. Artist has other albums rated by user in `rym_ratings_cache` (user cares about this artist)
3. Album source tag = "1001albums" (from daily generator)

This filters out pure friend requests while keeping the user's own pipeline working.

### MINOR: Double-entry risk for wishlist → Lidarr → to-listen

**Flow**: User wishlists album → poller sends to Lidarr → Lidarr downloads → `check_lidarr_imports` adds to to-listen → Meanwhile, Navidrome indexes it → `poll_listens` might also try to create it.

**Guards**: `_find_duplicate()` should catch this since both paths check artist+title. Working correctly.

### MINOR: Completion estimation from backfill is unreliable

**Issue**: LB stats report total listens (including repeated tracks), not unique tracks heard. Backfill divided total_listens / track_count, which overestimates completion (e.g., Tool albums marked "listened-unrated" when user only heard a few tracks repeatedly).

**Guard**: User can dismiss these. Going forward, real-time polling uses `COUNT(DISTINCT track_name)` which is accurate.
