"""Every launch of the in-process embed server gives it a log file.

Doctor's stop-gap start ran it with stdout/stderr to DEVNULL and no --log-file,
so when that server died nothing said why (2026-10-17). The boot task always
passed one; both now take the name from m3_core.paths.
"""

from __future__ import annotations

import importlib
import os

from m3_core.paths import EMBED_SERVER_INPROC_LOG, embed_server_inproc_log, get_m3_logs_root


def test_doctors_stop_gap_start_passes_a_log_file(monkeypatch):
    P = importlib.import_module("doctor.shared_embedder_probe")
    seen: list[list[str]] = []
    monkeypatch.setattr(P.subprocess, "Popen", lambda argv, **_k: seen.append(list(argv)))
    assert P._fix_start_server() is True
    argv = seen[0]
    log = argv[argv.index("--log-file") + 1]
    assert log == embed_server_inproc_log()
    assert os.path.dirname(log) == get_m3_logs_root() and log.endswith(EMBED_SERVER_INPROC_LOG)


def test_the_boot_task_uses_the_same_name():
    src = importlib.util.find_spec("install_schedules").origin or ""
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert '"embed_server_inproc.log"' not in text, "use m3_core.paths.EMBED_SERVER_INPROC_LOG"
    assert "_log(EMBED_SERVER_INPROC_LOG)" in text
