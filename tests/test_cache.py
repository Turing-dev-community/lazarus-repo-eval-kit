import pytest

from eval_kit.cache import EvalCache


# ── enabled=False ─────────────────────────────────────────────────────────────


def test_disabled_get_returns_none(tmp_path):
    cache = EvalCache(cache_dir=tmp_path, enabled=False)
    assert cache.get("any_key") is None


def test_disabled_set_is_noop(tmp_path):
    cache = EvalCache(cache_dir=tmp_path, enabled=False)
    cache.set("any_key", {"x": 1})
    # No disk cache was created
    assert cache._cache is None


def test_disabled_get_or_compute_calls_fn(tmp_path):
    cache = EvalCache(cache_dir=tmp_path, enabled=False)
    called = []
    result = cache.get_or_compute("k", lambda: called.append(1) or {"v": 1})
    assert result == {"v": 1}
    assert called == [1]


# ── read_enabled=False (--no-cache) ───────────────────────────────────────────


def test_no_cache_reads_always_miss(tmp_path):
    # Prime the cache with read_enabled=True
    warm = EvalCache(cache_dir=tmp_path, enabled=True)
    warm.set("k", {"data": 42})

    no_read = EvalCache(cache_dir=tmp_path, enabled=True, read_enabled=False)
    assert no_read.get("k") is None


def test_no_cache_writes_are_visible_to_next_run(tmp_path):
    no_read = EvalCache(cache_dir=tmp_path, enabled=True, read_enabled=False)
    no_read.set("k", {"data": 99})

    reader = EvalCache(cache_dir=tmp_path, enabled=True, read_enabled=True)
    assert reader.get("k") == {"data": 99}


# ── JSON round-trip ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}),
        ([1, "two", 3.0], [1, "two", 3.0]),
        ("plain string", "plain string"),
        (42, 42),
    ],
)
def test_round_trip(tmp_path, value, expected):
    cache = EvalCache(cache_dir=tmp_path)
    cache.set("k", value)
    assert cache.get("k") == expected


# ── get_or_compute ────────────────────────────────────────────────────────────


def test_get_or_compute_calls_fn_on_miss(tmp_path):
    cache = EvalCache(cache_dir=tmp_path)
    calls = []
    result = cache.get_or_compute("k", lambda: calls.append(1) or {"v": 1})
    assert result == {"v": 1}
    assert len(calls) == 1


def test_get_or_compute_skips_fn_on_hit(tmp_path):
    cache = EvalCache(cache_dir=tmp_path)
    cache.set("k", {"v": 1})
    calls = []
    result = cache.get_or_compute("k", lambda: calls.append(1) or {"v": 2})
    assert result == {"v": 1}
    assert calls == []


# ── version isolation ─────────────────────────────────────────────────────────


def test_different_versions_dont_share_entries(tmp_path):
    v1 = EvalCache(cache_dir=tmp_path, version="1.0.0")
    v2 = EvalCache(cache_dir=tmp_path, version="2.0.0")

    v1.set("k", {"score": 10})
    assert v2.get("k") is None


def test_same_version_shares_entries(tmp_path):
    a = EvalCache(cache_dir=tmp_path, version="1.0.0")
    b = EvalCache(cache_dir=tmp_path, version="1.0.0")

    a.set("k", {"score": 10})
    assert b.get("k") == {"score": 10}


# ── key builders ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,args,expected",
    [
        ("repo_key", ("github", "acme", "widgets"), "repo:github:acme/widgets"),
        (
            "pr_patch_key",
            ("github", "acme", "widgets", 7),
            "pr_patch:github:acme/widgets:7",
        ),
        (
            "f2p_key",
            ("github", "acme", "widgets", 7, "aaa", "bbb"),
            "f2p:github:acme/widgets:7:aaa:bbb",
        ),
        (
            "rubric_key",
            ("github", "acme", "widgets", 7, "aaa", "bbb"),
            "rubric:github:acme/widgets:7:aaa:bbb",
        ),
        (
            "taxonomy_key",
            ("github", "acme", "widgets", 7, "aaa", "bbb"),
            "taxonomy:github:acme/widgets:7:aaa:bbb",
        ),
        (
            "fairness_key",
            ("github", "acme", "widgets", 7, "aaa", "bbb"),
            "fairness:github:acme/widgets:7:aaa:bbb",
        ),
        (
            "quality_checks_key",
            ("github", "acme", "widgets", False, "abc123"),
            "quality:github:acme/widgets:llm:abc123",
        ),
        (
            "quality_checks_key",
            ("github", "acme", "widgets", True, ""),
            "quality:github:acme/widgets:nollm:",
        ),
    ],
)
def test_key_builders(method, args, expected):
    assert getattr(EvalCache, method)(*args) == expected
