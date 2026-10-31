"""Tests for the tenant-source registry (per-tenant-DB topology). Dependency-free."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from common.registry import load_sources, source_for_tenant  # noqa: E402


def test_default_single_shared_source():
    s = load_sources("postgresql://x/primary", "agg_slot", env="")
    assert len(s) == 1 and s[0].tenant_id is None and s[0].slot == "agg_slot"


def test_per_tenant_sources_parsed():
    env = '[{"tenant_id":1,"dsn":"d1","slot":"agg_slot_t1"},{"tenant_id":2,"dsn":"d2","slot":"agg_slot_t2"}]'
    s = load_sources("def", "agg_slot", env=env)
    assert [(e.tenant_id, e.dsn, e.slot) for e in s] == [(1, "d1", "agg_slot_t1"), (2, "d2", "agg_slot_t2")]


def test_slot_defaulted_when_missing():
    s = load_sources("def", "agg_slot", env='[{"tenant_id":7,"dsn":"d7"}]')
    assert s[0].slot == "agg_slot_t7"


def test_source_for_tenant_resolution():
    env = '[{"tenant_id":1,"dsn":"d1"},{"tenant_id":2,"dsn":"d2"}]'
    s = load_sources("def", "agg_slot", env=env)
    assert source_for_tenant(s, 2).dsn == "d2"
    # unknown tenant with no shared source -> first entry (safe fallback)
    assert source_for_tenant(s, 99).dsn == "d1"
    # shared source wins as fallback when present
    s2 = load_sources("def", "agg_slot", env="")
    assert source_for_tenant(s2, 42).tenant_id is None


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
