"""Query-time recall for file-based memories.

Offline-friendly: selection is rule-based (keyword overlap).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from .memdir import (
    MemoryHeader,
    format_manifest,
    get_memdir_root,
    parse_frontmatter,
    scan_memory_headers,
)


class FileMemoryStore:
    def __init__(self, storage_dir: Path, robot_id: str):
        self.storage_dir = storage_dir
        self.robot_id = robot_id

    @property
    def root(self) -> Path:
        return get_memdir_root(self.storage_dir, self.robot_id)

    def build_recall_context(self, query: str, max_chars: int = 3000) -> str:
        if not self.root.exists():
            return ""

        headers = scan_memory_headers(self.root)
        if not headers:
            return ""

        identity = [h for h in headers if h.filename == "robot_identity.md"]
        others = [h for h in headers if h.filename != "robot_identity.md"]
        selected = identity + self._select_by_overlap(query, others, k=4)

        parts: List[str] = ["=== PERSISTENT MEMORIES (memdir) ==="]
        manifest = format_manifest(selected)
        if manifest:
            parts.append("Selected memory files:")
            parts.append(manifest)

        for h in selected:
            p = self.root / h.filename
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            fm, body = parse_frontmatter(txt)
            mem_type = fm.get("type")
            desc = fm.get("description")
            parts.append("")
            parts.append(f"--- {h.filename} ({mem_type}) ---")
            if desc:
                parts.append(f"description: {desc}")
            parts.append(body.strip()[:1200])

        return ("\n".join(parts).strip() + "\n")[:max_chars]

    def _select_by_overlap(
        self, query: str, headers: List[MemoryHeader], k: int
    ) -> List[MemoryHeader]:
        q = (query or "").lower()
        q_tokens = set(re.findall(r"[a-z0-9_\-]+", q))

        def score(h: MemoryHeader) -> Tuple[int, int]:
            text = (h.filename + " " + (h.description or "") + " " + (h.type or "")).lower()
            tokens = set(re.findall(r"[a-z0-9_\-]+", text))
            overlap = len(q_tokens & tokens)
            recency = 1 if h.updated_at else 0
            return (overlap, recency)

        ranked = sorted(headers, key=score, reverse=True)
        return ranked[:k]
