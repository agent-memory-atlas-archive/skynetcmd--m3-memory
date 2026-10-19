"""Session recall parts B, C and E, on every backend.

B: the summary pass finds ready sessions (quiet, or ended by a compaction/exit
hook), writes ONE session_summary each from the best source (agent note >
compaction summary > model), supersedes it when the session grows, and leaves a
session unsummarized (never dropped) when the model is unavailable.
C: session_brief's numbered lines follow the approved order; the session-start
text frames them as data. E: the doctor probe reports the backlog.

Planted chat turns go straight into the chat table; summaries are real
memory_write rows. Each test uses its own user and conversation ids, so on a
shared PostgreSQL database it reads only what it planted.
"""

from __future__ import annotations

import importlib
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

NOW = datetime.now(UTC)


def _sr():
    return importlib.import_module("session_recall")


@pytest.fixture
def tag(each_backend, monkeypatch):
    """A unique prefix for this test's ids; its rows are removed afterwards.

    The pass and the probe see only this test's sessions: a shared PostgreSQL
    database holds other runs' turns, which the pass would otherwise summarize
    too (and a stub counting model calls would count them)."""
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    t = f"rc{uuid.uuid4().hex[:8]}"
    with each_backend.connection():
        pass  # the sandbox materializes the migrated main store on first touch
    sr = _sr()
    real_recent = sr._recent_sessions
    monkeypatch.setattr(
        sr, "_recent_sessions", lambda since: [x for x in real_recent(since) if x.conversation_id.startswith(t)]
    )
    yield t
    p = dialect().param()
    with _chat_conn() as conn:
        conn.execute(f"DELETE FROM {chatlog_table('items')} WHERE conversation_id LIKE {p}", (f"{t}%",))
        conn.commit()
    with each_backend.connection() as conn:
        conn.execute(f"DELETE FROM memory_items WHERE conversation_id LIKE {p}", (f"{t}%",))
        conn.execute(f"DELETE FROM memory_items WHERE user_id LIKE {p}", (f"{t}%",))
        conn.commit()


def _chat_conn():
    """The chat store as capture resolves it (a separate file on the SQLite
    sandbox, the chat_log_* tables on PostgreSQL)."""
    from m3_sdk import M3Context

    return M3Context.for_db(None).get_chatlog_conn()


def _turn(backend, cid, at, *, role="user", host="claude-code", content="turn", variant=None, user=None, **meta):
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    d = dialect()
    m = {"role": role, "host_agent": host, "provider": "anthropic", "model_id": "claude-x", **meta}
    with _chat_conn() as conn:
        conn.execute(
            f"INSERT INTO {chatlog_table('items')} (id, type, title, content, metadata_json, agent_id, user_id, "
            f"created_at, conversation_id, variant) VALUES ({d.placeholder(10)})",
            (
                str(uuid.uuid4()),
                "chat_log",
                content[:40],
                content,
                d.json_bind_value(m),
                f"{host}:tester@box",
                user or cid.split("-")[0],
                at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                cid,
                variant,
            ),
        )
        conn.commit()


async def _summary(user, cid, host, last_at, *, cwd="", tree="", branch="", menu="m", root=None):
    import memory_core as mc

    meta = {
        "menu": menu,
        "host_agent": host,
        "conversation_id": cid,
        "user_id": user,
        "last_turn_at": last_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tree": tree or None,
        "git_branch": branch or None,
        "git_root": root,
        "cwd": cwd or None,
    }
    res = await mc.memory_write_impl(
        type="session_summary",
        content=f"summary of {cid}",
        title=menu,
        metadata=json.dumps(meta),
        user_id=user,
        scope="user",
        conversation_id=cid,
        source="session_recall",
        check_contradictions=False,
    )
    assert res.startswith("Created:"), res
    return res.split()[1]


def _live_summaries(backend, cid):
    from memory.backends import dialect

    p = dialect().param()
    with backend.connection() as conn:
        rows = conn.execute(
            f"SELECT id, title, content, metadata_json, is_deleted FROM memory_items "
            f"WHERE type='session_summary' AND conversation_id={p}",
            (cid,),
        ).fetchall()
    return [r for r in rows if not r["is_deleted"]], [r for r in rows if r["is_deleted"]]


@pytest.fixture
def model(monkeypatch):
    """The model, stubbed: records each call and answers a fixed summary."""
    calls: list[str] = []

    async def _fake(tail, header, choice):
        calls.append(tail[0].content)
        return f"menu after {tail[0].content}", f"summary after {tail[0].content}", "stub:model"

    monkeypatch.setattr(_sr(), "_model_summary", _fake)
    return calls


def _pending_ids(tag):
    return [
        p.session.conversation_id
        # The test's own clock: NOW is taken at import, so on a long run the real clock
        # has moved on and a turn written as 3 minutes old reads as quiet.
        for p in _sr().find_backlog(now=NOW).pending
        if p.session.conversation_id.startswith(tag)
    ]


# ── B: which sessions are ready ──────────────────────────────────────────────


def test_quiet_and_ended_sessions_are_ready_active_ones_wait(each_backend, tag):
    quiet, active, ended = f"{tag}-quiet", f"{tag}-active", f"{tag}-ended"
    _turn(each_backend, quiet, NOW - timedelta(minutes=45))
    _turn(each_backend, active, NOW - timedelta(minutes=3))
    _turn(each_backend, ended, NOW - timedelta(minutes=2), variant="session_end_exit")
    _turn(each_backend, f"{tag}-old", NOW - timedelta(days=5))  # outside the 2-day window
    got = sorted(_pending_ids(tag))
    assert got == sorted([quiet, ended]), f"observed ready {got} on {each_backend.name}"
    reasons = {p.session.conversation_id: p.reason for p in _sr().find_backlog(now=NOW).pending}
    assert reasons[quiet] == "quiet" and reasons[ended] == "ended"


@pytest.mark.parametrize(
    "variant,ready",
    [
        ("pre_compact", True),
        ("pre_compress", True),
        ("session_end", True),
        ("session_end_logout", True),
        ("stop", False),
        ("after_model", False),
        (None, False),
    ],
)
def test_ready_variants(variant, ready):
    assert _sr().ready_variant(variant) is ready


# ── B: the pass ──────────────────────────────────────────────────────────────


async def test_pass_writes_one_summary_then_supersedes_it_when_the_session_grows(each_backend, tag, model):
    sr = _sr()
    cid = f"{tag}-s"
    first = NOW - timedelta(hours=2)
    _turn(each_backend, cid, first, content="start", cwd="/w/p", tree="p", git_root="/w/p", git_branch="main")
    _turn(each_backend, cid, NOW - timedelta(minutes=40), role="assistant", content="first end")
    out = await sr.run_summary_pass(limit=50)
    assert out["summarized"] >= 1 and model == ["first end"], (out, model)
    live, _ = _live_summaries(each_backend, cid)
    assert len(live) == 1, f"observed {len(live)} live summaries on {each_backend.name}"
    m = each_backend.dialect().json_column_to_dict(live[0]["metadata_json"])
    assert live[0]["title"] == "menu after first end" and live[0]["content"] == "summary after first end"
    assert m["host_agent"] == "claude-code" and m["tree"] == "p" and m["git_branch"] == "main", m
    assert m["summary_source"] == "model" and m["summary_model"] == "stub:model" and m["turn_count"] == 2, m
    assert m["first_turn_at"].startswith(first.strftime("%Y-%m-%dT%H:%M")), m

    await sr.run_summary_pass(limit=50)
    assert model == ["first end"], "a covered session was summarized again"

    _turn(each_backend, cid, NOW - timedelta(minutes=25), content="grown end")
    await sr.run_summary_pass(limit=50)
    live, closed = _live_summaries(each_backend, cid)
    assert [r["title"] for r in live] == ["menu after grown end"], [r["title"] for r in live]
    assert len(closed) == 1, f"the old summary was not superseded on {each_backend.name}"
    assert cid not in _pending_ids(tag)


async def test_an_agent_note_newer_than_the_last_turn_wins_without_a_model(each_backend, tag, model):
    import memory_core as mc

    cid = f"{tag}-n"
    _turn(each_backend, cid, NOW - timedelta(minutes=50), content="work")
    res = await mc.memory_write_impl(
        type="session_note",
        content="Shipped the recall pass; next: run the PG lane.",
        title="shipped the recall pass",
        conversation_id=cid,
        user_id=cid.split("-")[0],
        check_contradictions=False,
    )
    assert res.startswith("Created:"), res
    await _sr().run_summary_pass(limit=50)
    live, _ = _live_summaries(each_backend, cid)
    assert model == [], "the model was called although an agent note covered the session"
    m = each_backend.dialect().json_column_to_dict(live[0]["metadata_json"])
    assert m["summary_source"] == "agent_note" and live[0]["title"] == "shipped the recall pass", m
    assert "run the PG lane" in live[0]["content"]


async def test_a_compaction_summary_with_no_user_turn_after_it_is_used(each_backend, tag, model):
    cid = f"{tag}-c"
    text = (
        "This session is being continued from a previous conversation.\n\nSummary:\n"
        "1. **Primary Request and Intent:**\n   build recall\n"
        "8. **Current Work:**\n   Wiring the doctor probe for session recall.\n"
        "9. **Optional Next Step:**\n   Run the tests.\n"
    )
    _turn(each_backend, cid, NOW - timedelta(minutes=60), content="earlier")
    _turn(each_backend, cid, NOW - timedelta(minutes=50), content=text, compact_summary=True)
    _turn(each_backend, cid, NOW - timedelta(minutes=49), role="assistant", content="continuing")
    await _sr().run_summary_pass(limit=50)
    live, _ = _live_summaries(each_backend, cid)
    assert model == [], "the model was called although a compaction summary covered the session"
    m = each_backend.dialect().json_column_to_dict(live[0]["metadata_json"])
    assert m["summary_source"] == "compaction", m
    assert live[0]["title"] == "Wiring the doctor probe for session recall"
    assert "Next: Run the tests." in live[0]["content"]


async def test_a_user_turn_after_the_compaction_summary_sends_it_to_the_model(each_backend, tag, model):
    cid = f"{tag}-cu"
    _turn(each_backend, cid, NOW - timedelta(minutes=50), content="**Current Work:** x", compact_summary=True)
    _turn(each_backend, cid, NOW - timedelta(minutes=45), content="new request")
    await _sr().run_summary_pass(limit=50)
    assert model == ["new request"]


async def test_an_unavailable_model_leaves_the_session_ready(each_backend, tag, monkeypatch, caplog):
    sr = _sr()
    from llm_failover import LLMUnavailable

    async def _down(tail, header, choice):
        raise LLMUnavailable("observed: no local server answered")

    monkeypatch.setattr(sr, "_model_summary", _down)
    cid = f"{tag}-down"
    _turn(each_backend, cid, NOW - timedelta(minutes=40))
    out = await sr.run_summary_pass(limit=50)
    assert out["summarized"] == 0 and "unavailable" in out["stopped"], out
    assert _live_summaries(each_backend, cid) == ([], [])
    assert cid in _pending_ids(tag), "a session was dropped when the model was down"


async def test_a_failing_session_is_retried_a_bounded_number_of_times(each_backend, tag, monkeypatch):
    sr = _sr()
    calls = []

    async def _empty(tail, header, choice):
        calls.append(1)
        return  # the reply held nothing usable

    monkeypatch.setattr(sr, "_model_summary", _empty)
    monkeypatch.setattr(sr, "_ATTEMPTS", {})
    cid = f"{tag}-bad"
    _turn(each_backend, cid, NOW - timedelta(minutes=40))
    for _ in range(sr.MAX_ATTEMPTS + 2):
        await sr.run_summary_pass(limit=50)
    assert len(calls) == sr.MAX_ATTEMPTS, f"observed {len(calls)} model calls for one unchanged session"


async def test_the_pass_yields_and_honours_recall_enabled(each_backend, tag, model, monkeypatch):
    sr = _sr()
    _turn(each_backend, f"{tag}-y", NOW - timedelta(minutes=40))
    out = await sr.run_summary_pass(limit=50, should_yield=lambda: "user active")
    assert out["stopped"] == "yielded: user active" and model == []
    real = sr.recall_settings
    monkeypatch.setattr(sr, "recall_settings", lambda: {**real(), "enabled": False})
    assert (await sr.run_summary_pass(limit=50))["stopped"] == "recall.enabled is false"
    assert sr.has_pending() is False


# ── B: the model call and its profile ────────────────────────────────────────


def test_parse_summary_reply_accepts_json_fences_think_and_plain_text():
    sr = _sr()
    assert sr.parse_summary_reply('```json\n{"menu": "fixed the gate", "summary": "Done."}\n```') == (
        "fixed the gate",
        "Done.",
    )
    assert sr.parse_summary_reply('<think>hmm</think>{"menu":"a","summary":"b"}') == ("a", "b")
    assert sr.parse_summary_reply("rotated the DSN\nmoved it to the secrets file") == (
        "rotated the DSN",
        "rotated the DSN moved it to the secrets file",
    )
    assert sr.parse_summary_reply("") is None and sr.parse_summary_reply(None) is None
    long = sr.parse_summary_reply(json.dumps({"menu": "x" * 200, "summary": "y" * 2000}))
    assert len(long[0]) == sr.MENU_MAX and len(long[1]) == sr.SUMMARY_MAX


def test_the_local_profile_ships_and_names_a_loopback_endpoint():
    from slm_intent import _is_loopback, load_profile

    prof = load_profile("session_summary")
    assert prof is not None and _is_loopback(prof.url) and prof.model == ""


def test_resolve_profile_local_session_and_named(monkeypatch):
    sr = _sr()
    import auth_utils

    Turn = sr.Turn
    tail = [
        Turn(
            "1", "x", {"role": "assistant", "provider": "anthropic", "model_id": "claude-opus-4-7"}, None, None, "", ""
        )
    ]
    prof, how = sr._resolve_profile("local", tail)
    assert how == "local" and prof.name == "session_summary"
    monkeypatch.setattr(auth_utils, "get_api_key", lambda name: None)
    prof, how = sr._resolve_profile("session", tail)
    assert prof.name == "session_summary" and "no ANTHROPIC_API_KEY stored" in how, how
    monkeypatch.setattr(auth_utils, "get_api_key", lambda name: "k" if name == "ANTHROPIC_API_KEY" else None)
    prof, how = sr._resolve_profile("session", tail)
    assert how == "session" and prof.model == "claude-opus-4-7" and prof.backend == "anthropic"
    with pytest.raises(sr.SummaryConfigError, match=r"m3 config set recall\.summary_profile local"):
        sr._resolve_profile("no_such_profile_xyz", tail)


async def test_model_summary_calls_the_profile_and_labels_the_model(monkeypatch):
    sr = _sr()
    slm = importlib.import_module("slm_intent")
    sent = []

    async def _call(prof, text, client):
        sent.append(text)
        return '{"menu": "drafted the hook", "summary": "Hook drafted."}'

    async def _with(prof, client):
        from dataclasses import replace

        return replace(prof, model="qwen-local")

    monkeypatch.setattr(slm, "_call_model", _call)
    monkeypatch.setattr(sr, "_with_model", _with)
    tail = [sr.Turn(str(i), f"turn {i}", {"role": "user"}, None, None, "", "") for i in range(3)]
    got = await sr._model_summary(tail, "agent: Claude Code\n\n", "local")
    assert got == ("drafted the hook", "Hook drafted.", "session_summary:qwen-local")
    assert sent[0].startswith("agent: Claude Code") and sent[0].index("turn 2") < sent[0].index("turn 0"), sent[0]


def test_render_tail_drops_the_oldest_turns_to_fit():
    sr = _sr()
    tail = [sr.Turn(str(i), f"t{i} " + "x" * 50, {"role": "user"}, None, None, "", "") for i in range(10)]
    text = sr._render_tail(tail, "H\n", 200)
    assert len(text) <= 200 and "t9" in text and "t0" not in text


def test_compaction_digest_without_headings_uses_the_summary_text():
    menu, summary = _sr().compaction_digest("Summary:\nRefactored the seams. Then more.")
    assert menu == "Refactored the seams" and summary.startswith("Refactored the seams.")


# ── C: the menu ──────────────────────────────────────────────────────────────


@pytest.fixture
def no_git(monkeypatch):
    seams = importlib.import_module("m3_core.seams")
    monkeypatch.setattr(seams, "git_checkout", lambda cwd: None)


async def test_session_brief_orders_lines_per_the_design(each_backend, tag, no_git, tmp_path):
    sr = _sr()
    user = f"{tag}user"
    here, there = str(tmp_path / "proj-a"), str(tmp_path / "proj-b")
    a = {"cwd": here, "tree": "proj-a", "branch": "main"}
    b = {"cwd": there, "tree": "proj-b"}
    ids = {
        "c1": await _summary(user, f"{tag}-c1", "claude-code", NOW - timedelta(hours=2), **a, menu="c1"),
        "c2": await _summary(user, f"{tag}-c2", "claude-code", NOW - timedelta(hours=1), **b, menu="c2"),
        "c3": await _summary(user, f"{tag}-c3", "claude-code", NOW - timedelta(hours=5), **a, menu="c3"),
        "c4": await _summary(user, f"{tag}-c4", "claude-code", NOW - timedelta(hours=10), **a, menu="c4"),
        "c5": await _summary(user, f"{tag}-c5", "gemini-cli", NOW - timedelta(minutes=30), **b, menu="c5"),
        "c6": await _summary(user, f"{tag}-c6", "gemini-cli", NOW - timedelta(hours=3), **a, menu="c6"),
        "c7": await _summary(user, f"{tag}-c7", "codex", NOW - timedelta(minutes=20), **b, menu="c7"),
        "c8": await _summary(user, f"{tag}-c8", "opencode", NOW - timedelta(days=3), **a, menu="c8"),
        "c9": await _summary(user, f"{tag}-c9", "aider", NOW - timedelta(minutes=5), **b, menu="c9"),
    }
    await _summary(f"{tag}other", f"{tag}-x", "claude-code", NOW - timedelta(minutes=1), **a, menu="other user")

    brief = sr.session_brief("claude", user_id=user, cwd=here, now=NOW)
    got = [ln["menu"] for ln in brief["lines"]]
    assert got == ["c1", "c3", "c4", "c6", "c9", "c7"], f"observed {got} on {each_backend.name}"
    assert [ln["n"] for ln in brief["lines"]] == [1, 2, 3, 4, 5, 6]
    assert brief["lines"][0]["id"] == ids["c1"] and brief["lines"][0]["this_agent"] is True
    assert brief["lines"][0]["current_tree"] is True and brief["lines"][0]["branch"] == "main"
    active = {ln["menu"]: ln["active"] for ln in brief["lines"]}
    assert active["c9"] is True and active["c7"] is False and active["c1"] is False
    assert brief["host_agent"] == "claude-code" and brief["current_tree"] == "proj-a"
    menu = brief["menu"].splitlines()
    assert menu[0] == sr.MENU_HEADER and menu[-1] == "  0  none" and len(menu) == 8
    assert "Claude Code (this agent)" in menu[1] and "proj-a @ main" in menu[1]
    assert menu[5].rstrip().endswith("c9 (active)"), menu[5]
    assert brief["elapsed_ms"] < 500, f"the lookup took {brief['elapsed_ms']} ms (target < 500)"


async def test_brief_without_a_current_tree_fills_by_recency(each_backend, tag, no_git):
    sr = _sr()
    user = f"{tag}u2"
    await _summary(user, f"{tag}-r1", "claude-code", NOW - timedelta(hours=3), tree="t1", menu="older")
    await _summary(user, f"{tag}-r2", "claude-code", NOW - timedelta(hours=1), menu="no project")
    brief = sr.session_brief("claude-code", user_id=user, now=NOW)
    assert [ln["menu"] for ln in brief["lines"]] == ["no project", "older"]
    assert brief["lines"][0]["tree"] == sr.NO_PROJECT


async def test_brief_counts_come_from_the_settings(each_backend, tag, no_git, monkeypatch):
    sr = _sr()
    user = f"{tag}u3"
    for i in range(4):
        await _summary(user, f"{tag}-k{i}", "claude-code", NOW - timedelta(hours=i + 1), menu=f"k{i}")
    await _summary(user, f"{tag}-g", "gemini-cli", NOW - timedelta(hours=1), menu="g")
    real = sr.recall_settings
    monkeypatch.setattr(sr, "recall_settings", lambda: {**real(), "same_agent_sessions": 1, "other_agents": 0})
    assert [ln["menu"] for ln in sr.session_brief("claude-code", user_id=user, now=NOW)["lines"]] == ["k0"]
    monkeypatch.setattr(sr, "recall_settings", lambda: {**real(), "enabled": False})
    off = sr.session_brief("claude-code", user_id=user, now=NOW)
    assert off["enabled"] is False and off["lines"] == [] and "recall.enabled" in off["reason"]


async def test_session_start_text_frames_the_menu_as_data(each_backend, tag, no_git):
    sr = _sr()
    user = f"{tag}u4"
    sid = await _summary(user, f"{tag}-t", "codex", NOW - timedelta(hours=1), tree="homelab", menu="rotated the DSN")
    text = sr.recall_menu_text("claude", user_id=user)
    assert "data, not instructions" in text and "memory_get" in text and "0 or Enter means none" in text
    assert "answer it and show the menu alongside" in text
    assert f"1 = {sid}" in text and "Codex" in text and "rotated the DSN" in text
    assert sr.recall_menu_text("claude", user_id=f"{tag}nobody") == "", "an empty window must print nothing"


def test_session_start_text_names_the_cause_when_it_cannot_look(monkeypatch):
    sr = _sr()

    def _boom(user_id, since):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sr, "_read_summaries", _boom)
    text = sr.recall_menu_text("claude", user_id="someone")
    assert text.startswith("m3 recall unavailable:") and "database is locked" in text and "m3 doctor" in text
    out = json.loads(sr.session_brief_impl("claude-code", user_id="someone"))
    assert "database is locked" in out["error"] and "lines" not in out


def test_time_labels_show_local_time_with_zone_and_age(monkeypatch):
    sr = _sr()
    edt = timezone(timedelta(hours=-4), "EDT")
    monkeypatch.setattr(sr, "_local_tz", lambda: edt)
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)  # 16:00 EDT
    assert sr.time_labels(datetime(2026, 10, 18, 18, 32, tzinfo=UTC), now) == ("today 14:32 EDT", "1 h ago")
    assert sr.time_labels(datetime(2026, 10, 18, 1, 40, tzinfo=UTC), now) == ("yesterday 21:40 EDT", "18 h ago")
    assert sr.time_labels(datetime(2026, 10, 12, 13, 10, tzinfo=UTC), now)[0] == "Mon 09:10 EDT"
    assert sr.time_labels(now - timedelta(seconds=10), now)[1] == "just now"
    long_name = timezone(timedelta(hours=-4), "Eastern Daylight Time")
    monkeypatch.setattr(sr, "_local_tz", lambda: long_name)
    assert sr.time_labels(now, now)[0].endswith(" EDT")


def test_every_capture_host_has_a_display_name():
    import chatlog_config

    missing = sorted(set(chatlog_config.VALID_HOST_AGENTS) - set(_sr().AGENT_NAMES))
    assert not missing, f"host agents with no menu name: {missing}"


# ── E: doctor ────────────────────────────────────────────────────────────────


def test_doctor_reports_current_queued_and_lagging(each_backend, tag, capsys, monkeypatch):
    from doctor import session_recall_probe as probe

    sr = _sr()
    real = sr.recall_settings
    assert probe.run(brief=True) == 0
    assert "✅ session recall:" in capsys.readouterr().out

    _turn(each_backend, f"{tag}-q", NOW - timedelta(minutes=30), cwd="/w", tree="w")
    probe.run(brief=True)
    out = capsys.readouterr().out
    assert out.startswith("✅") and "1 session(s) queued" in out, out

    _turn(each_backend, f"{tag}-late", NOW - timedelta(hours=3))
    probe.run(brief=True)
    out = capsys.readouterr().out
    assert out.startswith("⚠️") and "2 session(s) wait for a summary" in out and "not keeping up" in out, out

    probe.run(brief=False)
    full = capsys.readouterr().out
    assert "=== session recall ===" in full and "status   : WARN" in full and "claude-code" in full

    monkeypatch.setattr(sr, "recall_settings", lambda: {**real(), "enabled": False})
    probe.run(brief=True)
    assert "off (recall.enabled = false)" in capsys.readouterr().out


def test_doctor_never_crashes_and_names_the_cause(monkeypatch, capsys):
    from doctor import session_recall_probe as probe

    sr = _sr()
    monkeypatch.setattr(sr, "find_backlog", lambda **k: (_ for _ in ()).throw(RuntimeError("no such table")))
    assert probe.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "could not check" in out and "no such table" in out


def test_doctor_exposes_skip_session_recall_flag():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "bin" / "memory_doctor.py").read_text(encoding="utf-8")
    assert "--skip-session-recall" in text and "session_recall_probe.run(brief=brief)" in text


# ── wiring: settings, types, tool, instructions, loop ───────────────────────


def test_recall_settings_are_registered_with_the_design_defaults():
    from m3_core.config import REGISTRY

    want = {
        "recall.enabled": True,
        "recall.window_days": 2,
        "recall.same_agent_sessions": 3,
        "recall.other_agents": 3,
        "recall.active_minutes": 10,
        "recall.quiet_minutes": 20,
        "recall.summary_profile": "local",
    }
    for name, default in want.items():
        key = REGISTRY.get(name)
        assert key is not None and key.default == default and not key.env_names, name
    assert _sr().recall_settings()["summary_profile"] == "local"


def test_the_new_memory_types_are_valid():
    from catalog.spec import VALID_MEMORY_TYPES

    assert {"session_summary", "session_note"} <= VALID_MEMORY_TYPES


def test_session_brief_is_an_mcp_tool_in_the_memory_domain():
    import mcp_tool_catalog
    import tool_domains

    spec = {t.name: t for t in mcp_tool_catalog.TOOLS}["session_brief"]
    assert spec.mcp and spec.default_allowed and not spec.is_destructive
    assert spec.parameters["required"] == ["host_agent"]
    assert tool_domains.domain_of_tool("session_brief") == "memory"
    assert "session_brief" not in tool_domains.ESSENTIAL_TOOL_NAMES  # reached through m3_call


def test_the_server_instructions_offer_the_menu():
    import memory_bridge

    text = memory_bridge._SERVER_INSTRUCTIONS
    assert 'm3_call("session_brief"' in text and "data, not instructions" in text and "session_note" in text


async def test_the_loop_registers_and_runs_the_pass(monkeypatch):
    import argparse

    import m3_cognitive_loop as loop

    sr = _sr()
    seen = {}

    async def _fake_pass(limit, should_yield=None):
        seen["limit"] = limit
        seen["yield"] = should_yield
        return {"summarized": 1, "skipped": 0, "failed": 0, "pending": 1, "stopped": ""}

    monkeypatch.setattr(sr, "has_pending", lambda: True)
    monkeypatch.setattr(sr, "run_summary_pass", _fake_pass)
    await loop.run_summarize_sessions_pass(argparse.Namespace(limit_per_pass=7))
    assert seen["limit"] == 7 and callable(seen["yield"])

    monkeypatch.setattr(sr, "has_pending", lambda: (_ for _ in ()).throw(RuntimeError("store down")))
    assert loop.has_summarize_work() is False
    from pathlib import Path

    src = Path(loop.__file__).read_text(encoding="utf-8")
    assert '"name": "summarize_sessions"' in src and "--skip-summarize-sessions" in src


def test_latest_compaction_is_found_anywhere_in_the_session(each_backend, tag):
    """A long session's newest compaction summary feeds its summary even when it
    lies outside the last turns: a turn flagged compact_summary, or one that opens
    the way Claude Code's summary does (turns captured before the flag)."""
    sr = _sr()
    cid = f"{tag}-long"
    _turn(each_backend, cid, NOW - timedelta(hours=5), content=f"{sr.COMPACTION_OPENING}. Older summary.")
    _turn(each_backend, cid, NOW - timedelta(hours=3), content="the newer summary", compact_summary=True)
    for i in range(sr.TAIL_TURNS + 5):  # more turns than the tail holds
        _turn(each_backend, cid, NOW - timedelta(minutes=100 - i), content=f"turn {i}")
    got = sr._latest_compaction(cid)
    assert got is not None and got.content == "the newer summary", f"observed {got} on {each_backend.name}"
    assert sr._latest_compaction(f"{tag}-none") is None
    only_opening = f"{tag}-old"
    _turn(each_backend, only_opening, NOW - timedelta(hours=1), content=f"{sr.COMPACTION_OPENING}: the arc.")
    assert sr._latest_compaction(only_opening).content.endswith("the arc.")
