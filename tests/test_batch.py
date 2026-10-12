"""`m3 batch` (approved interface, m3 009d7cce): several m3 commands from stdin
in one process; command text (POSIX quoting) or a JSON array per line; JSON
Lines results; --set/--config only on the batch; refusals named by line."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


def _batch(stdin: str, *flags: str, pre: tuple[str, ...] = ()) -> tuple[int, list[dict], str]:
    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *pre, "batch", *flags],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    return out.returncode, rows, out.stdout


def test_text_and_json_lines_run_in_one_process_and_comments_are_skipped():
    code, rows, _ = _batch(
        "# a comment\n\nconfig get search.row_cap\n" + json.dumps(["config", "get", "db.backend"]) + "\n"
    )
    assert code == 0
    assert [r["line"] for r in rows] == [3, 4]
    assert [r["exit"] for r in rows] == [0, 0]
    assert rows[1]["command"] == ["config", "get", "db.backend"] and rows[1]["stdout"].strip()


def test_a_batch_wide_set_reaches_every_line():
    code, rows, _ = _batch("config get search.row_cap\n", pre=("--set={search.row_cap=11}",))
    assert code == 0 and rows[0]["stdout"].strip() == "11"


@pytest.mark.parametrize(
    "line, why",
    [
        ("--set={search.row_cap=1} config get search.row_cap", "process-wide"),
        ("config get search.row_cap --config=x.toml", "process-wide"),
        ("setup", "asks questions"),
        ("secret set M3_PRIMARY_PG_URL", "asks for the secret"),
        ("serve", "runs until stopped"),
        ("upgrade", "changes or stops the m3 installation"),
        ("batch", "do not nest"),
        ('config get "unclosed', "unclosed quote"),
        ('["config", 1]', "array of strings"),
    ],
)
def test_refused_lines_say_why_and_the_rest_still_run(line, why):
    code, rows, _ = _batch(f"{line}\nconfig get search.row_cap\n")
    assert code == 1
    assert rows[0]["exit"] == 2 and why in rows[0]["stderr"] and rows[0]["command"] is None
    assert rows[1]["exit"] == 0, "a refused line does not stop the batch"


def test_a_failing_command_is_reported_and_stop_on_error_stops():
    code, rows, _ = _batch("config get no.such_key\nconfig get search.row_cap\n")
    assert code == 1 and rows[0]["exit"] != 0 and len(rows) == 2
    code, rows, _ = _batch("config get no.such_key\nconfig get search.row_cap\n", "--stop-on-error")
    assert code == 1 and len(rows) == 1


def test_text_output_is_readable_sections():
    code, _, stdout = _batch("config get search.row_cap\n", "--text")
    assert code == 0 and "=== line 1: config get search.row_cap (exit 0) ===" in stdout


# Runs in its own interpreter: conftest ORs CREATE_NO_WINDOW into every Popen in
# this process, and on Windows a CREATE_NO_WINDOW child with inherited handles
# writes nowhere (measured 2026-10-12), so the child output would vanish rather
# than reach either stream. m3 spawns foreground children with
# hidden_window_kwargs (SW_HIDE), which keeps it; the probe does the same.
_CHILD_PROBE = """
import json, subprocess, sys
from m3_memory import cli
from m3_memory._platform import hidden_window_kwargs

def fake_main():
    subprocess.run([sys.executable, "-c", "print('from the child')"], check=True, **hidden_window_kwargs())
    print("from the command")

cli.main = fake_main
code, out, err = cli._run_batch_line(["anything"])
print(json.dumps({"code": code, "out": out}))
"""


def test_a_child_process_output_is_captured_not_leaked():
    """A command that runs a child writes through the inherited descriptor; it
    must land in that line's stdout, never in the JSON stream."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    *leaked, last = proc.stdout.splitlines()
    got = json.loads(last)
    assert got["code"] == 0 and "from the child" in got["out"] and "from the command" in got["out"]
    assert not any("from the child" in line for line in leaked), leaked
