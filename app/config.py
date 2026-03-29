"""Configuration loader - reads config.yaml and .env."""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_config: Optional[dict] = None

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config() -> dict[str, Any]:
    global _config
    if _config is not None:
        return _config
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {CONFIG_PATH}. "
            "Copy config.yaml.example to config.yaml and fill in your values."
        )
    with open(CONFIG_PATH) as f:
        _config = yaml.safe_load(f)
    return _config


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
