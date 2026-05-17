import os
from pathlib import Path

import yaml


def load_config(path=None):
    if path is None:
        path = os.environ.get("EARWRYM_CONFIG", "/data/config.yaml")
    config_path = Path(path)
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "config.example.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg):
    """Allow env vars to override config values for Docker deployments."""
    overrides = {
        "EARWRYM_LB_USERNAME": ("listenbrainz", "username"),
        "EARWRYM_RYM_USERNAME": ("rym", "username"),
        "EARWRYM_RYM_PROXY_URL": ("rym", "proxy_url"),
        "EARWRYM_NAVIDROME_URL": ("navidrome", "url"),
        "EARWRYM_NAVIDROME_USER": ("navidrome", "username"),
        "EARWRYM_NAVIDROME_PASS": ("navidrome", "password"),
        "EARWRYM_LIDARR_URL": ("lidarr", "url"),
        "EARWRYM_LIDARR_API_KEY": ("lidarr", "api_key"),
        "EARWRYM_1001_SLUG": ("one_thousand_one_albums", "project_slug"),
        "EARWRYM_HC_PING_URL": ("healthchecks", "ping_url"),
    }
    for env_key, (section, field) in overrides.items():
        val = os.environ.get(env_key)
        if val:
            cfg.setdefault(section, {})[field] = val
