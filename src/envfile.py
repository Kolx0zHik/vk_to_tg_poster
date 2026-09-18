"""Loader for a ``.env`` file located next to the config path.

Secrets (VK API token, Telegram bot token, LLM API key) live only in the
environment, not in ``config.yaml``.  ``load_env_file`` reads a tiny
``KEY=VALUE`` file (no quoting/escaping needed) and only sets variables that
are not already present in ``os.environ`` so an explicit Docker/Compose env
always wins.
"""

import os
from pathlib import Path
from typing import Optional


def env_file_path(config_path: Optional[str]) -> Path:
    """Path to the ``.env`` file sitting next to ``config_path``.

    An explicitly passed ``config_path`` wins over the ``CONFIG_PATH``
    environment variable; the default is ``data/config.yaml``.
    """
    base = config_path or os.environ.get("CONFIG_PATH") or "data/config.yaml"
    config_file = Path(base)
    if config_file.name == ".env":
        return config_file
    return config_file.parent / ".env"


def load_env_file(config_path: Optional[str] = None) -> None:
    """Load ``KEY=VALUE`` pairs from ``<config_dir>/.env`` into the process env.

    Existing environment variables are never overwritten (explicit env wins).
    Missing or unreadable files are ignored silently.
    """
    path = env_file_path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip())