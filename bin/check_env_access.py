#!/usr/bin/env python3
"""Inventory every process-environment access in m3's production code (plan Step 2A.1).

The configuration redesign (CONFIG_DESIGN) moves every m3 setting behind ONE
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

REPORT MODE (this step): print the inventory and exit 0, so the baseline can be
reviewed before the gate enforces anything. A file that does not parse is NOT
skipped silently -- it is reported and makes the run exit 2, because an
unscanned file is not a clean file (the same rule CI applies to bandit).

Usage:
    python bin/check_env_access.py                 # report: findings + summary
    python bin/check_env_access.py --json          # machine-readable report
    python bin/check_env_access.py --summary       # counts only
    python bin/check_env_access.py --selftest      # planted violations must all be caught

Exit: 0 = report written (or selftest passed); 1 = selftest failed;
2 = a scanned file could not be parsed.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("bin", "m3_memory")

# Modules allowed to touch the environment. Empty until the config package and
# the child_env helper exist (Phase 2C); paths are repo-relative, POSIX style.
ALLOWED_MODULES: frozenset[str] = frozenset()

# Names m3 reads that are NOT m3 settings: the OS's (measured readers, plan
# review §4b), plus terminals' and other programs' own variables (U4 bucket A,
# user decision 2026-10-08). Reads are allowed; a write is still a violation.
OS_NAMES: frozenset[str] = frozenset({
    "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMPUTERNAME", "HOSTNAME",
    "USER", "USERNAME", "LOGNAME", "USERDOMAIN", "SHELL", "PATH", "SystemRoot",
    "TERM_PROGRAM", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # U4 bucket A
    "ANSICON", "FORCE_COLOR", "WT_SESSION", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "OLLAMA_MODELS", "OPENCLAW_STATE_DIR", "PYTHONUTF8",
})

# Functions that read the environment on the caller's behalf: (name, index of the
# env-name argument). A call to one is a read of that name.
READ_WRAPPERS: dict[str, tuple[int, ...]] = {"getenv_compat": (0, 1)}

# A str.format() placeholder: {name}, {name!r}, {0:>4}. Doubled {{ }} are literal.
_PLACEHOLDER = re.compile(r"(?<!\{)\{[^{}\n]*\}(?!\})")
# A line that reads as code, not prose: an import, or a statement on os.*.
_CODE_LINE = re.compile(r"\s*(import \w|from [\w.]+ import |os\.(environ|getenv|putenv|unsetenv))")

WRITE_METHODS = frozenset({"pop", "setdefault", "update", "clear", "popitem", "__setitem__", "__delitem__"})
COPY_METHODS = frozenset({"copy", "items", "keys", "values"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str      # read | write | copy | dotenv | alias
    form: str      # the syntactic shape, e.g. "os.environ.get"
    name: str      # the variable name, "<dynamic>", or "*" for the whole environment
    verdict: str   # violation | allowed-os-name | allowed-module


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.found: list[Finding] = []
        self.os_names: set[str] = {"os"}            # names bound to the os module
        self.environ_names: set[str] = set()        # names bound to os.environ
        self.getenv_names: set[str] = set()         # names bound to os.getenv
        self.putenv_names: set[str] = set()         # os.putenv / os.unsetenv aliases

    # ── binding discovery ──────────────────────────────────────────────────
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == "os":
                self.os_names.add(a.asname or "os")
        self.generic_visit(node)

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

    def visit_Assign(self, node: ast.Assign) -> None:
        # ENV = os.environ  -> ENV is an alias of the environment
        if self._is_environ(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.environ_names.add(t.id)
                    self._add(node, "alias", f"{t.id} = os.environ", "*")
        # os.environ[NAME] = value
        for t in node.targets:
            if isinstance(t, ast.Subscript) and self._is_environ(t.value):
                self._add(node, "write", "os.environ[...] =", self._name_of(t.slice))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Subscript) and self._is_environ(node.target.value):
            self._add(node, "write", "os.environ[...] op=", self._name_of(node.target.slice))
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for t in node.targets:
            if isinstance(t, ast.Subscript) and self._is_environ(t.value):
                self._add(node, "write", "del os.environ[...]", self._name_of(t.slice))
        self.generic_visit(node)

    # ── reads, writes, copies ─────────────────────────────────────────────
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_environ(node.value):
            self._add(node, "read", "os.environ[...]", self._name_of(node.slice))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and self._is_environ(right):
                self._add(node, "read", "NAME in os.environ", self._name_of(node.left))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            if k is None and self._is_environ(v):  # {**os.environ}
                self._add(node, "copy", "{**os.environ}", "*")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if self._is_environ(node.iter):
            self._add(node, "copy", "for k in os.environ", "*")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute):
            # os.getenv / os.putenv / os.unsetenv
            if self._is_os(f.value):
                if f.attr == "getenv":
                    self._add(node, "read", "os.getenv", self._arg_name(node, 0))
                elif f.attr in ("putenv", "unsetenv"):
                    self._add(node, "write", f"os.{f.attr}", self._arg_name(node, 0))
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
            elif f.id in READ_WRAPPERS:
                for i in READ_WRAPPERS[f.id]:
                    if i < len(node.args):
                        self._add(node, "read", f.id, self._arg_name(node, i))
        # env=os.environ handed to a child process
        for kw in node.keywords:
            if kw.arg == "env" and self._is_environ(kw.value):
                self._add(node, "copy", "env=os.environ", "*")
        self.generic_visit(node)

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
    docstrings = {id(b[0].value) for b in (getattr(x, "body", None) for x in ast.walk(tree))
                  if isinstance(b, list) and b and isinstance(b[0], ast.Expr)
                  and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str)}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str) and "\n" in n.value
                and id(n) not in docstrings
                and ("environ" in n.value or "getenv" in n.value)
                and any(_CODE_LINE.match(line) for line in n.value.splitlines())):
            for candidate in (n.value, _PLACEHOLDER.sub("_", n.value)):
                try:
                    ast.parse(candidate)
                except SyntaxError:
                    continue
                yield candidate, n.lineno
                break
            else:
                yield None, n.lineno


def scan_source(src: str, rel: str) -> list[Finding]:
    tree = ast.parse(src, filename=rel)
    s = _Scanner(rel)
    s.visit(tree)
    found = list(s.found)
    for code, first_line in _embedded_programs(tree):
        if code is None:  # mentions the env, cannot be scanned: say so
            found.append(Finding(rel, first_line, "unparsed", "embedded program (unparsable)", "*", "violation"))
            continue
        inner = _Scanner(rel)
        inner.visit(ast.parse(code))
        found += [Finding(f.path, first_line + f.line - 1, f.kind, f"{f.form} (embedded program)", f.name, f.verdict)
                  for f in inner.found]
    return sorted(set(found), key=lambda f: (f.path, f.line, f.form, f.name))


def _production_files() -> list[Path]:
    out = []
    for root in SCAN_ROOTS:
        for p in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts or p.name.startswith(("bench_", "benchmark_", "test_")):
                continue
            if p.resolve() == Path(__file__).resolve():  # the gate's own planted snippets
                continue
            out.append(p)
    return out


def scan_repo() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    unparsed: list[str] = []
    for p in _production_files():
        rel = p.relative_to(REPO).as_posix()
        try:
            findings.extend(scan_source(p.read_text(encoding="utf-8"), rel))
        except (SyntaxError, UnicodeDecodeError) as e:
            unparsed.append(f"{rel}: {type(e).__name__}: {e}")
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
    ("T = '''\nimport os\nos.environ['M3_X'] = '1'\nfor q in {queries!r}:\n    pass\n'''",
     "write", "os.environ[...] = (embedded program)"),
    ("T = '''\nimport os\nos.environ['M3_X'] = (\n'''", "unparsed", "embedded program (unparsable)"),
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
    for f in failures:
        print(f, file=sys.stderr)
    print(f"selftest: {len(PLANTED) + 2 - len(failures)}/{len(PLANTED) + 2} checks passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="every planted violation must be caught")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    findings, unparsed = scan_repo()
    violations = [f for f in findings if f.verdict == "violation"]
    by_kind = Counter(f.kind for f in violations)
    copies = Counter(f.form for f in violations if f.kind == "copy")
    if args.json:
        print(json.dumps({"findings": [asdict(f) for f in findings], "unparsed": unparsed,
                          "violations_by_kind": dict(by_kind)}, indent=2))
    else:
        if not args.summary:
            for f in findings:
                print(f"{f.path}:{f.line}: {f.verdict:15} {f.kind:6} {f.form:28} {f.name}")
        files = len({f.path for f in violations})
        print(f"\nREPORT MODE (exit 0): {len(violations)} violations in {files} files; "
              f"by kind {dict(sorted(by_kind.items()))}; whole-env copies by form {dict(sorted(copies.items()))}; "
              f"allowed OS-name reads {sum(f.verdict == 'allowed-os-name' for f in findings)}")
    for u in unparsed:
        print(f"UNPARSED (not scanned, not clean): {u}", file=sys.stderr)
    return 2 if unparsed else 0


if __name__ == "__main__":
    sys.exit(main())
