"""Shared Gemini configuration lookup for the app and background workers."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ENV_FILE = Path(__file__).resolve().parent / ".env"


def gemini_api_key(env_file: Path | str | None = None) -> str:
    """Load project configuration without replacing environment overrides."""
    load_dotenv(dotenv_path=Path(env_file) if env_file else PROJECT_ENV_FILE, override=False)
    return os.environ.get("GEMINI_API_KEY", "").strip()
