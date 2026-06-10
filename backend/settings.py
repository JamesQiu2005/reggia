"""User-facing settings: .env mediated profile + API keys, avatar storage,
and template rendering for the chat workspace (CLAUDE.md, skill files).

Single-user local app — no auth. All key reveals/edits go through POST so
nothing sensitive lands in URLs or server access logs.
"""

import base64
import binascii
import os
from pathlib import Path
from typing import Optional

from dotenv import set_key
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
AVATAR_DIR = BASE_DIR / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

CHAT_WORKSPACE = BASE_DIR / "chat_workspace"
# Only CLAUDE.md is templated. The `reggia` skill at .claude/skills/reggia.md
# is a fixed file — its YAML frontmatter must remain stable so Claude Code can
# auto-discover the skill, and it carries no user-specific content.
TEMPLATE_TARGETS = [
    (CHAT_WORKSPACE / "CLAUDE.md.template", CHAT_WORKSPACE / "CLAUDE.md"),
]

MANAGED_FIELDS = {
    "user_name": "USER_NAME",
    "display_name": "DISPLAY_NAME",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "notion_api_key": "NOTION_API_KEY",
    "avatar_file": "AVATAR_FILE",
}

SECRET_FIELDS = {"deepseek_api_key", "notion_api_key"}
REQUIRED_FOR_ONBOARDING = ["user_name", "deepseek_api_key", "notion_api_key"]

ALLOWED_AVATAR_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_AVATAR_BYTES = 4 * 1024 * 1024


def _env_get(key: str) -> str:
    return os.environ.get(key, "").strip()


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def _avatar_path() -> Optional[Path]:
    fname = _env_get("AVATAR_FILE")
    if not fname:
        return None
    p = AVATAR_DIR / fname
    return p if p.exists() else None


def _missing() -> list[str]:
    return [f for f in REQUIRED_FOR_ONBOARDING if not _env_get(MANAGED_FIELDS[f])]


def _write_env(field: str, value: str) -> None:
    """Persist to .env and to the live process environment.

    `quote_mode="auto"` adds quotes only when a value contains a character that
    would break shell `source backend/.env` (e.g. a display name with spaces).
    """
    env_key = MANAGED_FIELDS[field]
    set_key(str(ENV_PATH), env_key, value, quote_mode="auto")
    if value:
        os.environ[env_key] = value
    else:
        os.environ.pop(env_key, None)


def render_chat_workspace() -> None:
    """Materialize CLAUDE.md and skill files from their .template siblings,
    substituting {USER_NAME}. Idempotent — safe to call on every boot.
    """
    user_name = _env_get("USER_NAME") or "the user"
    for template, target in TEMPLATE_TARGETS:
        if not template.exists():
            continue
        rendered = template.read_text(encoding="utf-8").replace("{USER_NAME}", user_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")


def public_snapshot() -> dict:
    avatar_file = _env_get("AVATAR_FILE")
    return {
        "user_name": _env_get("USER_NAME"),
        "display_name": _env_get("DISPLAY_NAME") or _env_get("USER_NAME"),
        "avatar_url": f"/settings/avatar?file={avatar_file}" if avatar_file and _avatar_path() else None,
        "deepseek_api_key_set": bool(_env_get("DEEPSEEK_API_KEY")),
        "deepseek_api_key_masked": _mask(_env_get("DEEPSEEK_API_KEY")),
        "notion_api_key_set": bool(_env_get("NOTION_API_KEY")),
        "notion_api_key_masked": _mask(_env_get("NOTION_API_KEY")),
        "needs_onboarding": len(_missing()) > 0,
        "missing": _missing(),
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings():
    return public_snapshot()


@router.get("/onboarding")
async def onboarding():
    missing = _missing()
    return {"needs_onboarding": bool(missing), "missing": missing}


@router.post("")
async def update_settings(payload: dict):
    """Partial update of any managed field. Sensitive keys are accepted via
    POST body only — never via query params.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    touched_user_name = False
    for field, value in payload.items():
        if field not in MANAGED_FIELDS or field == "avatar_file":
            # avatar_file is managed through /settings/avatar only
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{field} must be a string")
        clean = value.strip()
        # For secrets, allow setting to "" only via an explicit unset flag — not
        # via accidental empty submission.
        if field in SECRET_FIELDS and clean == "":
            continue
        _write_env(field, clean)
        if field == "user_name":
            touched_user_name = True

    if touched_user_name:
        render_chat_workspace()

    return public_snapshot()


@router.post("/reveal")
async def reveal(payload: dict):
    field = (payload.get("field") or "").strip()
    if field not in SECRET_FIELDS:
        raise HTTPException(status_code=400, detail="unsupported field")
    return {"field": field, "value": _env_get(MANAGED_FIELDS[field])}


@router.post("/avatar")
async def upload_avatar(payload: dict):
    """Accept a base64-encoded image. JSON body avoids needing python-multipart
    and keeps the request body symmetric with the other settings calls.

    payload: {"mime": "image/png", "data": "<base64>"}
    """
    mime = (payload.get("mime") or "").strip().lower()
    data_b64 = payload.get("data") or ""
    if mime not in ALLOWED_AVATAR_MIME:
        raise HTTPException(status_code=400, detail="unsupported image type")
    # Strip a data URL prefix if present.
    if isinstance(data_b64, str) and data_b64.startswith("data:"):
        _, _, data_b64 = data_b64.partition(",")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64 data")
    if len(raw) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="image too large (max 4MB)")

    ext = ALLOWED_AVATAR_MIME[mime]
    # Remove any pre-existing avatar so we never accumulate orphan files.
    for old in AVATAR_DIR.glob("avatar.*"):
        try:
            old.unlink()
        except OSError:
            pass
    fname = f"avatar{ext}"
    (AVATAR_DIR / fname).write_bytes(raw)
    _write_env("avatar_file", fname)
    return public_snapshot()


@router.delete("/avatar")
async def delete_avatar():
    for old in AVATAR_DIR.glob("avatar.*"):
        try:
            old.unlink()
        except OSError:
            pass
    _write_env("avatar_file", "")
    return public_snapshot()


@router.get("/avatar")
async def get_avatar(file: str = ""):
    # `file` is supplied by the snapshot URL; ignored here but kept so the
    # browser treats different versions as different URLs (cache-bust on swap).
    _ = file
    p = _avatar_path()
    if not p:
        raise HTTPException(status_code=404, detail="no avatar")
    return FileResponse(p)
