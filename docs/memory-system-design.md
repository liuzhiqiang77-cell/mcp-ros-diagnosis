# Manastone Diagnostic — File Memory System (MemDir)

This repo includes a file-based persistent memory system inspired by Claude Code's `memdir`.

## Why

Diagnostics often depend on durable context that is not derivable from the codebase:
- robot-specific facts (hardware quirks, deployment topology)
- safety gotchas (what to never do)
- procedures (runbooks)
- operator preferences (how to report / what to prioritize)
- incidents (what happened before)
- service context (who/what we serve, and drift in preferences)

A file-based memory store makes this context auditable and easy to maintain.

## Storage layout

Memories are stored under the configured `storage_dir` (passed to server init):

```
<storage_dir>/
  memories/
    <robot_id>/
      MEMORY.md
      robot_identity.md
      ... other memory files ...
```

`MEMORY.md` is an index only (one line per file). It is size-capped (200 lines / 25KB).

## Memory taxonomy

Supported `type` values in frontmatter:

- `robot_fact`
- `safety_gotcha`
- `procedure`
- `preference`
- `incident`
- `service_context`

## Identity (deterministic)

`robot_identity.md` is maintained deterministically by the program during startup.

## Auto-enrichment (optional)

After each diagnostic query, the orchestrator may attempt to auto-enrich memories
using the configured LLM.

Safety model:
- the LLM does NOT write files directly
- it returns a JSON "write plan" (upserts/deletes)
- the program applies the plan under the memdir root with filename sanitization

If the LLM is not available (no API key / call failure), enrichment degrades to a no-op.
