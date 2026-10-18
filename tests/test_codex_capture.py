"""Codex chat capture: chatlog_ingest --format codex reads a Codex rollout.

A rollout is planted line by line in the documented shapes (session_meta,
turn_context, response_item messages, reasoning and tool calls, the context
Codex injects as a user message) and must yield exactly the person's and the
assistant's turns, with ids that are the same on every re-read (Stop re-reads
the whole file after each turn)."""

from __future__ import annotations

import json

import chatlog_ingest as ci


def _line(kind, payload, ts="2026-10-18T17:00:00.000Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def _rollout():
    return "\n".join(
        [
            _line("session_meta", {"session_id": "s-123", "id": "t-1", "cwd": "/work/site"}),
            _line("turn_context", {"model": "gpt-5-codex"}),
            _line(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}],
                },
            ),
            _line(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Use PostgreSQL for the site."}],
                },
                ts="2026-10-18T17:00:01.000Z",
            ),
            _line("response_item", {"type": "reasoning", "summary": []}),
            _line("response_item", {"type": "function_call", "name": "shell", "arguments": "{}"}),
            _line(
                "response_item",
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
            ),
            _line(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done: PostgreSQL it is."}],
                },
                ts="2026-10-18T17:00:05.000Z",
            ),
            _line("event_msg", {"type": "user_message", "message": "Use PostgreSQL for the site."}),
        ]
    )


def test_only_real_turns_are_captured():
    items, sid = ci._parse_codex(_rollout())
    assert sid == "s-123"
    assert [(i["role"], i["content"]) for i in items] == [
        ("user", "Use PostgreSQL for the site."),
        ("assistant", "Done: PostgreSQL it is."),
    ]
    assert items[1]["model_id"] == "gpt-5-codex" and items[1]["provider"] == "openai"
    assert all(i["conversation_id"] == "s-123" for i in items)


def test_ids_are_stable_across_reads():
    first = [i["uuid"] for i in ci._parse_codex(_rollout())[0]]
    again = [i["uuid"] for i in ci._parse_codex(_rollout())[0]]
    assert first == again and len(set(first)) == 2


def test_a_bad_line_is_skipped_not_fatal():
    items, _ = ci._parse_codex(_rollout() + "\n{not json")
    assert len(items) == 2


def test_codex_is_a_registered_format():
    assert ci.PARSERS["codex"] is ci._parse_codex
