"""LLM-assisted memory extraction for manastone-diagnostic.

This uses a *structured write plan* pattern:
- The LLM returns JSON describing which files to upsert/delete.
- The program applies changes safely under the memdir root.

Note: The LLM client in this repo is OpenAI-compatible and does not provide
native JSON-schema enforcement. We therefore:
- instruct the model to output JSON only
- parse with json.loads (best effort)
- validate required keys and sanitize filenames

If parsing fails, extraction is a no-op.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .memdir import (
    MEMORY_TYPES,
    build_frontmatter,
    format_manifest,
    get_index_path,
    get_memdir_root,
    parse_frontmatter,
    resolve_memory_path,
    sanitize_memory_filename,
    scan_memory_headers,
    upsert_index_entry,
)


SYSTEM_PROMPT = """You are a memory extraction module for a robot diagnostic assistant.

You update a persistent file-based memory store.

Rules:
- Output ONLY valid JSON.
- Be selective: only save durable, non-obvious facts, gotchas, procedures, preferences, incidents, or service context.
- Prefer updating an existing file over creating duplicates.
- Do not store secrets.

Memory types: robot_fact, safety_gotcha, procedure, preference, incident, service_context.

Return JSON with keys: upserts (array), deletes (array), optional notes.
"""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    # Best effort: try direct parse, then regex-pick the largest object.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


@dataclass
class ExtractContext:
    robot_id: str
    user_query: str
    context_summary: str
    response_summary: str


class MemDirExtractor:
    def __init__(self, storage_dir: Path, robot_id: str, llm: Any):
        self.storage_dir = storage_dir
        self.robot_id = robot_id
        self.llm = llm

    @property
    def root(self) -> Path:
        return get_memdir_root(self.storage_dir, self.robot_id)

    @property
    def index_path(self) -> Path:
        return get_index_path(self.storage_dir, self.robot_id)

    def _safe_type(self, t: str) -> str:
        return t if t in MEMORY_TYPES else "incident"

    async def extract_and_apply(self, ctx: ExtractContext) -> Dict[str, Any]:
        if not self.llm or not getattr(self.llm, "is_available", lambda: True)():
            return {"applied": False, "reason": "llm_unavailable"}

        self.root.mkdir(parents=True, exist_ok=True)

        headers = scan_memory_headers(self.root)
        manifest = format_manifest(headers)

        prompt = (
            f"Robot: {ctx.robot_id}\n"
            f"User query: {ctx.user_query}\n\n"
            f"Context summary:\n{ctx.context_summary}\n\n"
            f"Response summary:\n{ctx.response_summary}\n\n"
            "Existing memory files:\n"
            + (manifest if manifest else "(none)")
            + "\n\n"
            "Return a write plan. If nothing is worth saving, return upserts=[] and deletes=[]."
        )

        try:
            raw = await self.llm.chat(prompt, system_prompt=SYSTEM_PROMPT)
        except Exception as e:
            return {"applied": False, "reason": f"llm_error: {str(e)[:80]}"}

        plan = _extract_json(raw)
        if not plan:
            return {"applied": False, "reason": "invalid_json"}

        upserts = plan.get("upserts")
        deletes = plan.get("deletes")
        if not isinstance(upserts, list) or not isinstance(deletes, list):
            return {"applied": False, "reason": "missing_keys"}

        applied = {"upserts": 0, "deletes": 0}

        for fname in deletes:
            if not isinstance(fname, str):
                continue
            try:
                path = resolve_memory_path(self.root, fname)
                if path.exists():
                    path.unlink()
                    applied["deletes"] += 1
            except Exception:
                continue

        for item in upserts:
            if not isinstance(item, dict):
                continue
            try:
                mtype = self._safe_type(str(item.get("type", "incident")))
                filename = sanitize_memory_filename(str(item.get("filename", "memory.md")))
                title = str(item.get("title", "")).strip()
                hook = str(item.get("hook", "")).strip()
                description = str(item.get("description", "")).strip()
                body = str(item.get("body", "")).strip()

                if not title or not body or not hook:
                    continue

                path = resolve_memory_path(self.root, filename)
                now = datetime.now(timezone.utc).isoformat()

                frontmatter: Dict[str, Any] = {
                    "type": mtype,
                    "description": description,
                    "robot_id": self.robot_id,
                    "updated_at": now,
                }

                if path.exists():
                    try:
                        existing = path.read_text(encoding="utf-8")
                        fm_old, _ = parse_frontmatter(existing)
                        for k, v in fm_old.items():
                            if k not in frontmatter:
                                frontmatter[k] = v
                    except Exception:
                        pass

                content = build_frontmatter(frontmatter) + "\n" + f"# {title}\n\n" + body + "\n"
                path.write_text(content, encoding="utf-8")
                upsert_index_entry(self.index_path, title=title, filename=path.name, hook=hook)
                applied["upserts"] += 1
            except Exception:
                continue

        return {"applied": True, "counts": applied, "notes": plan.get("notes", "")}
