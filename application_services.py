"""Lazy service construction shared by graphical workflow pages."""
import os
from pathlib import Path

from dotenv import load_dotenv

from mealie_client import MealieClient
from runtime_safety import fixture_mode


def configured_mealie_client(output_dir: Path):
    """Called only by an explicit import, tag repair or reconciliation action."""
    if fixture_mode():
        from fixture_services import FixtureMealie
        return FixtureMealie(output_dir)
    load_dotenv()
    url = os.environ.get("MEALIE_URL", "").strip()
    token = os.environ.get("MEALIE_TOKEN", "").strip()
    if not url or not token:
        raise ValueError("Configure MEALIE_URL and MEALIE_TOKEN in .env before importing.")
    return MealieClient(url, token)
