#!/usr/bin/env python3
"""Generate download-count badge data (PyPI + GitHub) for the README.

Writes two shields.io *endpoint* JSON files that the README badges point at:

  * docs/badges/pypi-downloads.json / pypi-monthly.json — PyPI downloads, total
        and last 30 days, WITH mirrors (pypistats "overall"), labelled "PyPI
        downloads" as pepy.tech and most projects' badges are. The totals WITHOUT
        mirrors (closer to real installs) are kept in counters.json beside them.
  * docs/badges/github-clones.json   — TOTAL unique GitHub clones
        (the repo traffic API's daily `uniques`).

Both sources keep only a rolling window (pypistats ~180 days, GitHub 14 days), so
the totals are RUNNING totals. The public repo keeps only the aggregate:
docs/badges/counters.json holds each source's total and the last complete day it
counted, and each run adds the complete days after that (a weekly run stays inside
both windows; a gap longer than a window is reported, since those days are gone).
The per-day history is private (user decision 2026-10-13): `--history-dir` (for
example ~/.m3-private/badges) also merges every day into pypi-history.json and
clone-history.json there. CI never passes it, so no history reaches this repo.

These are ESTIMATES by design — download/clone counts include automation and
can't be deduplicated to people; the numbers are labelled accordingly and the
mirror-excluded / unique variants are chosen to be the least-noisy signal.

The README references the JSON via a shields endpoint URL, e.g.:
    https://img.shields.io/endpoint?url=<raw json url>&style=flat-square
so shields renders the current committed number with no third-party number source.

Run in CI on a schedule; commit the result back. Locally, keeping the history:
    GITHUB_TOKEN=$(gh auth token) python bin/gen_download_badges.py \
        --repo skynetcmd/m3-memory --package m3-memory --history-dir ~/.m3-private/badges

Exit codes: 0 = badges written (or unchanged); 2 = hard error (network/auth).
Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

GH_API = "https://api.github.com"
PYPISTATS_API = "https://pypistats.org/api/packages"

_UA = "m3-download-badge-generator"


def _fmt(n: int) -> str:
    """Human-compact count: 9909 -> '9.9k', 32273 -> '32k', 1200000 -> '1.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        k = n / 1000.0
        return f"{k:.1f}k".replace(".0k", "k") if k < 100 else f"{round(k)}k"
    m = n / 1_000_000.0
    return f"{m:.1f}M".replace(".0M", "M")


def _get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url)  # nosec B310 — fixed https hosts below
    req.add_header("User-Agent", _UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return cast("dict", json.load(resp))


def _shields_endpoint(label: str, message: str, color: str) -> dict:
    """A shields.io 'endpoint' schema object (schemaVersion 1)."""
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
    }


def _write_badge_svg(path: Path, label: str, message: str, color: str, logo: str | None) -> bool:
    """Fetch the RENDERED badge SVG from shields.io and commit it under
    docs/badges/. We self-host the SVG (not a live shields `endpoint?url=` link)
    because GitHub's Camo image proxy intermittently fails to fetch shields,
    showing a broken image; a committed SVG served from raw.githubusercontent is
    Camo-reliable. Same pattern as the other committed badges. Returns True if the
    file changed. On any fetch failure, leaves the existing SVG untouched (never
    overwrite a good badge with an error page)."""
    import urllib.parse

    def _enc(s: str) -> str:
        # shields path-encoding: '-' -> '--', ' ' -> '_', then URL-quote.
        return urllib.parse.quote(s.replace("-", "--").replace(" ", "_"), safe="")

    url = f"https://img.shields.io/badge/{_enc(label)}-{_enc(message)}-{color}?style=flat-square"
    if logo:
        url += f"&logo={urllib.parse.quote(logo)}&logoColor=white"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            svg = resp.read()
        if b"<svg" not in svg[:200]:
            print(f"[svg] {path.name}: unexpected response, not overwriting", file=sys.stderr)
            return False
        old = path.read_bytes() if os.path.exists(path) else b""
        if old != svg:
            path.write_bytes(svg)
            return True
        return False
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[svg] {path.name}: fetch failed ({type(e).__name__}) — keeping existing", file=sys.stderr)
        return False


# ── PyPI ─────────────────────────────────────────────────────────────────────


def pypi_days(package: str) -> dict[str, list[tuple[str, int]]]:
    """{category: [(YYYY-MM-DD, downloads), ...]} over pypistats' ~180-day window, for
    "with_mirrors" (every download: what pepy.tech and most projects' badges show) and
    "without_mirrors" (mirror bots such as bandersnatch excluded: closer to installs)."""
    data = _get_json(f"{PYPISTATS_API}/{package}/overall")
    out: dict[str, list[tuple[str, int]]] = {"with_mirrors": [], "without_mirrors": []}
    for r in data.get("data", []):
        cat, day = r.get("category"), str(r.get("date", ""))[:10]
        if cat in out and day:
            out[cat].append((day, int(r.get("downloads", 0))))
    return out


def last_30_days(days: list[tuple[str, int]], today: str) -> int:
    """Downloads over the 30 complete days before ``today`` (today's count is partial)."""
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=30)).isoformat()
    return sum(n for d, n in days if start <= d < today)


# ── GitHub clones (running total across the 14-day window) ───────────────────


def github_clone_days(repo: str, token: str) -> list[tuple[str, int]]:
    """Return [(YYYY-MM-DD, unique_clones), ...] from the traffic API (14-day)."""
    url = f"{GH_API}/repos/{repo}/traffic/clones"
    data = _get_json(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    out: list[tuple[str, int]] = []
    for d in data.get("clones", []):
        ts = str(d.get("timestamp", ""))[:10]  # date only
        if ts:
            out.append((ts, int(d.get("uniques", 0))))
    return out


def accumulate_daily_total(history_path: Path, fresh_days: list[tuple[str, int]]) -> int:
    """Merge a source's rolling window into a persisted per-day history (dedup by
    date, newest value wins) and return the running total.

    GitHub exposes 14 days of clones and pypistats ~180 days of downloads, so a
    scheduled run stitches successive windows into an all-time total. A day already
    in history is updated to the latest reported value (the same day can appear in
    two overlapping windows, and the newest is the authoritative count).
    """
    history: dict[str, int] = {}
    if os.path.exists(history_path):
        try:
            history = {str(k): int(v) for k, v in json.loads(history_path.read_text("utf-8")).items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            history = {}
    for day, uniques in fresh_days:
        # Newest window's value for a day wins (it's the authoritative latest).
        history[day] = uniques
    # Persist sorted for a stable, reviewable diff.
    ordered = dict(sorted(history.items()))
    history_path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8", newline="\n")  # LF on every OS
    return sum(ordered.values())


# ── the public running counter (aggregate only) ─────────────────────────────


def _today() -> str:
    """Today's UTC date: its count is partial, so it is never counted."""
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def advance_counter(
    counter: dict | None, days: list[tuple[str, int]], today: str, window_days: int, label: str
) -> tuple[dict, list[str]]:
    """Add the COMPLETE days after ``counter["through"]`` to its total.

    Returns (new counter, warnings). Only days before ``today`` count (today's
    figure is partial and would be double-counted next run). A counter with no
    ``through`` cannot be advanced: the caller seeds it (from the private history)
    first. A gap is when the window no longer reaches the day after ``through``:
    those days are gone, and the warning says how many.
    """
    if not counter or not counter.get("through"):
        raise ValueError(f"{label}: no counter to advance; seed it with --history-dir")
    through, total = str(counter["through"]), int(counter.get("total", 0))
    complete = sorted((d, n) for d, n in days if d < today)
    warnings: list[str] = []
    if complete:
        first = datetime.date.fromisoformat(complete[0][0])
        expected = datetime.date.fromisoformat(through) + datetime.timedelta(days=1)
        if first > expected:
            missing = (first - expected).days
            warnings.append(
                f"observed: {label} window starts {complete[0][0]}, but the counter stops at {through}; "
                f"{missing} day(s) are no longer reported (window ~{window_days} days). cause: no run "
                f"for longer than the window. fix: run at least every {window_days} days; the total "
                f"undercounts by those days."
            )
    new = [(d, n) for d, n in complete if d > through]
    if new:
        total += sum(n for _, n in new)
        through = new[-1][0]
    return {"total": total, "through": through}, warnings


def seed_counter(history_path: Path, today: str) -> dict | None:
    """A counter from a private per-day history: complete days only."""
    if not os.path.isfile(history_path):  # os.path: Path.is_file raises under an untraversable parent (3.12/3.13)
        return None
    rows = {str(k): int(v) for k, v in json.loads(history_path.read_text("utf-8")).items() if str(k) < today}
    if not rows:
        return None
    return {"total": sum(rows.values()), "through": max(rows)}


# ── write + main ─────────────────────────────────────────────────────────────


def _write_json(path: Path, obj: dict) -> bool:
    """Write pretty JSON; return True if the file content changed."""
    new = json.dumps(obj, indent=2) + "\n"
    old = path.read_text("utf-8") if os.path.exists(path) else None
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")  # LF on every OS
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate PyPI + GitHub download badges.")
    ap.add_argument("--repo", default="skynetcmd/m3-memory", help="owner/name")
    ap.add_argument("--package", default="m3-memory", help="PyPI package name")
    ap.add_argument("--badges-dir", default="docs/badges", help="output directory")
    ap.add_argument(
        "--history-dir",
        default=None,
        help="private directory for the per-day history (e.g. ~/.m3-private/badges); never the repo",
    )
    args = ap.parse_args()

    badges = Path(args.badges_dir)
    history = Path(args.history_dir).expanduser() if args.history_dir else None
    counters_path = badges / "counters.json"
    counters: dict = json.loads(counters_path.read_text("utf-8")) if os.path.isfile(counters_path) else {}
    today = _today()
    changed = False

    def _counted(key: str, history_name: str, days: list[tuple[str, int]], window: int, label: str) -> int:
        if history is not None:
            history.mkdir(parents=True, exist_ok=True)
            accumulate_daily_total(history / history_name, days)
            if key not in counters:
                seeded = seed_counter(history / history_name, today)
                if seeded:
                    counters[key] = seeded
        counters[key], warnings = advance_counter(counters.get(key), days, today, window, label)
        for w in warnings:
            print(f"[{key}] WARNING: {w}", file=sys.stderr)
        return int(counters[key]["total"])

    # PyPI — never fatal to the whole run if one source is down; report + continue.
    # The badges show downloads WITH mirrors, labelled "PyPI downloads", as pepy.tech
    # and most projects' badges do (user decision 2026-10-13), so m3's figure compares
    # like for like. The figures WITHOUT mirrors (closer to real installs) are kept
    # beside them in counters.json, which the README's note points to.
    try:
        series = pypi_days(args.package)
        total = _counted("pypi_all", "pypi-all-history.json", series["with_mirrors"], 180, "PyPI (with mirrors)")
        _counted("pypi", "pypi-history.json", series["without_mirrors"], 180, "PyPI (without mirrors)")
        month = last_30_days(series["with_mirrors"], today)
        counters["pypi_last_30_days"] = {
            "with_mirrors": month,
            "without_mirrors": last_30_days(series["without_mirrors"], today),
        }
        for name, message in (("pypi-downloads", _fmt(total)), ("pypi-monthly", f"{_fmt(month)}/month")):
            if _write_json(badges / f"{name}.json", _shields_endpoint("PyPI downloads", message, "blue")):
                changed = True
            # Self-hosted SVG (Camo-reliable) — the README points at this, not the JSON.
            if _write_badge_svg(badges / f"{name}.svg", "PyPI downloads", message, "blue", logo="pypi"):
                changed = True
        print(f"[pypi] with mirrors: {total:,} total, {month:,} last 30 days")
        print(
            f"[pypi] without mirrors: {counters['pypi']['total']:,} total, "
            f"{counters['pypi_last_30_days']['without_mirrors']:,} last 30 days"
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
        print(f"[pypi] WARNING: could not fetch downloads ({type(e).__name__}: {e})", file=sys.stderr)

    # GitHub clones — requires a token that can read the private traffic API.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("M3_STARGAZER") or ""
    if not token:
        print(
            "[github] no GITHUB_TOKEN/M3_STARGAZER — skipping clone badge "
            "(traffic API needs an admin/collaborator token).",
            file=sys.stderr,
        )
    else:
        try:
            days = github_clone_days(args.repo, token)
            total = _counted("clones", "clone-history.json", days, 14, "GitHub clones")
            if _write_json(
                badges / "github-clones.json",
                _shields_endpoint("GitHub clones", f"{_fmt(total)}", "24292e"),
            ):
                changed = True
            # Self-hosted SVG with the GitHub icon (Camo-reliable).
            if _write_badge_svg(badges / "github-clones.svg", "GitHub clones", _fmt(total), "24292e", logo="github"):
                changed = True
            print(f"[github] running unique-clone total: {total:,} -> {_fmt(total)}")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            print(f"[github] WARNING: could not fetch clone traffic ({type(e).__name__}: {e})", file=sys.stderr)

    if counters and _write_json(counters_path, dict(sorted(counters.items()))):
        changed = True
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(2) from None
