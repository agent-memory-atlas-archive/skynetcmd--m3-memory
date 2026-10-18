"""Setup asks once whether m3 checks pypi.org daily for a newer version.

The flags decide it unattended; the question is asked only while the setting
was never recorded; the answer is saved as updates.check, and a failed save is
reported with the command that fixes it."""

from __future__ import annotations

import pytest

from m3_memory import setup_wizard as w


@pytest.mark.parametrize("argv, expected", [([], None), (["--update-check"], True), (["--no-update-check"], False)])
def test_the_flags_set_the_plan(argv, expected):
    import argparse

    parser = argparse.ArgumentParser()
    w.add_arguments(parser)
    assert parser.parse_args(argv).update_check is expected


def test_the_question_is_asked_only_while_unanswered(monkeypatch):
    settings = __import__("m3_core.config.settings", fromlist=["origin_of"])
    monkeypatch.setattr(settings, "origin_of", lambda name: "default")
    assert w._update_check_unanswered() is True
    monkeypatch.setattr(settings, "origin_of", lambda name: "file")
    assert w._update_check_unanswered() is False


def test_the_answer_is_saved(monkeypatch, capsys):
    writer = __import__("m3_core.config.writer", fromlist=["set_values"])
    saved = []
    monkeypatch.setattr(writer, "set_values", lambda loc, values: saved.append(values) or "m3_config.toml")
    w._record_update_check(w.SetupPlan(update_check=True))
    assert [{k.name: v for k, v in s.items()} for s in saved] == [{"updates.check": True}]
    assert "version check on" in capsys.readouterr().out


def test_not_asked_means_nothing_is_written(monkeypatch):
    writer = __import__("m3_core.config.writer", fromlist=["set_values"])
    monkeypatch.setattr(writer, "set_values", lambda *a: pytest.fail("wrote although not asked"))
    w._record_update_check(w.SetupPlan())


def test_a_failed_save_names_the_fix(monkeypatch, capsys):
    writer = __import__("m3_core.config.writer", fromlist=["set_values"])

    def refuse(loc, values):
        raise OSError("read-only")

    monkeypatch.setattr(writer, "set_values", refuse)
    w._record_update_check(w.SetupPlan(update_check=False))
    out = capsys.readouterr().out
    assert "could not record updates.check" in out and "m3 config set updates.check false" in out
