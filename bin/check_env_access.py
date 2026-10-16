#!/usr/bin/env python3
"""Inventory every process-environment access in m3's production code.

The configuration redesign moves every m3 setting behind ONE
resolver. Until then m3 reads and writes the environment from hundreds of
places, in more shapes than a grep finds: aliased modules (``import os as
_os``), ``from os import environ``, splat copies (``{**os.environ}``,
``dict(os.environ)``), ``subprocess.run(env=os.environ)``, ``load_dotenv()``,
dynamic names (``os.getenv(name)``), and wrappers such as ``getenv_compat``.
This gate walks the AST so each shape is counted, and so a planted violation
of each one is caught (``--selftest``).

DENY BY DEFAULT. A site is a violation unless it is in an allowed module (the
config package, once it exists, and the ``child_env`` helper) or reads a non-m3
OS name on the allow-list (HOME, PATH, ...). Writes, whole-environment copies
and dynamic names are violations wherever an allowed module is not.

OWNERS: a site whose JOB is reading
the environment -- an approved locator (the roots), the legacy-name shim, the
doctor / import-env scanners, the parity snapshot -- carries ``# env-owner: <why>``
on its line or alone on the line above. Its verdict is ``owner``, not
``violation``; owners have their own ratchet, so a new one is still reviewed. A
marker without a reason is ignored.

REPORT MODE: print the inventory and exit 0. The counts are a RATCHET
(tests/test_env_access_ratchet.py): they may only fall, and a
fall must lower the stored budget. ``--tests`` counts the other side, env WRITES
of non-OS names in tests/ (tests that configure m3 through the environment
instead of the ``m3_settings`` fixture). A file that does not parse is NOT
skipped silently -- it is reported and makes the run exit 2, because an
unscanned file is not a clean file (the same rule CI applies to bandit).

Usage:
    python bin/check_env_access.py                 # report: findings + summary
    python bin/check_env_access.py --json          # machine-readable report
    python bin/check_env_access.py --summary       # counts only
    python bin/check_env_access.py --tests         # env writes in tests/
    python bin/check_env_access.py --selftest      # planted violations must all be caught

Exit: 0 = report written (or selftest passed); 1 = selftest failed;
2 = a scanned file could not be parsed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import override

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("bin", "m3_memory")
# Root-level files that ship as production code. setup.py stages each into
# m3_memory/ at build time (a gitignored copy): the source is scanned and the
# staged copy skipped, so a local build and a clean CI checkout count the same.
ROOT_FILES = ("install_os.py",)

# Modules allowed to touch the environment; paths are repo-relative, POSIX style.
# The config package reads env in exactly one module, plus the child_env helper.
ALLOWED_MODULES: frozenset[str] = frozenset({"bin/m3_core/config/env.py", "m3_memory/_childenv.py"})

# Names m3 reads that are NOT m3 settings: the OS's (measured readers, plan
# review §4b), plus terminals' and other programs' own variables.
# Reads are allowed; a write is still a violation.
OS_NAMES: frozenset[str] = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "COMPUTERNAME",
        "HOSTNAME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "USERDOMAIN",
        "SHELL",
        "PATH",
        "SystemRoot",
        "TERM_PROGRAM",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",  # the login session's runtime dir (_platform.user_service_runtime_dir)
        # Set by Windows' OneDrive client: where it syncs (_platform.synced_folder_roots)
        "OneDrive",
        "OneDriveConsumer",
        "OneDriveCommercial",
        # Terminals' own
        "ANSICON",
        "FORCE_COLOR",
        "WT_SESSION",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "OLLAMA_MODELS",
        "OPENCLAW_STATE_DIR",
        "PYTHONUTF8",
        # Other programs' own variables:
        # GitHub's tokens (release tooling only), Hermes Agent's and OpenCode's homes.
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HERMES_HOME",
        "OPENCODE_DATA_DIR",
        # git's own: tests bound repository discovery to their temp dir with it.
        "GIT_CEILING_DIRECTORIES",
        # The OS's and toolchains' own. Reads only: a
        # write (GGML_LOG_LEVEL, PYTHONUTF8 for children) stays a violation until
        # child_env owns it.
        "SSH_CONNECTION",
        "SSH_TTY",
        "CUDA_PATH",
        "VULKAN_SDK",
        "RUSTUP_HOME",
        "PYTHONPATH",
        "NO_COLOR",
        "GGML_LOG_LEVEL",
    }
)

# Functions that read the environment on the caller's behalf: (name, index of the
# env-name argument). A call to one is a read of that name.
READ_WRAPPERS: dict[str, tuple[int, ...]] = {"getenv_compat": (0, 1)}

# A str.format() placeholder: {name}, {name!r}, {0:>4}. Doubled {{ }} are literal.
_PLACEHOLDER = re.compile(r"(?<!\{)\{[^{}\n]*\}(?!\})")
# A line that reads as code, not prose: an import, or a statement on os.*.
_CODE_LINE = re.compile(r"\s*(import \w|from [\w.]+ import |os\.(environ|getenv|putenv|unsetenv))")

# `# env-owner: <reason>`; the reason is mandatory (an unexplained owner is a violation).
_OWNER = re.compile(r"#\s*env-owner:\s*\S")

WRITE_METHODS = frozenset({"pop", "setdefault", "update", "clear", "popitem", "__setitem__", "__delitem__"})
COPY_METHODS = frozenset({"copy", "items", "keys", "values"})


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    kind: str  # read | write | copy | dotenv | alias | handout
    form: str  # the syntactic shape, e.g. "os.environ.get"
    name: str  # the variable name, "<dynamic>", or "*" for the whole environment
    verdict: str  # violation | allowed-os-name | allowed-module | owner


class _Scanner(ast.NodeVisitor):
    def __init__(
        self,
        rel: str,
        wrappers: dict[str, tuple[int, ...]] | None = None,
        bare: dict[str, tuple[int, ...]] | None = None,
    ) -> None:
        self.rel = rel
        self.wrappers = wrappers if wrappers is not None else dict(READ_WRAPPERS)  # qualified calls
        self.bare = bare if bare is not None else dict(READ_WRAPPERS)  # bare-name calls
        self.found: list[Finding] = []
        self.os_names: set[str] = {"os"}  # names bound to the os module
        self.environ_names: set[str] = set()  # names bound to os.environ
        self.getenv_names: set[str] = set()  # names bound to os.getenv
        self.putenv_names: set[str] = set()  # os.putenv / os.unsetenv aliases

    # ── binding discovery ──────────────────────────────────────────────────
    @override
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == "os":
                self.os_names.add(a.asname or "os")
        self.generic_visit(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for a in node.names:
                local = a.asname or a.name
                if a.name in ("environ", "environb"):
                    self.environ_names.add(local)
                    self._add(node, "alias", f"from os import {a.name}", "*")
                elif a.name == "getenv":
                    self.getenv_names.add(local)
                    self._add(node, "alias", "from os import getenv", "*")
                elif a.name in ("putenv", "unsetenv"):
                    self.putenv_names.add(local)
                    self._add(node, "alias", f"from os import {a.name}", "*")
        if node.module == "dotenv":
            for a in node.names:
                if a.name in ("load_dotenv", "dotenv_values", "find_dotenv"):
                    self._add(node, "dotenv", f"from dotenv import {a.name}", "*")
        self.generic_visit(node)

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        # ENV = os.environ  -> ENV is an alias of the environment
        if self._is_environ(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.environ_names.add(t.id)
                    self._add(node, "alias", f"{t.id} = os.environ", "*")
                else:  # self.env = os.environ, d[k] = os.environ: out of the gate's sight
                    self._add(node, "handout", "<target> = os.environ", "*")
        # os.environ[NAME] = value
        for t in node.targets:
            if isinstance(t, ast.Subscript) and self._is_environ(t.value):
                self._add(node, "write", "os.environ[...] =", self._name_of(t.slice))
        self.generic_visit(node)

    @override
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Subscript) and self._is_environ(node.target.value):
            self._add(node, "write", "os.environ[...] op=", self._name_of(node.target.slice))
        self.generic_visit(node)

    @override
    def visit_Delete(self, node: ast.Delete) -> None:
        for t in node.targets:
            if isinstance(t, ast.Subscript) and self._is_environ(t.value):
                self._add(node, "write", "del os.environ[...]", self._name_of(t.slice))
        self.generic_visit(node)

    # ── reads, writes, copies ─────────────────────────────────────────────
    @override
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_environ(node.value):
            self._add(node, "read", "os.environ[...]", self._name_of(node.slice))
        self.generic_visit(node)

    @override
    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.In, ast.NotIn)) and self._is_environ(right):
                self._add(node, "read", "NAME in os.environ", self._name_of(node.left))
        self.generic_visit(node)

    @override
    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values, strict=True):
            if k is None and self._is_environ(v):  # {**os.environ}
                self._add(node, "copy", "{**os.environ}", "*")
        self.generic_visit(node)

    @override
    def visit_For(self, node: ast.For) -> None:
        if self._is_environ(node.iter):
            self._add(node, "copy", "for k in os.environ", "*")
        self.generic_visit(node)

    @override
    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute):
            # os.getenv / os.putenv / os.unsetenv
            if self._is_os(f.value):
                if f.attr == "getenv":
                    self._add(node, "read", "os.getenv", self._arg_name(node, 0))
                elif f.attr in ("putenv", "unsetenv"):
                    self._add(node, "write", f"os.{f.attr}", self._arg_name(node, 0))
            # module.wrapper("M3_X"): a discovered read wrapper called qualified
            if (
                f.attr in self.wrappers
                and f.attr not in _GENERIC_ATTRS
                and not self._is_os(f.value)
                and not self._is_environ(f.value)
            ):
                for i in self.wrappers[f.attr]:
                    if i < len(node.args):
                        self._add(node, "read", f"{f.attr} (qualified)", self._arg_name(node, i))
            # os.environ.<method>(...)
            if self._is_environ(f.value):
                if f.attr == "get":
                    self._add(node, "read", "os.environ.get", self._arg_name(node, 0))
                elif f.attr in WRITE_METHODS:
                    name = self._arg_name(node, 0) if f.attr in ("pop", "setdefault") else "*"
                    self._add(node, "write", f"os.environ.{f.attr}", name)
                elif f.attr in COPY_METHODS:
                    self._add(node, "copy", f"os.environ.{f.attr}()", "*")
        elif isinstance(f, ast.Name):
            if f.id in self.getenv_names:
                self._add(node, "read", "getenv (aliased)", self._arg_name(node, 0))
            elif f.id in self.putenv_names:
                self._add(node, "write", f"{f.id} (aliased)", self._arg_name(node, 0))
            elif f.id in ("dict", "list", "set", "tuple", "sorted") and node.args and self._is_environ(node.args[0]):
                self._add(node, "copy", f"{f.id}(os.environ)", "*")
            elif f.id in ("load_dotenv", "dotenv_values"):
                self._add(node, "dotenv", f"{f.id}()", "*")
            elif f.id in self.bare:
                for i in self.bare[f.id]:
                    if i < len(node.args):
                        self._add(node, "read", f.id, self._arg_name(node, i))
        # env=os.environ handed to a child process
        for kw in node.keywords:
            if kw.arg == "env" and self._is_environ(kw.value):
                self._add(node, "copy", "env=os.environ", "*")
            elif self._is_environ(kw.value):
                self._add(node, "handout", f"{kw.arg}=os.environ", "*")
        # f(os.environ): the whole environment handed to code the gate cannot see
        if not (isinstance(f, ast.Name) and f.id in ("dict", "list", "set", "tuple", "sorted")):
            for a in node.args:
                if self._is_environ(a):
                    self._add(node, "handout", "f(os.environ)", "*")
        self.generic_visit(node)

    @override
    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None and self._is_environ(node.value):
            self._add(node, "handout", "return os.environ", "*")
        self.generic_visit(node)

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "environb" and self._is_os(node.value):
            self._add(node, "read", "os.environb", "*")
        self.generic_visit(node)

    # ── helpers ───────────────────────────────────────────────────────────
    def _is_os(self, n: ast.AST) -> bool:
        return isinstance(n, ast.Name) and n.id in self.os_names

    def _is_environ(self, n: ast.AST) -> bool:
        if isinstance(n, ast.Attribute) and n.attr in ("environ", "environb") and self._is_os(n.value):
            return True
        return isinstance(n, ast.Name) and n.id in self.environ_names

    @staticmethod
    def _name_of(n: ast.AST) -> str:
        return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else "<dynamic>"

    def _arg_name(self, call: ast.Call, i: int) -> str:
        if i < len(call.args):
            return self._name_of(call.args[i])
        for kw in call.keywords:
            if kw.arg in ("key", "name", "new_name"):
                return self._name_of(kw.value)
        return "<dynamic>"

    def _add(self, node: ast.AST, kind: str, form: str, name: str) -> None:
        if self.rel in ALLOWED_MODULES:
            verdict = "allowed-module"
        elif kind == "read" and name in OS_NAMES:
            verdict = "allowed-os-name"
        else:
            verdict = "violation"
        self.found.append(Finding(self.rel, getattr(node, "lineno", 0), kind, form, name, verdict))


def _embedded_programs(tree: ast.AST):
    """String constants that are themselves Python programs touching the env.

    m3 runs child programs kept as source strings (``python -c CODE``,
    ``search_differential._PROBE``); their env writes are real but invisible to
    an AST that sees only a string. A multi-line constant that mentions the
    environment and parses as a module is scanned as code; a ``.format()``
    template is parsed with each ``{...}`` placeholder replaced by a name. One
    that still does not parse is yielded with source None, so the caller reports
    it rather than skipping it silently. Yields (source | None, first line)."""
    docstrings = {
        id(b[0].value)
        for b in (getattr(x, "body", None) for x in ast.walk(tree))
        if isinstance(b, list)
        and b
        and isinstance(b[0], ast.Expr)
        and isinstance(b[0].value, ast.Constant)
        and isinstance(b[0].value.value, str)
    }
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "\n" in n.value
            and id(n) not in docstrings
            and ("environ" in n.value or "getenv" in n.value)
            and any(_CODE_LINE.match(line) for line in n.value.splitlines())
        ):
            for candidate in (n.value, _PLACEHOLDER.sub("_", n.value)):
                try:
                    ast.parse(candidate)
                except SyntaxError:
                    continue
                yield candidate, n.lineno
                break
            else:
                yield None, n.lineno


# ── read wrappers, discovered (found 2026-10-09: llm_failover._flag(name) reads
# os.environ.get(name), so the names passed to it were invisible; a wrapper list
# kept by hand goes stale, so it is derived from the code) ─────────────────────
_GENERIC_ATTRS = frozenset({"get", "getenv", "pop", "setdefault"})


def _env_read_arg(call: ast.Call, wrappers: dict[str, tuple[int, ...]]) -> list[ast.expr]:
    """The argument nodes a call reads as env NAMES: os.environ.get(x), os.getenv(x),
    or a known wrapper's name positions."""
    f = call.func
    if isinstance(f, ast.Attribute):
        if f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "environ":
            return call.args[:1]
        if f.attr == "getenv":
            return call.args[:1]
        name = f.attr if f.attr not in _GENERIC_ATTRS else None
    elif isinstance(f, ast.Name):
        name = f.id
    else:
        name = None
    if name is not None and name in wrappers:
        return [call.args[i] for i in wrappers[name] if i < len(call.args)]
    return []


def _wrappers_in(tree: ast.AST, known: dict[str, tuple[int, ...]]) -> dict[str, tuple[int, ...]]:
    """Functions that read env with one of their own parameters as the NAME."""
    out: dict[str, tuple[int, ...]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        hits: set[int] = set()
        for node in ast.walk(fn):
            args: list[ast.expr] = []
            if isinstance(node, ast.Call):
                args = _env_read_arg(node, known)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
            ):
                args = [node.slice]
            hits |= {params.index(a.id) for a in args if isinstance(a, ast.Name) and a.id in params}
        if hits:
            out[fn.name] = tuple(sorted(hits))
    return out


def discover_wrappers(trees: list[ast.AST]) -> dict[str, tuple[int, ...]]:
    """Every read wrapper across the trees, to a fixed point (a wrapper may call one)."""
    wrappers = dict(READ_WRAPPERS)
    while True:
        found: dict[str, tuple[int, ...]] = {}
        for tree in trees:
            found.update(_wrappers_in(tree, wrappers))
        merged = {**wrappers, **found}
        if merged == wrappers:
            return wrappers
        wrappers = merged


def _bare_wrappers(tree: ast.AST, wrappers: dict[str, tuple[int, ...]]) -> dict[str, tuple[int, ...]]:
    """The wrappers a BARE call in this module can mean: ones it defines, and ones it
    imports by name, unless a same-named local function (one that does not read
    env) shadows it. Matching by name alone mistook the registry's own `_flag`
    helper for llm_failover's `_flag` reader (found 2026-10-09)."""
    local_defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    local_wrappers = _wrappers_in(tree, wrappers)
    imported = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    out = dict(local_wrappers)
    for name in imported:
        if name in wrappers and name not in local_defs:
            out[name] = wrappers[name]
    if "getenv_compat" in imported or "getenv_compat" in local_wrappers:
        out["getenv_compat"] = READ_WRAPPERS["getenv_compat"]
    return out


def scan_source(src: str, rel: str, wrappers: dict[str, tuple[int, ...]] | None = None) -> list[Finding]:
    tree = ast.parse(src, filename=rel)
    lines = src.splitlines()
    known = wrappers if wrappers is not None else discover_wrappers([tree])
    s = _Scanner(rel, known, _bare_wrappers(tree, known))
    s.visit(tree)
    found = list(s.found)
    for code, first_line in _embedded_programs(tree):
        if code is None:  # mentions the env, cannot be scanned: say so
            found.append(Finding(rel, first_line, "unparsed", "embedded program (unparsable)", "*", "violation"))
            continue
        inner = _Scanner(rel, s.wrappers, s.bare)
        inner.visit(ast.parse(code))
        found += [
            Finding(f.path, first_line + f.line - 1, f.kind, f"{f.form} (embedded program)", f.name, f.verdict)
            for f in inner.found
        ]
    return sorted({_with_owner(f, lines) for f in found}, key=lambda f: (f.path, f.line, f.form, f.name))


def _with_owner(f: Finding, lines: list[str]) -> Finding:
    """``f`` with verdict ``owner`` when its line, or a lone comment line directly
    above it, carries ``# env-owner: <reason>``. Embedded programs never qualify."""
    if f.verdict != "violation" or "(embedded program)" in f.form or not 0 < f.line <= len(lines):
        return f
    here = lines[f.line - 1]
    above = lines[f.line - 2] if f.line >= 2 else ""
    if _OWNER.search(here) or (above.lstrip().startswith("#") and _OWNER.search(above)):
        return Finding(f.path, f.line, f.kind, f.form, f.name, "owner")
    return f


def _production_files() -> list[Path]:
    out = [REPO / name for name in ROOT_FILES if os.path.isfile(REPO / name)]
    staged = {(REPO / "m3_memory" / name).resolve() for name in ROOT_FILES}
    for root in SCAN_ROOTS:
        for p in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts or p.name.startswith(("bench_", "benchmark_", "test_")):
                continue
            if p.resolve() == Path(__file__).resolve():  # the gate's own planted snippets
                continue
            if p.resolve() in staged:
                continue
            out.append(p)
    return out


def _patch_dict_names(call: ast.Call) -> list[str] | None:
    """Names a ``patch.dict(os.environ, {...})`` call writes, or None if it is not one."""
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr == "dict" and call.args):
        return None
    target = call.args[0]
    is_environ = (isinstance(target, ast.Attribute) and target.attr == "environ") or (
        isinstance(target, ast.Constant) and target.value == "os.environ"
    )
    if not is_environ:
        return None
    names: list[str] = []
    values = call.args[1] if len(call.args) > 1 else None
    if isinstance(values, ast.Dict):
        for k in values.keys:
            names.append(k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else "<dynamic>")
    elif values is not None:
        names.append("<dynamic>")
    names += [kw.arg for kw in call.keywords if kw.arg and kw.arg != "clear"]
    if any(kw.arg == "clear" for kw in call.keywords) and not names:
        names.append("*")
    return names


def scan_test_source(src: str, rel: str) -> list[Finding]:
    """Environment WRITES in a test file: monkeypatch setenv/delenv, patch.dict on
    os.environ, and the production write forms. OS names (``OS_NAMES``) are not counted:
    the ratchet tracks tests that configure m3 through the environment instead of the
    ``m3_settings`` fixture."""
    tree = ast.parse(src, filename=rel)
    found: list[Finding] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in ("setenv", "delenv") and n.args:
            a = n.args[0]
            name = a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "<dynamic>"
            found.append(Finding(rel, n.lineno, "write", f"monkeypatch.{f.attr}", name, "violation"))
            continue
        names = _patch_dict_names(n)
        if names is not None:
            found += [Finding(rel, n.lineno, "write", "patch.dict(os.environ)", nm, "violation") for nm in names]
    found += [f for f in scan_source(src, rel, {}) if f.kind == "write"]
    return sorted({f for f in found if f.name not in OS_NAMES}, key=lambda f: (f.path, f.line, f.form, f.name))


def scan_tests() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    unparsed: list[str] = []
    for p in sorted((REPO / "tests").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(REPO).as_posix()
        try:
            findings += scan_test_source(p.read_text(encoding="utf-8"), rel)
        except (SyntaxError, UnicodeDecodeError) as e:
            unparsed.append(f"{rel}: {type(e).__name__}: {e}")
    return findings, unparsed


def scan_repo() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    unparsed: list[str] = []
    sources: list[tuple[str, str, ast.AST]] = []
    for p in _production_files():
        rel = p.relative_to(REPO).as_posix()
        try:
            src = p.read_text(encoding="utf-8")
            sources.append((rel, src, ast.parse(src, filename=rel)))
        except (SyntaxError, UnicodeDecodeError) as e:
            unparsed.append(f"{rel}: {type(e).__name__}: {e}")
    wrappers = discover_wrappers([t for _, _, t in sources])
    for rel, src, _tree in sources:
        findings.extend(scan_source(src, rel, wrappers))
    return findings, unparsed


# ── selftest: one planted snippet per shape; each must be flagged ─────────────
PLANTED: list[tuple[str, str, str]] = [
    # (snippet, expected kind, expected form)
    ("import os\nos.getenv('M3_X')", "read", "os.getenv"),
    ("import os\nos.environ.get('M3_X')", "read", "os.environ.get"),
    ("import os\nos.environ['M3_X']", "read", "os.environ[...]"),
    ("import os\n'M3_X' in os.environ", "read", "NAME in os.environ"),
    ("from os import environ\nenviron.get('M3_X')", "read", "os.environ.get"),
    ("from os import getenv as g\ng('M3_X')", "read", "getenv (aliased)"),
    ("import os\nos.environ.copy()['M3_X']", "copy", "os.environ.copy()"),
    ("import os\nos.environ['M3_X'] = '1'", "write", "os.environ[...] ="),
    ("import os\nname = 'M3_X'\nos.getenv(name)", "read", "os.getenv"),
    ("import os as _os\n_os.environ.copy()", "copy", "os.environ.copy()"),
    ("import os\nx = {**os.environ}", "copy", "{**os.environ}"),
    ("import os\ndict(os.environ)", "copy", "dict(os.environ)"),
    ("import os\nos.environ.items()", "copy", "os.environ.items()"),
    ("import os\nos.environb", "read", "os.environb"),
    ("import os, subprocess\nsubprocess.run(['x'], env=os.environ)", "copy", "env=os.environ"),
    ("from dotenv import load_dotenv\nload_dotenv()", "dotenv", "load_dotenv()"),
    ("import os\nos.environ.pop('M3_X', None)", "write", "os.environ.pop"),
    ("import os\nos.environ.setdefault('M3_X', '1')", "write", "os.environ.setdefault"),
    ("import os\nos.environ.update({'M3_X': '1'})", "write", "os.environ.update"),
    ("import os\ndel os.environ['M3_X']", "write", "del os.environ[...]"),
    ("import os\nos.putenv('M3_X', '1')", "write", "os.putenv"),
    ("import os\nos.unsetenv('M3_X')", "write", "os.unsetenv"),
    ("import os\nos.environ.clear()", "write", "os.environ.clear"),
    ("from m3_core.paths import getenv_compat\ngetenv_compat('M3_X', 'X')", "read", "getenv_compat"),
    ("import os\nE = os.environ\nE.get('M3_X')", "read", "os.environ.get"),
    ("CHILD = '''\nimport os\nos.environ['M3_X'] = '1'\n'''", "write", "os.environ[...] = (embedded program)"),
    (
        "T = '''\nimport os\nos.environ['M3_X'] = '1'\nfor q in {queries!r}:\n    pass\n'''",
        "write",
        "os.environ[...] = (embedded program)",
    ),
    ("T = '''\nimport os\nos.environ['M3_X'] = (\n'''", "unparsed", "embedded program (unparsable)"),
    # The whole environment handed out, readable by code the gate never sees
    # (found 2026-10-08: `return os.environ` produced no finding at all).
    ("import os\ndef f():\n    return os.environ", "handout", "return os.environ"),
    ("import os\ng(os.environ)", "handout", "f(os.environ)"),
    ("import os\ng(mapping=os.environ)", "handout", "mapping=os.environ"),
    ("import os\nclass C:\n    def __init__(self):\n        self.env = os.environ", "handout", "<target> = os.environ"),
    # A read through a wrapper the gate DISCOVERS (found 2026-10-09: names passed
    # to llm_failover._flag / _setting were invisible), directly and transitively.
    ("import os\ndef _flag(name):\n    return os.environ.get(name)\n_flag('M3_X')", "read", "_flag"),
    ("import os\ndef _s(n):\n    return os.environ.get(n)\ndef _f(n):\n    return _s(n)\n_f('M3_X')", "read", "_f"),
]

# Must NOT be reported: a same-named local helper that does not read env.
_NOT_READS = [
    "def _flag(name, env, default):\n    return (name, env, default)\n_flag('db.backend', ('M3_X',), 1)",
]


# --tests: (snippet, expected form, expected name); each must be counted.
_PLANTED_TEST_WRITES: list[tuple[str, str, str]] = [
    ("def t(monkeypatch):\n    monkeypatch.setenv('M3_X', '1')", "monkeypatch.setenv", "M3_X"),
    ("def t(mp):\n    mp.delenv('M3_X', raising=False)", "monkeypatch.delenv", "M3_X"),
    ("def t(monkeypatch):\n    n = 'M3_X'\n    monkeypatch.setenv(n, '1')", "monkeypatch.setenv", "<dynamic>"),
    (
        "import os\nfrom unittest import mock\n@mock.patch.dict(os.environ, {'M3_X': '1'})\ndef t():\n    pass",
        "patch.dict(os.environ)",
        "M3_X",
    ),
    (
        "from unittest.mock import patch\ndef t():\n    with patch.dict('os.environ', M3_X='1'):\n        pass",
        "patch.dict(os.environ)",
        "M3_X",
    ),
    ("import os\ndef t():\n    os.environ['M3_X'] = '1'", "os.environ[...] =", "M3_X"),
]


def selftest() -> int:
    failures = []
    for snippet, kind, form in PLANTED:
        hits = [f for f in scan_source(snippet, "planted.py") if f.verdict == "violation"]
        if not any(f.kind == kind and f.form == form for f in hits):
            failures.append(f"NOT CAUGHT ({kind} / {form}): {snippet!r} -> {[(f.kind, f.form) for f in hits]}")
    # the dynamic-name case must say so
    dyn = scan_source("import os\nname='M3_X'\nos.getenv(name)", "planted.py")
    if not any(f.name == "<dynamic>" for f in dyn):
        failures.append("dynamic name not reported as <dynamic>")
    # an allow-listed OS name is not a violation
    ok = scan_source("import os\nos.environ.get('HOME')", "planted.py")
    if any(f.verdict == "violation" for f in ok):
        failures.append("allow-listed OS name HOME reported as a violation")
    # a same-named helper that does not read env is not a wrapper (false-positive guard)
    for snippet in _NOT_READS:
        hits = [f for f in scan_source(snippet, "planted.py") if f.verdict == "violation"]
        if hits:
            failures.append(f"FALSE POSITIVE: {snippet!r} -> {[(f.form, f.name) for f in hits]}")
    # --tests: each test-side write shape is counted, and an OS name is not
    for snippet, form, name in _PLANTED_TEST_WRITES:
        hits = scan_test_source(snippet, "planted_test.py")
        if not any(f.form == form and f.name == name for f in hits):
            failures.append(
                f"TEST WRITE NOT CAUGHT ({form} / {name}): {snippet!r} -> {[(f.form, f.name) for f in hits]}"
            )
    if scan_test_source("def t(monkeypatch):\n    monkeypatch.setenv('HOME', '/x')", "planted_test.py"):
        failures.append("test write of the OS name HOME was counted")
    # an owner marker with a reason makes the site an owner, on its line or above it;
    # without a reason it is still a violation
    for snippet, want in (
        ("import os\nos.getenv(n)  # env-owner: the roots locator", "owner"),
        ("import os\n# env-owner: the roots locator\nos.getenv(n)", "owner"),
        ("import os\nos.getenv(n)  # env-owner:", "violation"),
        ("import os\nx = 1  # env-owner: not this line\n\nos.getenv(n)", "violation"),
    ):
        got = [f.verdict for f in scan_source(snippet, "planted.py") if f.kind == "read"]
        if got != [want]:
            failures.append(f"OWNER MARKER: {snippet!r} -> {got}, expected [{want!r}]")
    total = len(PLANTED) + 2 + len(_NOT_READS) + len(_PLANTED_TEST_WRITES) + 1 + 4
    for f in failures:
        print(f, file=sys.stderr)
    print(f"selftest: {total - len(failures)}/{total} checks passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="every planted violation must be caught")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    ap.add_argument(
        "--tests",
        action="store_true",
        help="inventory env WRITES of non-OS names in tests/ instead of production reads",
    )
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.tests:
        tfind, tunparsed = scan_tests()
        if args.json:
            print(json.dumps({"findings": [asdict(f) for f in tfind], "unparsed": tunparsed}, indent=2))
        else:
            if not args.summary:
                for f in tfind:
                    print(f"{f.path}:{f.line}: {f.form:28} {f.name}")
            names = Counter(f.name for f in tfind)
            print(
                f"\nTESTS: {len(tfind)} env writes of non-OS names in {len({f.path for f in tfind})} files; "
                f"by form {dict(sorted(Counter(f.form for f in tfind).items()))}; {len(names)} distinct names"
            )
        for u in tunparsed:
            print(f"UNPARSED (not scanned, not clean): {u}", file=sys.stderr)
        return 2 if tunparsed else 0

    findings, unparsed = scan_repo()
    violations = [f for f in findings if f.verdict == "violation"]
    by_kind = Counter(f.kind for f in violations)
    copies = Counter(f.form for f in violations if f.kind == "copy")
    if args.json:
        print(
            json.dumps(
                {"findings": [asdict(f) for f in findings], "unparsed": unparsed, "violations_by_kind": dict(by_kind)},
                indent=2,
            )
        )
    else:
        if not args.summary:
            for f in findings:
                print(f"{f.path}:{f.line}: {f.verdict:15} {f.kind:6} {f.form:28} {f.name}")
        files = len({f.path for f in violations})
        print(
            f"\nREPORT MODE (exit 0): {len(violations)} violations in {files} files; "
            f"by kind {dict(sorted(by_kind.items()))}; whole-env copies by form {dict(sorted(copies.items()))}; "
            f"allowed OS-name reads {sum(f.verdict == 'allowed-os-name' for f in findings)}"
        )
    for u in unparsed:
        print(f"UNPARSED (not scanned, not clean): {u}", file=sys.stderr)
    return 2 if unparsed else 0


if __name__ == "__main__":
    sys.exit(main())
