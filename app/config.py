"""Configuration loader - reads config.yaml and .env.

On first load, Pydantic models validate critical sections (currently
``kalshi:``) so typos and bad values are caught at startup.
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_config: Optional[dict] = None
_validated_kalshi = None  # cached KalshiConfig model

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

logger = logging.getLogger("bot.config")


def load_config() -> dict[str, Any]:
    global _config, _validated_kalshi
    if _config is not None:
        return _config
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {CONFIG_PATH}. "
            "Copy config.yaml.example to config.yaml and fill in your values."
        )
    with open(CONFIG_PATH) as f:
        _config = yaml.safe_load(f)

    # Validate Kalshi config at startup
    kalshi_raw = _config.get("kalshi")
    if kalshi_raw:
        try:
            from app.models import validate_kalshi_config
            _validated_kalshi = validate_kalshi_config(kalshi_raw)
            logger.info(f"Kalshi config validated OK (mode={_validated_kalshi.mode})")
        except Exception as e:
            logger.error(f"Kalshi config validation FAILED: {e}")
            raise

    return _config


def get_kalshi_config():
    """Return the validated KalshiConfig model (or None if kalshi not configured)."""
    global _validated_kalshi
    if _validated_kalshi is None:
        load_config()
    return _validated_kalshi


def get(section: str, key: Optional[str] = None, default: Any = None) -> Any:
    cfg = load_config()
    sec = cfg.get(section, {})
    if key is None:
        return sec
    return sec.get(key, default)


def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def reload_config() -> dict[str, Any]:
    global _config
    _config = None
    return load_config()
