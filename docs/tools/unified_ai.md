---
tool: bin/unified_ai.py
sha1: 66e82d925850
mtime_utc: 2026-10-10T21:05:16.473830+00:00
generated_utc: 2026-10-10T21:05:30.024360+00:00
private: false
---

# bin/unified_ai.py

## Purpose

Unified chat client across Gemini, Claude, and LM Studio.

Hardened httpx client with HTTP/2 disabled and zero keep-alive reuse —
this configuration was provided by Google support to work around
keep-alive hangs we observed against the Gemini OpenAI-compatible
endpoint during high-volume enrichment runs (April-May 2026).

Usage:
    from unified_ai import UnifiedAI
    cli = UnifiedAI(gemini_key=os.environ["GEMINI_API_KEY"])
    text = cli.chat(
        "gemini", "gemini-2.5-flash",
        messages=[{"role": "system", "content": "..."},
                  {"role": "user",   "content": "..."}],
        temperature=0, max_tokens=1024, reasoning_effort="none",
    )

The chat method returns just the assistant's text content. For richer
metadata (token counts, finish reason) call .chat_raw() which returns
the parsed provider-native JSON.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `GEMINI_API_KEY`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 54)
- `httpx.AsyncClient()` (line 71)
- `httpx.AsyncClient()` (line 72)
- `httpx.Client()` (line 90)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
