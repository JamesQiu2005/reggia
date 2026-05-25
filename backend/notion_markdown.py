"""Bidirectional converter between Notion blocks and Markdown.

Pure functions — no HTTP, no DB. Unsupported block types round-trip via a
caller-provided passthrough store so human-added images/embeds/etc. survive
a pull/push cycle without being silently dropped.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable, Optional

# Block types we render natively. Everything else goes to passthrough.
SUPPORTED_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "quote", "code",
}

PASSTHROUGH_MARKER_RE = re.compile(r"<!--\s*reggia:block:([0-9a-f-]{8,})\s*-->")

PassthroughWriter = Callable[[str, dict], None]
PassthroughReader = Callable[[str], Optional[dict]]


# ---------------------------------------------------------------------------
# Notion → Markdown
# ---------------------------------------------------------------------------

def _rich_to_md(rich_text: list) -> str:
    """Convert a Notion rich_text array into Markdown text with inline marks."""
    out = []
    for span in rich_text or []:
        text = span.get("plain_text", "")
        if not text:
            continue
        ann = span.get("annotations", {}) or {}
        href = span.get("href")
        # Apply marks from innermost to outermost. Order matters for nesting.
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("bold"):
            text = f"**{text}**"
        if href:
            # If the inner text already includes formatting marks, wrap them.
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _block_to_md(block: dict, indent: int, passthrough_writer: PassthroughWriter) -> list[str]:
    """Render one block (and its children) to a list of Markdown lines."""
    btype = block.get("type", "")
    pad = "  " * indent
    lines: list[str] = []

    if btype in ("paragraph", "heading_1", "heading_2", "heading_3", "quote"):
        text = _rich_to_md(block.get(btype, {}).get("rich_text", []))
        prefix = {
            "paragraph": "",
            "heading_1": "# ",
            "heading_2": "## ",
            "heading_3": "### ",
            "quote": "> ",
        }[btype]
        # Preserve blank paragraphs as a single blank line so the round-trip
        # doesn't collapse intentional spacing.
        if text == "" and btype == "paragraph":
            lines.append("")
        else:
            lines.append(f"{pad}{prefix}{text}")

    elif btype == "bulleted_list_item":
        text = _rich_to_md(block.get(btype, {}).get("rich_text", []))
        lines.append(f"{pad}- {text}")

    elif btype == "numbered_list_item":
        text = _rich_to_md(block.get(btype, {}).get("rich_text", []))
        # Always emit "1." — Markdown renderers auto-renumber.
        lines.append(f"{pad}1. {text}")

    elif btype == "code":
        code_info = block.get("code", {})
        text = "".join(s.get("plain_text", "") for s in code_info.get("rich_text", []))
        lang = code_info.get("language", "") or ""
        # Normalize Notion's "plain text" → empty fence language.
        if lang == "plain text":
            lang = ""
        lines.append(f"{pad}```{lang}")
        for line in text.split("\n"):
            lines.append(f"{pad}{line}")
        lines.append(f"{pad}```")

    else:
        # Unsupported — emit a marker comment and stash the raw block.
        marker = uuid.uuid4().hex[:12]
        stored = {k: v for k, v in block.items()
                  if k not in ("id", "created_time", "created_by",
                               "last_edited_time", "last_edited_by",
                               "parent", "archived", "in_trash",
                               "has_children")}
        passthrough_writer(marker, stored)
        lines.append(f"{pad}<!-- reggia:block:{marker} -->")

    # Children (Notion provides them inline if we attached them; see sync.py).
    children = block.get("_children") or []
    for child in children:
        lines.extend(_block_to_md(child, indent + 1, passthrough_writer))

    return lines


def blocks_to_markdown(blocks: list, passthrough_writer: PassthroughWriter) -> str:
    """Convert Notion block list (with `_children` attached) to Markdown.

    `passthrough_writer(marker_id, block_json)` is invoked for each
    unsupported block so the caller can persist it for the inverse trip.
    """
    out: list[str] = []
    prev_kind: Optional[str] = None
    for block in blocks:
        kind = block.get("type", "")
        # Blank line between blocks, except keep consecutive list items together.
        if out:
            same_list = (
                prev_kind in ("bulleted_list_item", "numbered_list_item")
                and kind == prev_kind
            )
            if not same_list:
                out.append("")
        out.extend(_block_to_md(block, 0, passthrough_writer))
        prev_kind = kind
    # Ensure trailing newline.
    return "\n".join(out).rstrip() + "\n" if out else ""


# ---------------------------------------------------------------------------
# Markdown → Notion
# ---------------------------------------------------------------------------

# Inline tokenizers. Tried in order; first match at the cursor wins.
_INLINE_PATTERNS = [
    ("code",   re.compile(r"`([^`\n]+)`")),
    ("link",   re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")),
    ("bold",   re.compile(r"\*\*([^*\n]+)\*\*")),
    ("italic", re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")),
    ("strike", re.compile(r"~~([^~\n]+)~~")),
]


def _md_to_rich(text: str) -> list:
    """Tokenize Markdown inline marks into a Notion rich_text array."""
    spans: list[dict] = []
    plain: list[str] = []
    i = 0

    def _flush_plain():
        if plain:
            spans.append({
                "type": "text",
                "text": {"content": "".join(plain)},
                "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                "underline": False, "code": False, "color": "default"},
            })
            plain.clear()

    while i < len(text):
        matched = False
        for kind, pat in _INLINE_PATTERNS:
            m = pat.match(text, i)
            if not m:
                continue
            _flush_plain()
            if kind == "link":
                content, url = m.group(1), m.group(2)
                spans.append({
                    "type": "text",
                    "text": {"content": content, "link": {"url": url}},
                    "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                    "underline": False, "code": False, "color": "default"},
                    "href": url,
                })
            else:
                content = m.group(1)
                ann = {"bold": False, "italic": False, "strikethrough": False,
                       "underline": False, "code": False, "color": "default"}
                if kind in ann:
                    ann[kind if kind != "strike" else "strikethrough"] = True
                spans.append({
                    "type": "text",
                    "text": {"content": content},
                    "annotations": ann,
                })
            i = m.end()
            matched = True
            break
        if matched:
            continue
        plain.append(text[i])
        i += 1
    _flush_plain()
    if not spans:
        spans = [{
            "type": "text",
            "text": {"content": ""},
            "annotations": {"bold": False, "italic": False, "strikethrough": False,
                            "underline": False, "code": False, "color": "default"},
        }]
    return spans


def _make_block(btype: str, text: str) -> dict:
    return {
        "object": "block",
        "type": btype,
        btype: {"rich_text": _md_to_rich(text)},
    }


_NUMBERED_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def markdown_to_blocks(md: str, passthrough_reader: PassthroughReader) -> list[dict]:
    """Parse Markdown back into a flat list of Notion block dicts.

    `passthrough_reader(marker_id)` returns the stored block JSON or None.
    Nested lists are NOT round-tripped — they flatten to top-level.
    """
    blocks: list[dict] = []
    lines = md.split("\n")
    i = 0
    paragraph_buf: list[str] = []

    def flush_paragraph():
        if paragraph_buf:
            text = "\n".join(paragraph_buf).strip()
            if text:
                blocks.append(_make_block("paragraph", text))
            paragraph_buf.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Blank line — closes any open paragraph.
        if not stripped:
            flush_paragraph()
            i += 1
            continue

        # Passthrough marker.
        m = PASSTHROUGH_MARKER_RE.match(stripped)
        if m:
            flush_paragraph()
            marker = m.group(1)
            stored = passthrough_reader(marker)
            if stored:
                blocks.append(stored)
            i += 1
            continue

        # Code fence.
        if stripped.startswith("```"):
            flush_paragraph()
            lang = stripped[3:].strip()
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            # consume the closing fence
            if i < len(lines):
                i += 1
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "\n".join(code_lines)},
                        "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                        "underline": False, "code": False, "color": "default"},
                    }],
                    "language": lang or "plain text",
                },
            })
            continue

        # Headings.
        if stripped.startswith("### "):
            flush_paragraph()
            blocks.append(_make_block("heading_3", stripped[4:]))
            i += 1
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            blocks.append(_make_block("heading_2", stripped[3:]))
            i += 1
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            blocks.append(_make_block("heading_1", stripped[2:]))
            i += 1
            continue

        # Quote.
        if stripped.startswith("> "):
            flush_paragraph()
            blocks.append(_make_block("quote", stripped[2:]))
            i += 1
            continue

        # Bullet list.
        m = _BULLET_RE.match(line)
        if m:
            flush_paragraph()
            blocks.append(_make_block("bulleted_list_item", m.group(1)))
            i += 1
            continue

        # Numbered list.
        m = _NUMBERED_RE.match(line)
        if m:
            flush_paragraph()
            blocks.append(_make_block("numbered_list_item", m.group(2)))
            i += 1
            continue

        # Default — accumulate paragraph.
        paragraph_buf.append(stripped)
        i += 1

    flush_paragraph()
    return blocks
