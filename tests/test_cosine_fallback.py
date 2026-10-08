"""embedding_utils.cosine's pure-Python fallback agrees with the numpy path.

The fallback runs only without numpy (a hard dependency, so a broken install),
and it was copied into files_memory/dedup.py. It now uses math.sumprod and is
the one fallback; dedup calls it. A dimension mismatch raises, as np.dot does,
where zip() used to truncate silently and score the overlap as if it were the
whole vector.
"""
from __future__ import annotations

import random

import pytest


@pytest.fixture
def no_numpy(monkeypatch):
    import embedding_utils

    monkeypatch.setattr(embedding_utils, "HAS_NUMPY", False)
    return embedding_utils


def _vecs(seed, n=64):
    r = random.Random(seed)
    return [r.uniform(-1, 1) for _ in range(n)], [r.uniform(-1, 1) for _ in range(n)]


@pytest.mark.parametrize("seed", range(20))
def test_fallback_matches_numpy(no_numpy, seed):
    import embedding_utils

    a, b = _vecs(seed)
    fallback = no_numpy.cosine(a, b)
    no_numpy.HAS_NUMPY = True
    try:
        reference = embedding_utils.cosine(a, b)
    finally:
        no_numpy.HAS_NUMPY = False
    assert fallback == pytest.approx(reference, abs=1e-6)


def test_zero_vector_scores_zero(no_numpy):
    assert no_numpy.cosine([0.0, 0.0], [1.0, 2.0]) == 0.0


@pytest.mark.parametrize("numpy_on", [False, True])
def test_dimension_mismatch_raises_on_both_paths(monkeypatch, numpy_on):
    import embedding_utils

    monkeypatch.setattr(embedding_utils, "HAS_NUMPY", numpy_on)
    with pytest.raises(ValueError):
        embedding_utils.cosine([1.0, 2.0, 3.0], [1.0, 2.0])


def test_dedup_has_no_cosine_copy():
    """dedup's no-numpy path delegates to the one fallback."""
    import inspect

    from files_memory import dedup

    src = inspect.getsource(dedup)
    assert "sum(x * y for x, y in zip(" not in src, "dedup carries its own cosine copy"
