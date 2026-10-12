"""`--set={...}`: one quoted TOML inline table immediately after `m3` (or a script
name); read once by the pre-parser, applied to the whole process, removed from
the command line (user decisions, m3 a6cdfc48, 7612b947)."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _st():
    from m3_core.config import settings

    return settings


def test_values_are_typed_like_the_file():
    got = _st().parse_set(
        '{search.row_cap = 50, db.backend = "postgres", llm.endpoints = ["http://a/v1", "http://b/v1"]}'
    )
    assert got == {"search.row_cap": 50, "db.backend": "postgres", "llm.endpoints": ["http://a/v1", "http://b/v1"]}


def test_a_nested_table_reads_like_dotted_keys():
    assert _st().parse_set("{search = {row_cap = 50}}") == {"search.row_cap": 50}


@pytest.mark.parametrize(
    "text, why",
    [
        ('{paths.engine_root = "/srv/x"}', "--config"),
        ('{secrets.file = "/srv/s.toml"}', "--config"),
        ('{M3_PRIMARY_PG_URL = "postgresql://x/y"}', "is a secret"),
        ('{M3_DB_BACKEND = "postgres"}', "the setting is db.backend"),
        ("{search.row_capp = 1}", "did you mean search.row_cap"),
        ('{search.row_cap = "lots"}', "search.row_cap"),
        ("{search.row_capp = 1}", "not a setting"),
        ("x=1", "quote the whole token"),
        ("{search.row_cap = }", "does not parse"),
        ("{db.backend = postgres}", r"Strings use \^\.\.\.\^"),
        ("{db.backend=^postgres}", "is not closed"),
    ],
)
def test_refused_with_the_reason(text, why):
    with pytest.raises(_st().OverrideError, match=why):
        _st().parse_set(text)


def test_a_secret_is_refused_by_name_without_echoing_a_value():
    from m3_core.config import REGISTRY, KeyClass

    secret = next(k for k in REGISTRY if k.klass is KeyClass.SECRET)
    with pytest.raises(_st().OverrideError) as e:
        _st().parse_set(f'{{{secret.name} = "hunter2"}}')
    assert "never flags" in str(e.value) and "hunter2" not in str(e.value)


def test_nothing_is_applied_unless_every_pair_is_valid():
    st = _st()
    with pytest.raises(st.OverrideError, match="row_capp"):
        st.apply_set(["m3", "--set={search.row_cap = 7, search.row_capp = 1}", "search"])
    assert st.origin_of("search.row_cap") != "cli"


@pytest.mark.parametrize(
    "argv, rest",
    [
        (["m3", "--set={search.row_cap=7}", "search", "q"], ["m3", "search", "q"]),
        (["m3", "--set", "{search.row_cap=7}", "search", "q"], ["m3", "search", "q"]),
        (["m3", "search", "--", "--set={x=1}"], ["m3", "search", "--", "--set={x=1}"]),
    ],
)
def test_the_token_is_removed_and_the_rest_passes_untouched(argv, rest):
    assert _st().apply_set(argv) == rest


def test_set_after_the_command_is_refused_not_passed_downstream():
    with pytest.raises(_st().OverrideError, match="immediately after the program name"):
        _st().apply_set(["m3", "search", "--set={search.row_cap=7}"])


def test_a_brace_expanded_token_is_refused():
    """bash turns --set={x=1,y=2} into --set=x=1 --set=y=2 (measured)."""
    with pytest.raises(_st().OverrideError, match="came in pieces"):
        _st().apply_set(["m3", "--set=search.row_cap=7", "--set=db.backend=postgres", "search"])


def test_two_set_tokens_say_to_merge_them():
    with pytest.raises(_st().OverrideError, match="only one --set"):
        _st().apply_set(["m3", "--set={search.row_cap = 7}", '--set={db.backend = "sqlite"}', "x"])


@pytest.mark.parametrize(
    "argv",
    [
        ["m3", "--set=M3_PRIMARY_PG_URL=postgresql://u:hunter2@h/db", "x"],
        ["m3", '--set={M3_PRIMARY_PG_URL = "postgresql://u:hunter2@h/db"}', "x"],
        ["m3", '--set={M3_PRIMARY_PG_URL = "postgresql://u:hunter2@h/db"', "x"],
    ],
    ids=["brace-less", "table", "malformed"],
)
def test_a_refusal_never_echoes_what_could_be_a_secret(argv):
    with pytest.raises(_st().OverrideError) as e:
        _st().apply_set(argv)
    assert "hunter2" not in str(e.value)


def test_every_refusal_says_nothing_was_applied_and_lists_each_problem():
    with pytest.raises(_st().OverrideError) as e:
        _st().parse_set('{search.row_capp = 1, paths.bin = "/x", search.row_cap = "lots"}')
    text = str(e.value)
    assert "nothing was applied" in text and len(e.value.problems) == 3


def test_the_value_reaches_every_reader_in_the_process():
    st = _st()
    st.apply_set(["m3", "--set={search.row_cap = 7}", "search"])
    assert st.setting("search.row_cap") == 7 and st.origin_of("search.row_cap") == "cli"


def test_cli_args_hand_the_same_values_to_a_child():
    st = _st()
    st.apply_set(["m3", '--set={search.row_cap = 7, llm.endpoints = ["http://a/v1"]}', "x"])
    [token] = st.cli_args()
    assert token.startswith("--set={")
    assert st.parse_set(token[len("--set=") :]) == {"search.row_cap": 7, "llm.endpoints": ["http://a/v1"]}


def test_early_argv_updates_sys_argv_and_stops_on_a_refusal(monkeypatch, capsys):
    from m3_core.paths import apply_early_argv

    monkeypatch.setattr(sys, "argv", ["sync_all.py", "--set={search.row_cap = 7}", "--dry-run"])
    apply_early_argv()
    assert sys.argv == ["sync_all.py", "--dry-run"]
    monkeypatch.setattr(sys, "argv", ["sync_all.py", '--set={paths.engine_root = "/x"}'])
    with pytest.raises(SystemExit) as done:
        apply_early_argv()
    assert done.value.code == 2 and "--config" in capsys.readouterr().err


def test_m3_applies_it_end_to_end():
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "m3_memory.cli",
            "--set={search.row_cap = 7}",
            "config",
            "get",
            "search.row_cap",
            "--origin",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[0] == "7" and "cli" in out.stdout, out.stdout


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{db.backend=^postgres^, search.row_cap=50}", {"db.backend": "postgres", "search.row_cap": 50}),
        ("{llm.endpoints=[^http://a/v1^, ^http://b/v1^]}", {"llm.endpoints": ["http://a/v1", "http://b/v1"]}),
        ("{llm.url=^http://h/^^x^}", {"llm.url": "http://h/^x"}),
        ("{llm.url=^a, b it's^}", {"llm.url": "a, b it's"}),
        ('{llm.url="http://h/^x"}', {"llm.url": "http://h/^x"}),
        ("{llm.url='lit^eral'}", {"llm.url": "lit^eral"}),
    ],
    ids=["strings", "list", "doubled-caret", "commas-apostrophe", "caret-in-toml-string", "caret-in-literal"],
)
def test_caret_strings_and_toml_strings_both_read(text, expected):
    """^...^ is the documented quote (one rule in every shell); the file's own
    "..." and '...' still work, and a ^ inside them is just a character."""
    assert _st().parse_set(text) == expected


def test_an_unclosed_caret_string_names_its_position_not_its_text():
    with pytest.raises(_st().OverrideError) as e:
        _st().parse_set("{llm.url=^postgresql://u:hunter2@h/db}")
    assert "character 10" in str(e.value) and "hunter2" not in str(e.value)


def test_m3_applies_a_caret_set_end_to_end():
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "m3_memory.cli",
            "--set={db.backend=^sqlite^, search.row_cap=9}",
            "config",
            "get",
            "search.row_cap",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "9", out.stdout
