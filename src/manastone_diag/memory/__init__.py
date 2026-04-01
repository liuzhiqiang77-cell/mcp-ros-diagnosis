"""Persistent file-based memory for manastone-diagnostic.

A lightweight, auditable memory system inspired by Claude Code's memdir.

This package is intentionally small:
- `memdir.py` handles storage layout, index management, and frontmatter.
- `store.py` provides query-time recall context.
- `extractor.py` optionally uses an LLM to auto-enrich memories.

The source of truth is the filesystem under the configured storage_dir.
"""
