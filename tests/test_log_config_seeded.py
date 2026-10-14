"""ensure_log_config seeds .log_config.json, and the daemons call it.

It existed to make the log-rotation cap discoverable (headless launchers do
not inherit M3_LOG_MAX_BYTES, so the file is the knob that reaches them) but
had no caller. It is now called beside ensure_governor_config at cognitive-loop
start and by install_schedules --add.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"


def test_seeds_once_and_never_clobbers(monkeypatch, tmp_path):
    import _task_runtime

    path = tmp_path / ".log_config.json"
    monkeypatch.setattr(_task_runtime, "_log_config_path", lambda: str(path))
    assert _task_runtime.ensure_log_config() == str(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["max_bytes"], int)
    path.write_text(json.dumps({"max_bytes": 7}), encoding="utf-8")
    _task_runtime.ensure_log_config()
    assert json.loads(path.read_text(encoding="utf-8")) == {"max_bytes": 7}


def _calls(fn_name: str, module: str) -> bool:
    tree = ast.parse((_BIN / module).read_text(encoding="utf-8"))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn_name for n in ast.walk(tree)
    )


def test_wired_where_the_governor_seed_is():
    for module in ("m3_cognitive_loop.py", "install_schedules.py"):
        assert _calls("ensure_governor_config", module), module
        assert _calls("ensure_log_config", module), f"{module} seeds the governor config but not the log config"
