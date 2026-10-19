"""Each agent host's chat capture, end to end, the way a user's install runs it.

For each host in tests/host_capture.py: a throwaway m3 (its own config root and
stores), the launchers m3 setup installs, and then the host's own way in:

- "line" hosts (Claude Code, Codex, Gemini CLI, Antigravity): the exact command
  setup writes into the host's config, fed the host's recorded hook input;
- "js" hosts (OpenCode, OpenClaw): the JavaScript setup installs in the host,
  run under Node with the host's recorded event, which starts that same line;
- Aider: the command its watcher runs.

Then the turns are read back through m3, and the session's final reply must be
the last turn stored. The host-capture workflow runs one host per job; locally:
``pytest -m slow tests/test_host_capture.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from host_capture import HOSTS

from conftest import skip_reason

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hosts"
BIN = Path(__file__).resolve().parents[1] / "bin"
_NODE = shutil.which("node")

pytestmark = pytest.mark.slow

_READ_TURNS = """
import json
from m3_memory import _bin
_bin.ensure_on_path()
import chatlog_config
from m3_core.paths import pin_database
from memory.backends import active_backend
pin_database(chatlog_config.chatlog_db_path())
try:
    with active_backend().connection() as c:
        rows = c.execute(
            "SELECT content, metadata_json FROM memory_items WHERE type = 'chat_log' ORDER BY valid_from, created_at"
        ).fetchall()
finally:
    pin_database(None)
print(json.dumps([[json.loads(m or "{}").get("role"), t] for t, m in rows]))
"""


class Sandbox:
    """A throwaway m3 install: config root, stores, home; nothing outside tmp."""

    def __init__(self, tmp: Path):
        self.root = tmp
        self.home = tmp / "home"
        self.cfg = tmp / "m3" / "config"
        self.engine = tmp / "m3" / "engine"
        self.work = tmp / "work"
        for d in (self.home, self.cfg, self.engine, self.work, tmp / "m3" / "logs"):
            d.mkdir(parents=True)
        (self.cfg / "m3_config.toml").write_text(
            "schema_version = 1\n[paths]\n"
            f'engine_root = "{self.engine.as_posix()}"\n'
            f'logs_root = "{(tmp / "m3" / "logs").as_posix()}"\n',
            encoding="utf-8",
        )
        # HOME too: a hook's failure fallback writes under the home folder.
        self.env = {
            **os.environ,
            "M3_CONFIG_ROOT": str(self.cfg),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
        }

    def run(self, argv, *, input_text: str = "", extra_env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            input=input_text,
            env={**self.env, **(extra_env or {})},
            cwd=self.work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def python(self, code: str, *args: str) -> str:
        r = self.run([sys.executable, "-c", code, *args])
        assert r.returncode == 0, r.stderr[-2000:]
        return r.stdout.strip().splitlines()[-1]

    def require_installed_checkout(self) -> None:
        """The hook line runs whatever m3 this interpreter has INSTALLED (the
        launcher imports m3_memory with -E -s, from a folder outside the
        checkout). A suite run from a checkout with no install, or another
        version installed, would test nothing or the wrong code: skip, named.
        The host-capture workflow installs the checkout first."""
        code = (
            "import json, os, m3_memory; from m3_memory._bin import bin_dir; "
            "print(json.dumps([os.path.dirname(m3_memory.__file__), str(bin_dir() or '')]))"
        )
        r = self.run([sys.executable, "-E", "-s", "-c", code])
        if r.returncode != 0 or not r.stdout.strip():
            pytest.skip(skip_reason("env", f"m3 is not installed for {sys.executable}; the hook line needs an install"))
        pkg, bin_dir = (Path(p) for p in json.loads(r.stdout.strip().splitlines()[-1]))
        repo = Path(__file__).resolve().parents[1]
        pairs = [(pkg / n, repo / "m3_memory" / n) for n in ("hook_launchers.py", "host_plugins.py")]
        pairs += [(bin_dir / n, repo / "bin" / n) for n in ("chatlog_ingest.py", "chatlog_core.py")]
        pairs += [(bin_dir / p.relative_to(repo / "bin"), p) for p in (repo / "bin" / "hooks" / "chatlog").glob("*.py")]
        for installed, ours in pairs:
            if not installed.is_file() or installed.read_bytes() != ours.read_bytes():
                pytest.skip(
                    skip_reason(
                        "env", f"the m3 installed for {sys.executable} is not this checkout ({installed} differs)"
                    )
                )

    def hook_argv(self, hook: str) -> list[str]:
        """What setup writes for ``hook``: launchers installed, the line as argv."""
        self.require_installed_checkout()
        code = (
            "import json, sys; from m3_memory import hook_launchers as h; h.install_launchers(); "
            "print(json.dumps([h.command_for(sys.argv[1], sys.executable), h.argv_for(sys.argv[1], sys.executable)]))"
        )
        line, argv = json.loads(self.python(code, hook))
        assert shlex.split(line) == argv, (line, argv)  # the config line and the JS argv are the same command
        return argv

    def turns(self) -> list[list[str]]:
        return json.loads(self.python(_READ_TURNS))


def _fill(text: str, transcript: Path, cwd: Path) -> str:
    return text.replace("{transcript}", transcript.as_posix()).replace("{cwd}", cwd.as_posix())


def _run_node(sb: Sandbox, module_text: str, call: str, extra_env: dict) -> subprocess.CompletedProcess:
    module = sb.root / "plugin.mjs"
    module.write_text(module_text, encoding="utf-8")
    script = f"import({json.dumps(module.as_uri())}).then(async (m) => {{ {call} }});"
    return sb.run([_NODE, "--input-type=module", "-e", script], extra_env=extra_env)


@pytest.mark.parametrize("host", sorted(HOSTS), ids=lambda h: h.replace("-", "_"))
def test_capture(host, tmp_path):
    spec = HOSTS[host]
    fx = FIXTURES / host
    expected = json.loads((fx / "expected.json").read_text(encoding="utf-8"))
    sb = Sandbox(tmp_path)
    extra_env: dict[str, str] = {}

    if host == "opencode":
        store = sb.root / "opencode-data"
        for rel, obj in json.loads((fx / "store.json").read_text(encoding="utf-8")).items():
            (store / rel).parent.mkdir(parents=True, exist_ok=True)
            (store / rel).write_text(json.dumps(obj), encoding="utf-8")
        extra_env["OPENCODE_DATA_DIR"] = str(store)  # OpenCode's own setting for where its store is
        transcript = store
    elif host == "openclaw":
        state = sb.root / "openclaw-state"
        sid = "host-ci-openclaw-0001"
        transcript = state / "agents" / "main" / "sessions" / f"{sid}.jsonl"
        transcript.parent.mkdir(parents=True)
        shutil.copy(fx / "transcript.jsonl", transcript)
        extra_env["OPENCLAW_STATE_DIR"] = str(state)
    else:
        src = next(p for p in fx.iterdir() if p.name.startswith("transcript"))
        transcript = sb.work / src.name
        shutil.copy(src, transcript)

    if spec.runs == "line":
        argv = sb.hook_argv(spec.hook)
        envelope = _fill((fx / "envelope.json").read_text(encoding="utf-8"), transcript, sb.work)
        r = sb.run(argv, input_text=envelope, extra_env=extra_env)
    elif spec.runs == "js":
        if _NODE is None:
            pytest.skip(skip_reason("dep", f"node is not installed; {host} runs m3's capture from JavaScript"))
        from m3_memory import host_plugins

        argv = sb.hook_argv(spec.hook)
        event = _fill((fx / "event.json").read_text(encoding="utf-8"), transcript, sb.work)
        if host == "opencode":
            text = host_plugins.opencode_plugin_text(argv)
            call = (
                f"const h = await m.M3Capture({{ directory: {json.dumps(sb.work.as_posix())} }});"
                f"await h.event({{ event: {event} }});"
            )
        else:
            text = host_plugins.openclaw_handler_text(argv)
            call = f"m.default({event});"
        r = _run_node(sb, text, call, extra_env)
    else:  # Aider: the command bin/hooks/chatlog/aider_chat_watcher.sh runs
        r = sb.run(
            [
                sys.executable,
                str(BIN / "chatlog_ingest.py"),
                "--format",
                "aider",
                "--transcript-path",
                str(transcript),
                "--variant",
                "session_end",
            ]
        )
    assert r.returncode == 0, f"{host} capture exited {r.returncode}:\n{r.stdout[-1500:]}\n{r.stderr[-2500:]}"

    turns = sb.turns()
    assert len(turns) == expected["turns"], (host, turns)
    assert turns[0] == ["user", expected["first_user"]], (host, turns[0])
    role, content = turns[-1]
    assert role == "assistant" and expected["final_reply"] in content, (
        f"{host}: the session's final reply was not the last turn stored: {turns[-1]}"
    )


def test_every_host_has_a_recorded_input():
    for host, spec in HOSTS.items():
        fx = FIXTURES / host
        assert (fx / "expected.json").is_file(), host
        wanted = {"line": "envelope.json", "js": "event.json", "watcher": None}[spec.runs]
        if wanted:
            assert (fx / wanted).is_file(), (host, wanted)


def test_the_change_map_picks_hosts():
    from host_capture import hosts_for

    assert hosts_for(["bin/hooks/chatlog/codex_stop.py"]) == ["codex"]
    assert hosts_for(["m3_memory/host_plugins.py"]) == ["opencode", "openclaw"]
    assert hosts_for(["tests/fixtures/hosts/aider/transcript.md"]) == ["aider"]
    assert hosts_for(["bin/chatlog_ingest.py"]) == list(HOSTS)
    assert hosts_for(["README.md", "docs/CHATLOG.md"]) == []
    for spec in HOSTS.values():  # every hook named is one setup can install
        if spec.hook:
            from m3_memory.hook_launchers import HOOKS

            assert spec.hook in HOOKS, spec.hook
