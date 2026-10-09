"""A judge reply with fewer verdicts than items fails that batch, loudly, and the
run continues. Before, zip() silently dropped the unjudged items; with
zip(strict=True) alone the mismatch would have ended the whole run."""
from __future__ import annotations

import sys

import promote_pipeline as pp


def test_short_judge_reply_fails_only_its_batch(monkeypatch, tmp_path, capsys):
    items = [{"id": f"m{i}", "signal": "decision", "role": "user", "content": f"c{i}"}
             for i in range(4)]
    monkeypatch.setattr(pp, "select", lambda db: items)
    replies = iter([
        [{"verdict": "PROMOTE", "fact": "f0"}],                 # 1 verdict for 2 items
        [{"verdict": "PROMOTE", "fact": "f2", "title": "t"},
         {"verdict": "SKIP"}],
    ])
    monkeypatch.setattr(pp, "judge", lambda batch: next(replies))
    out = tmp_path / "accepted.jsonl"
    monkeypatch.setattr(sys, "argv", ["promote_pipeline.py", "--db", "unused", "--run",
                                      "--batch", "2", "--out", str(out)])
    pp.main()
    printed = capsys.readouterr().out
    assert "batch 0: ERROR judge returned 1 verdicts for 2 items" in printed
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"source_id": "m2"' in lines[0]
