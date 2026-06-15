"""Per-user SEED_PAGES, sourced from env vars set by the Tauri sidecar spawn.

Replaces the hardcoded `SEED_PAGES` dict in `backend/longterm_db.py` after
`prepare_backend.py` swaps the original assignment for `from .longterm_db_patch import SEED_PAGES`.
"""
from __future__ import annotations

import os

_DOMAINS = ("work", "research", "intellectual", "personal", "index")


def _load() -> dict[str, str]:
    out: dict[str, str] = {}
    missing: list[str] = []
    for domain in _DOMAINS:
        var = f"NOTION_PAGE_{domain.upper()}"
        val = os.environ.get(var, "").strip()
        if not val:
            missing.append(var)
        out[domain] = val
    if missing:
        raise RuntimeError(
            f"Reggia: missing Notion page env vars: {', '.join(missing)}. "
            "Run the first-run wizard, or set them manually if launching the backend directly."
        )
    return out


SEED_PAGES: dict[str, str] = _load()
