"""File-based persistent memory (memdir) for manastone-diagnostic.

This mirrors the core memdir ideas:
- Memories are Markdown files with YAML frontmatter.
- MEMORY.md is an index (one-line hooks), not memory content.
- Storage is bounded and auditable.

Unlike snakes-V (tuning agent), this diagnostic package does not have an
idle-cycle concept. By default, callers may choose to consolidate after each
user query or on a schedule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


MEMORY_TYPES = {
    "robot_fact",
    "safety_gotcha",
    "procedure",
    "preference",
    "incident",
    "service_context",
}

INDEX_FILENAME = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SAFE_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}\.md$")


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    type: Optional[str]
    description: Optional[str]
    updated_at: Optional[str]


def get_memdir_root(storage_dir: Path, robot_id: str) -> Path:
    return storage_dir / "memories" / robot_id


def get_index_path(storage_dir: Path, robot_id: str) -> Path:
    return get_memdir_root(storage_dir, robot_id) / INDEX_FILENAME


def parse_frontmatter(markdown: str) -> Tuple[Dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(markdown)
    if not m:
        return {}, markdown
    fm_text, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return fm, body


def build_frontmatter(frontmatter: Dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n"


def sanitize_memory_filename(name: str) -> str:
    raw = name.strip().lower()
    if raw.endswith(".md"):
        stem = raw[:-3]
    else:
        stem = raw
    stem = stem.replace(" ", "_")
    stem = re.sub(r"[^a-z0-9_-]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    base = (stem or "memory") + ".md"
    if not _SAFE_FILENAME_RE.match(base):
        base = "memory.md"
    return base


def resolve_memory_path(root: Path, filename: str) -> Path:
    safe = sanitize_memory_filename(filename)
    path = (root / safe).resolve()
    root_resolved = root.resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError("Path traversal detected")
    return path


def ensure_index_exists(index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if not index_path.exists():
        index_path.write_text(
            "# MEMORY\n\n"
            "Index only. Do not write memory content here.\n"
            "Each entry: - [Title](file.md) — one-line hook\n\n",
            encoding="utf-8",
        )


def _truncate_index(text: str) -> str:
    lines = text.splitlines()
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
    truncated = "\n".join(lines).strip() + "\n"
    if len(truncated) > MAX_INDEX_BYTES:
        cut_at = truncated.rfind("\n", 0, MAX_INDEX_BYTES)
        truncated = truncated[: cut_at if cut_at > 0 else MAX_INDEX_BYTES].strip() + "\n"
    return truncated


def upsert_index_entry(index_path: Path, *, title: str, filename: str, hook: str) -> None:
    ensure_index_exists(index_path)
    raw = index_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    entry = f"- [{title}]({filename}) — {hook}".strip()
    link_pat = re.compile(rf"^\s*-\s*\[[^\]]+\]\({re.escape(filename)}\)\s*—\s*.*$")

    replaced = False
    out: List[str] = []
    for line in lines:
        if link_pat.match(line):
            out.append(entry)
            replaced = True
        else:
            out.append(line)

    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(entry)

    index_path.write_text(_truncate_index("\n".join(out)), encoding="utf-8")


def scan_memory_headers(root: Path, limit: int = 200) -> List[MemoryHeader]:
    if not root.exists():
        return []
    files = sorted([p for p in root.glob("*.md") if p.name != INDEX_FILENAME])
    headers: List[MemoryHeader] = []
    for p in files[:limit]:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = parse_frontmatter(text)
        headers.append(
            MemoryHeader(
                filename=p.name,
                type=str(fm.get("type")) if fm.get("type") is not None else None,
                description=str(fm.get("description")) if fm.get("description") is not None else None,
                updated_at=str(fm.get("updated_at")) if fm.get("updated_at") is not None else None,
            )
        )
    return headers


def format_manifest(headers: Iterable[MemoryHeader]) -> str:
    lines: List[str] = []
    for h in headers:
        t = f"[{h.type}] " if h.type else ""
        desc = f": {h.description}" if h.description else ""
        ua = f" (updated_at={h.updated_at})" if h.updated_at else ""
        lines.append(f"- {t}{h.filename}{ua}{desc}")
    return "\n".join(lines)


def ensure_robot_identity_memory(
    storage_dir: Path,
    robot_id: str,
    *,
    robot_type: str,
    mock_mode: bool,
    schema_path: str,
) -> Path:
    """Deterministically maintain a robot identity memory (robot_fact)."""
    root = get_memdir_root(storage_dir, robot_id)
    root.mkdir(parents=True, exist_ok=True)

    filename = "robot_identity.md"
    path = resolve_memory_path(root, filename)

    now = datetime.now(timezone.utc).isoformat()
    fm: Dict[str, Any] = {
        "type": "robot_fact",
        "description": "Stable identity/environment facts for this diagnostic instance",
        "robot_id": robot_id,
        "robot_type": robot_type,
        "mock_mode": mock_mode,
        "schema_path": schema_path,
        "updated_at": now,
    }

    body = (
        f"# Robot identity: {robot_id}\n\n"
        "This is a deterministic identity record maintained by the program.\n\n"
        f"- robot_id: {robot_id}\n"
        f"- robot_type: {robot_type}\n"
        f"- mock_mode: {mock_mode}\n"
        f"- schema_path: {schema_path}\n"
    )

    path.write_text(build_frontmatter(fm) + "\n" + body, encoding="utf-8")

    index_path = get_index_path(storage_dir, robot_id)
    upsert_index_entry(
        index_path,
        title=f"Robot identity: {robot_id}",
        filename=path.name,
        hook="Who I am: identity, mode, schema location.",
    )

    return path
