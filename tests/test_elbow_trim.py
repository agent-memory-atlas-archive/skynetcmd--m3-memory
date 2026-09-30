import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

# The trim_legacy fixture imports memory_core, which reaches transformers, whose
# lazy __getattr__ can pull an image model that imports torchvision. When that
# transitive chain is broken the fixture ERRORS at setup rather than failing a
# test — which reads as a red suite even though the elbow logic under test is
# pure scoring and needs none of it. Skip honestly instead.
pytestmark = pytest.mark.requires_memory_core


@pytest.fixture
def trim_legacy(monkeypatch):
    """Binds the documented small-pool overrides on the config module so the trim
    fires on the 5-element pools these tests use. Defaults (MIN_INPUT=20)
    skip trimming for pools under 20.

    Using monkeypatch.setattr directly on the config module object avoids
    reload-based state leakage or sys.modules mismatch errors across tests.
    """
    import sys

    # importorskip, not a bare import. `pytestmark = requires_memory_core` above
    # runs its probe at COLLECTION time, and the breakage this guards against can
    # arrive DURING the run: measured 2026-09-29 on Linux, this file passes when
    # run alone and ERRORS at setup with `ModuleNotFoundError: No module named
    # 'torch'` in a full run, because an earlier test leaves the `memory.*`
    # namespace in a state where re-importing `memory.search` re-enters the heavy
    # transformers chain (see conftest's `_restore_memory_modules`). A
    # collection-time probe cannot see run-time pollution, so the skip decision
    # has to be made HERE, where the import actually happens. The elbow logic
    # under test is pure scoring and needs none of it, so a skip is honest and a
    # setup ERROR is not.
    memory_core = pytest.importorskip(
        "memory_core", reason="memory_core not importable at fixture time")
    search = pytest.importorskip(
        "memory.search", reason="memory.search not importable at fixture time "
                                "(a transitive ML dep such as torch is absent)")

    # Directly patch memory.search's config module reference for maximum resilience
    monkeypatch.setattr(search.config, "ELBOW_MIN_INPUT", 3)
    monkeypatch.setattr(search.config, "ELBOW_MIN_RETURN", 1)
    monkeypatch.setattr(search.config, "ELBOW_ABS_THRESHOLD", 0.0)

    # hasattr() on an arbitrary module can EXECUTE IMPORTS. `transformers`
    # defines a lazy `__getattr__` that resolves unknown attributes by importing
    # submodules, and one of them (models/aria/image_processing_aria.py) does
    # `import torch`. hasattr swallows AttributeError but NOT
    # ModuleNotFoundError, so the probe itself raised and this fixture ERRORED at
    # setup. Measured 2026-09-29 on Linux after torch left requirements.txt: the
    # file passes ALONE and errors in a FULL run, because `transformers` is only
    # in sys.modules once an earlier test has imported it — which also means a
    # collection-time capability probe cannot see it coming.
    #
    # Scanning every loaded module is the fixture's own design (config constants
    # are re-exported in several places); making the PROBE safe keeps that intent
    # without letting a third-party lazy import decide whether these pure-scoring
    # tests can run.
    for name, module in list(sys.modules.items()):
        try:
            exposes_elbow = hasattr(module, "ELBOW_MIN_INPUT")
        except Exception:  # noqa: BLE001 — a lazy __getattr__ may import anything
            continue
        if exposes_elbow:
            monkeypatch.setattr(module, "ELBOW_MIN_INPUT", 3)
            monkeypatch.setattr(module, "ELBOW_MIN_RETURN", 1)
            monkeypatch.setattr(module, "ELBOW_ABS_THRESHOLD", 0.0)
    yield memory_core._trim_by_elbow


def _ranked(scores):
    return [(s, {"id": f"x{i}", "content": f"c{i}"}) for i, s in enumerate(scores)]


def test_sensitivity_default_preserves_behavior(trim_legacy):
    # Clean cliff: diffs [0.02, 0.02, 0.47, 0.01]. avg ≈ 0.13.
    # sensitivity=1.5 threshold ≈ 0.195. First diff > 0.195 is the 0.47 at i=2 → trim[:3].
    ranked = _ranked([1.00, 0.98, 0.96, 0.49, 0.48])
    assert len(trim_legacy(ranked)) == 3
    assert len(trim_legacy(ranked, sensitivity=1.5)) == 3


def test_looser_sensitivity_keeps_more_or_trims_earlier(trim_legacy):
    # Gentler slope: diffs [0.05, 0.10, 0.10, 0.15], avg=0.10.
    # sensitivity=1.5 → threshold 0.15, no diff exceeds → keep all 5.
    # sensitivity=1.0 → threshold 0.10, 0.15 at i=3 exceeds → trim[:4].
    # sensitivity=0.9 → threshold 0.09, 0.10 at i=1 exceeds → trim[:2].
    ranked = _ranked([1.00, 0.95, 0.85, 0.75, 0.60])
    assert len(trim_legacy(ranked, sensitivity=1.5)) == 5
    assert len(trim_legacy(ranked, sensitivity=1.0)) == 4
    assert len(trim_legacy(ranked, sensitivity=0.9)) == 2


def test_too_few_to_trim(trim_legacy):
    assert trim_legacy(_ranked([0.9, 0.8])) == _ranked([0.9, 0.8])
    assert trim_legacy([]) == []


def test_no_elbow_returns_all(trim_legacy):
    # Uniform diffs → no diff > avg*1.5.
    ranked = _ranked([1.0, 0.9, 0.8, 0.7, 0.6])
    assert len(trim_legacy(ranked)) == 5


def test_default_min_input_skips_small_pools():
    """Confirms the new default behavior: pools smaller than ELBOW_MIN_INPUT
    (20 by default) are returned unchanged, regardless of cliff shape.

    This protects against the at-scale "1-result collapse" by requiring
    enough samples to estimate the avg-diff threshold reliably.
    """
    from memory_core import _trim_by_elbow

    ranked = _ranked([1.00, 0.98, 0.96, 0.49, 0.48])
    # 5 elements < default MIN_INPUT=20 → no trim
    assert len(_trim_by_elbow(ranked)) == 5
