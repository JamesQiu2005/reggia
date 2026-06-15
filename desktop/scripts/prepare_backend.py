"""Copy + patch the backend for bundling.

Inputs are read from the repo and never modified. Outputs:

  build/backend_patched/                copy of ../backend/ with libcst patches applied
    longterm_db.py                      SEED_PAGES dict replaced with env-var import
    main.py                             StaticFiles directory now reads REGGIA_FRONTEND_DIR
    longterm_db_patch.py                NEW: env-var-driven SEED_PAGES (copied from src-launcher/)
  src-tauri/resources/compose/docker-compose.yml
                                        copy of ../docker-compose.yml with workspace path
                                        swapped to ${REGGIA_WORKSPACE} and `build: .` swapped
                                        for `image: reggia/cc:0.1.0`
  src-tauri/resources/chat_workspace_seed/
                                        copy of ../backend/chat_workspace/ for first-run seeding

Run from `desktop/`:
    uv run --with libcst python scripts/prepare_backend.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import libcst as cst

DESKTOP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = DESKTOP_DIR.parent
BACKEND_SRC = REPO_DIR / "backend"
BACKEND_DST = DESKTOP_DIR / "build" / "backend"
COMPOSE_SRC = REPO_DIR / "docker-compose.yml"
COMPOSE_DST = DESKTOP_DIR / "src-tauri" / "resources" / "compose" / "docker-compose.yml"
WORKSPACE_SRC = BACKEND_SRC / "chat_workspace"
WORKSPACE_DST = DESKTOP_DIR / "src-tauri" / "resources" / "chat_workspace_seed"
PATCH_MODULE_SRC = DESKTOP_DIR / "src-launcher" / "longterm_db_patch.py"


class SeedPagesReplacer(cst.CSTTransformer):
    """Replace `SEED_PAGES: ... = { ... }` with `from .longterm_db_patch import SEED_PAGES`."""

    def __init__(self) -> None:
        self.found = False

    def leave_SimpleStatementLine(
        self, original: cst.SimpleStatementLine, updated: cst.SimpleStatementLine
    ) -> cst.BaseStatement:
        if len(updated.body) != 1:
            return updated
        stmt = updated.body[0]
        target_name: str | None = None
        if isinstance(stmt, cst.Assign) and len(stmt.targets) == 1:
            t = stmt.targets[0].target
            if isinstance(t, cst.Name):
                target_name = t.value
        elif isinstance(stmt, cst.AnnAssign) and isinstance(stmt.target, cst.Name):
            target_name = stmt.target.value
        if target_name != "SEED_PAGES":
            return updated
        self.found = True
        return cst.SimpleStatementLine(
            body=[
                cst.ImportFrom(
                    module=cst.Name("longterm_db_patch"),
                    names=[cst.ImportAlias(name=cst.Name("SEED_PAGES"))],
                    relative=[cst.Dot()],
                )
            ]
        )


class StaticFilesDirReplacer(cst.CSTTransformer):
    """Replace `directory="frontend"` with `directory=os.environ.get("REGGIA_FRONTEND_DIR", "frontend")`."""

    def __init__(self) -> None:
        self.found = False
        self.os_imported = False

    def visit_Module(self, node: cst.Module) -> None:
        for stmt in node.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for s in stmt.body:
                    if isinstance(s, cst.Import):
                        for alias in s.names:
                            if isinstance(alias.name, cst.Name) and alias.name.value == "os":
                                self.os_imported = True

    def leave_Arg(self, original: cst.Arg, updated: cst.Arg) -> cst.Arg:
        if (
            updated.keyword is not None
            and isinstance(updated.keyword, cst.Name)
            and updated.keyword.value == "directory"
            and isinstance(updated.value, cst.SimpleString)
            and updated.value.evaluated_value == "frontend"
        ):
            self.found = True
            return updated.with_changes(
                value=cst.parse_expression(
                    'os.environ.get("REGGIA_FRONTEND_DIR", "frontend")'
                )
            )
        return updated


def patch_file(path: Path, transformer: cst.CSTTransformer, what: str) -> None:
    source = path.read_text()
    tree = cst.parse_module(source)
    modified = tree.visit(transformer)
    if not transformer.found:
        print(f"ERROR: {what} not found in {path}", file=sys.stderr)
        sys.exit(1)
    path.write_text(modified.code)
    print(f"  patched {what} in {path.relative_to(DESKTOP_DIR)}")


def patch_writable_paths() -> None:
    """Redirect SQLite + log directories to a writable per-user location.

    backend/db.py, backend/longterm_db.py, backend/sessions.py, and the
    LOG_DIR line in backend/main.py all build paths from `__file__`, which in
    a PyInstaller bundle points inside the read-only extraction dir. We rewrite
    these to honor `REGGIA_DATA_DIR` (set by the Tauri sidecar / standalone
    launcher), falling back to the source-tree-relative path so the dev
    workflow keeps working when prepare_backend.py is rerun against a fresh
    copy.
    """
    targets = [
        (
            BACKEND_DST / "db.py",
            'DB_DIR = Path(__file__).resolve().parent / "databases"',
            'DB_DIR = (Path(os.environ["REGGIA_DATA_DIR"]) if os.environ.get("REGGIA_DATA_DIR") else Path(__file__).resolve().parent) / "databases"',
        ),
        (
            BACKEND_DST / "longterm_db.py",
            'DB_DIR = Path(__file__).resolve().parent / "databases"',
            'DB_DIR = (Path(os.environ["REGGIA_DATA_DIR"]) if os.environ.get("REGGIA_DATA_DIR") else Path(__file__).resolve().parent) / "databases"',
        ),
        (
            BACKEND_DST / "sessions.py",
            'LOG_DIR = Path(__file__).resolve().parent / "logs"',
            'LOG_DIR = (Path(os.environ["REGGIA_DATA_DIR"]) if os.environ.get("REGGIA_DATA_DIR") else Path(__file__).resolve().parent) / "logs"',
        ),
        (
            BACKEND_DST / "main.py",
            'LOG_DIR = BASE_DIR / "logs"',
            'LOG_DIR = (Path(os.environ["REGGIA_DATA_DIR"]) / "logs" if os.environ.get("REGGIA_DATA_DIR") else BASE_DIR / "logs")',
        ),
    ]
    for path, old, new in targets:
        src = path.read_text()
        if old not in src:
            print(f"ERROR: writable-path pattern not found in {path}", file=sys.stderr)
            sys.exit(1)
        src = src.replace(old, new)
        src = _ensure_import_os(src)
        path.write_text(src)
        print(f"  patched writable path in {path.relative_to(DESKTOP_DIR)}")


def _ensure_import_os(src: str) -> str:
    """Insert `import os` if absent. Goes AFTER any `from __future__` line
    (which Python requires to come first) and otherwise at the top."""
    import re

    if re.search(r"(?m)^import os(?:\s|$)", src) or re.search(r"(?m)^from os ", src):
        return src
    lines = src.splitlines(keepends=True)
    last_future_idx = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("from __future__"):
            last_future_idx = i
    insert_at = last_future_idx + 1 if last_future_idx >= 0 else 0
    lines.insert(insert_at, "import os\n")
    return "".join(lines)


def patch_compose() -> None:
    """Two surgical text edits to the docker-compose.yml copy.

    YAML is preserved as text (not round-tripped through a YAML lib) so formatting,
    comments, and key ordering stay identical except for the two lines we change.
    """
    source = COMPOSE_SRC.read_text()
    # Line 6: ./backend/chat_workspace -> ${REGGIA_WORKSPACE}
    workspace_line = "./backend/chat_workspace:/workspace"
    if workspace_line not in source:
        print(f"ERROR: '{workspace_line}' not found in {COMPOSE_SRC}", file=sys.stderr)
        sys.exit(1)
    patched = source.replace(workspace_line, "${REGGIA_WORKSPACE}:/workspace")
    # Replace `build: .` with `image: reggia/cc:0.1.0`. Keep indentation.
    build_line_old = "build: ."
    build_line_new = "image: reggia/cc:0.1.0"
    if build_line_old not in patched:
        print(f"ERROR: '{build_line_old}' not found in {COMPOSE_SRC}", file=sys.stderr)
        sys.exit(1)
    patched = patched.replace(build_line_old, build_line_new)
    COMPOSE_DST.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_DST.write_text(patched)
    print(f"  wrote {COMPOSE_DST.relative_to(DESKTOP_DIR)}")


def main() -> None:
    print("Preparing bundled backend...")
    if BACKEND_DST.exists():
        shutil.rmtree(BACKEND_DST)
    shutil.copytree(BACKEND_SRC, BACKEND_DST, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "databases", "logs", ".env",
    ))
    print(f"  copied backend/ -> {BACKEND_DST.relative_to(DESKTOP_DIR)}")

    patch_file(BACKEND_DST / "longterm_db.py", SeedPagesReplacer(), "SEED_PAGES")
    patch_file(BACKEND_DST / "main.py", StaticFilesDirReplacer(), 'directory="frontend"')

    shutil.copy(PATCH_MODULE_SRC, BACKEND_DST / "longterm_db_patch.py")
    print(f"  copied longterm_db_patch.py into bundled backend")

    print("Redirecting writable paths to REGGIA_DATA_DIR...")
    patch_writable_paths()

    print("Generating bundled docker-compose.yml...")
    patch_compose()

    print("Seeding chat_workspace/...")
    if WORKSPACE_DST.exists():
        shutil.rmtree(WORKSPACE_DST)
    shutil.copytree(WORKSPACE_SRC, WORKSPACE_DST)
    print(f"  copied chat_workspace/ -> {WORKSPACE_DST.relative_to(DESKTOP_DIR)}")

    print("Done. Bundled backend ready at build/backend/.")


if __name__ == "__main__":
    main()
