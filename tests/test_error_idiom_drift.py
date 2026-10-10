"""Guard: a DIAGNOSTIC error states what was observed and what to inspect.

DESIGN_PHILOSOPHIES §3 — "State what you OBSERVED; mark what you INFERRED." An
error message is a claim about the world and must carry its evidence level:

  * ``observed:`` — measured values only (counts, resolved paths, actual state)
  * ``cause:``    — ONLY when the mechanism is verified in code
  * ``possible:`` — candidate explanations when it is not
  * ``inspect:``  — the knob, env var or file the operator should go look at

The cost of not doing this is concrete and shipped: "Check embedder
availability" stated a guess as a diagnosis and sent an operator to inspect a
perfectly healthy server. The corrected message reads ``observed: 5 failed
batch(es), 0 content-shaped`` / ``possible: server unreachable, overloaded, or a
network path issue`` / ``inspect: M3_EMBED_FALLBACK_URL``.

WHAT THIS GUARD DOES **NOT** COVER — and deliberately so.

Only DIAGNOSTIC errors are in scope: the ones where an operator has to go look
at something (a service is unreachable, a path resolved wrong, a backend
failed). A CONTRACT VIOLATION is not in scope and must not be "fixed" to pass
this test::

    raise ValueError("conversation_id is required")      # already correct
    raise ValueError(f"role must be one of {VALID_ROLES}")  # already correct

For those, the observed value IS the argument and the knob IS the parameter
name. Adding ``observed:``/``inspect:`` ceremony to them makes every message
longer and trains readers to skim — which §3 calls out in its own terms: "a
false alarm is a §3 violation, not a safe default." A guard that pushes authors
toward noise is worse than no guard.

WHY A RATCHET AND NOT A HARD ZERO.

Measured 2026-09-16: 30 diagnostic sites, 2 labelled — 28 unlabelled, brought
down to 17 the same day. A hard zero fails on day
one, and a guard nobody can make pass gets deleted — which is strictly worse
than one that only ever tightens. This pins the current count of UNLABELLED
diagnostic sites so the debt can shrink but never grow.

To LOWER the budget after labelling sites: run this file, take the number it
reports, and lower ``_BUDGET``. That is the intended workflow.

To ADD a legitimate unlabelled diagnostic: that is almost always wrong. Label
it instead. If it genuinely is a contract violation that this heuristic
mis-flags, add the file to ``_EXEMPT`` with a one-line reason — a visible,
reviewable edit rather than a silent new copy.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Current floor. Lower this as sites are labelled; never raise it.
# 2026-09-16: 28 -> 17 after labelling the enrich, crypto/auth and
# server/backend subsystems.
# 2026-10-09: 17 -> 13, crypto_provider's copied FIPS-boundary check became one
# labelled helper.
# 2026-10-10: re-measured, 12 -> 27. NOT a loosening: the scanner now reads each
# message from the AST. The raw-text regex (400-char window) missed 15
# multi-line unlabelled messages, and the ruff format sweep moved its count
# without changing a message. The AST count is identical before and after
# formatting (measured per file).
# 2026-10-10: 27 -> 0, every flagged message labelled (test doubles exempt).
_BUDGET = 0

_SKIP_DIRS = {
    "build",  # packaging copy of the tree
    "tests",  # fixtures raise deliberately malformed errors
    "to_be_deleted",
    ".git",
    "__pycache__",
    "node_modules",
    ".scratch",
}

# Files whose flagged sites are contract violations this heuristic mis-reads.
# One line of reason each, so the exemption is reviewable.
_EXEMPT: dict[str, str] = {
    # _validate_role() validates an argument that becomes a filename; the
    # message already names the observed value and the allowed set.
    "bin/m3_halt.py": "argument validation, not a diagnostic",
    # A test double raising what a missing library would; not an operator message.
    "bin/test_fips_integrity.py": "a test double (Mock CDLL), not a diagnostic",
}

# A raise/exit whose MESSAGE mentions a knob, an endpoint, or a failure verb,
# i.e. something the operator would have to go and look at.
_DIAGNOSTIC = re.compile(
    r"(M3_[A-Z_]+|http://|https://|unreachable|failed to|could not"
    r"|timeout|refused|not found)",
    re.IGNORECASE,
)
_LABEL = re.compile(r"observed:|possible:|cause:|inspect:")
_RAISED = {"ValueError", "RuntimeError", "OSError"}


def _literal_text(node: ast.AST) -> str | None:
    """The literal text of a message: constants, f-string literal parts (each
    placeholder as {}), and + concatenations. None when it is not a literal.

    Read from the AST, so a message the formatter splits across lines reads the
    same as one on a single line. The first version scanned raw text with a
    400-character window, and the one-commit ruff format sweep moved its count
    from 12 to 14 without changing a single message (2026-10-10).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_text(node.left), _literal_text(node.right)
        return None if left is None or right is None else left + right
    return None


def _diagnostic_messages(tree: ast.AST):
    """(lineno, message) for each raise of a diagnostic exception type and each
    sys.exit(...) whose first argument is a literal message."""
    for node in ast.walk(tree):
        call = None
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            fn = node.exc.func
            if isinstance(fn, ast.Name) and fn.id in _RAISED:
                call = node.exc
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "exit"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "sys"
            ):
                call = node
        if call is not None and call.args:
            text = _literal_text(call.args[0])
            if text is not None:
                yield call.lineno, text


def _classify(text: str) -> str | None:
    """'labelled' / 'unlabelled' for a diagnostic message, None for others."""
    if not _DIAGNOSTIC.search(text):
        return None
    return "labelled" if _LABEL.search(text) else "unlabelled"


def _unlabelled_sites(root: pathlib.Path = _ROOT) -> list[str]:
    found: list[str] = []
    for path in (root / "bin").rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel in _EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for lineno, text in _diagnostic_messages(tree):
            if _classify(text) == "unlabelled":
                found.append(f"{rel}:{lineno}")
    return sorted(found)


class ErrorIdiomDriftTest(unittest.TestCase):
    def test_unlabelled_diagnostics_do_not_grow(self):
        sites = _unlabelled_sites()
        self.assertLessEqual(
            len(sites),
            _BUDGET,
            "Unlabelled diagnostic error sites rose from "
            f"{_BUDGET} to {len(sites)}. A diagnostic error must say what was "
            "observed and what to inspect (DESIGN_PHILOSOPHIES §3). New or "
            "changed sites:\n  " + "\n  ".join(sites),
        )

    def test_budget_is_not_stale(self):
        """If the count has dropped, the budget must come down with it.

        Without this, the guard silently stops ratcheting: someone labels ten
        sites, the budget stays high, and ten new unlabelled ones can appear
        without failing anything.
        """
        sites = _unlabelled_sites()
        self.assertGreaterEqual(
            len(sites),
            _BUDGET,
            f"Unlabelled diagnostics dropped to {len(sites)} but _BUDGET is "
            f"still {_BUDGET}. Lower _BUDGET to {len(sites)} to lock in the "
            "improvement.",
        )

    def test_the_guard_actually_matches(self):
        """Self-check: the scanner must classify a real message of each kind,
        and a message split across lines exactly as the same message on one.

        A drift guard blind to the shape it polices passes forever while the
        thing it guards rots.
        """

        def kinds(src: str) -> list[str | None]:
            return [_classify(t) for _l, t in _diagnostic_messages(ast.parse(src))]

        labelled = 'raise RuntimeError(f"embed failed. observed: {n} failed batch(es). inspect: M3_EMBED_FALLBACK_URL")'
        self.assertEqual(kinds(labelled), ["labelled"], "misses a labelled diagnostic")
        unlabelled = 'import sys\nsys.exit("could not reach the server at http://127.0.0.1:8082")'
        self.assertEqual(kinds(unlabelled), ["unlabelled"], "misses an unlabelled exit")
        split = 'raise RuntimeError(\n    "could not reach the server. "\n    "observed: refused"\n)'
        self.assertEqual(kinds(split), ["labelled"], "a split message reads differently")
        contract = 'raise ValueError("conversation_id is required")'
        self.assertEqual(
            kinds(contract),
            [None],
            "contract violations must stay OUT of scope — see the module "
            "docstring; flagging them pushes authors toward noise",
        )


if __name__ == "__main__":
    unittest.main()
