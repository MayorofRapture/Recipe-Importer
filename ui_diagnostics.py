"""Opt-in Review counters; durable_writes counts draft-save attempts, not cursor writes."""
from __future__ import annotations

import os
import hashlib
import traceback
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, MutableMapping
from urllib.parse import urlsplit


_COUNTERS = (
    "full_page_executions", "fragment_executions", "catalog_loads",
    "source_hashes", "ledger_reads", "preview_renders", "durable_writes",
)
_CURRENT: ContextVar[MutableMapping[str, int] | None] = ContextVar("ui_diagnostics", default=None)


def bind_session(counters: MutableMapping[str, Any] | None) -> None:
    """Bind this Streamlit execution to its session-owned counters."""
    _CURRENT.set(counters if os.environ.get("RECIPE_IMPORTER_UI_DIAGNOSTICS") == "1" else None)


def count(name: str, amount: int = 1) -> None:
    """Count one operation only when local diagnostics were explicitly enabled."""
    counters = _CURRENT.get()
    if counters is None or name not in _COUNTERS:
        return
    counters[name] = counters.get(name, 0) + amount
    if name == "ledger_reads":
        trace = counters.setdefault("ledger_read_traces", [])
        if len(trace) < 30:
            frames = traceback.extract_stack(limit=10)[:-1]
            trace.append([f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
                          for frame in frames if Path(frame.filename).name != "ui_diagnostics.py"])


def new_counters() -> dict[str, Any]:
    """Create an empty session-owned counter map."""
    return {**{key: 0 for key in _COUNTERS}, "ledger_read_traces": []}


@lru_cache(maxsize=1)
def build_marker() -> str:
    """Read checkout/source identity once per process without spawning Git."""
    root = Path(__file__).resolve().parent
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            head = (root / ".git" / head[5:]).read_text(encoding="utf-8").strip()
        revision = head[:12]
    except OSError:
        revision = "no-git-head"
    digest = hashlib.sha256()
    for relative in ("streamlit_app.py", "app_pages/common.py", "app_pages/review.py",
                     "ui_data.py", "review_store.py", "import_recipes.py", "ui_diagnostics.py"):
        try:
            digest.update((root / relative).read_bytes())
        except OSError:
            digest.update(relative.encode("utf-8"))
    return f"{revision} · source {digest.hexdigest()[:12]}"


def runtime_marker(browser_url: str | None) -> str:
    """Identify the served checkout, process and browser port in local diagnostics."""
    try:
        port = urlsplit(browser_url or "").port
    except ValueError:
        port = None
    return f"{build_marker()} · pid {os.getpid()} · port {port or 'unknown'}"
