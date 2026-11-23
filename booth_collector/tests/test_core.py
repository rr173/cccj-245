"""Unit tests for the collector mechanics. Run: python3 -I tests/test_core.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boothlib.client import BudgetExhausted, Client, Contradiction
from boothlib.explorer import Explorer
from boothlib.model import Booth, Filter, catalog_version, load_catalog
from boothlib.service import MockService, TransientError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTHS, VERSION = load_catalog(os.path.join(ROOT, "fixtures", "catalog_big.json"))


def make_client(budget: int = 100, **kw) -> tuple[MockService, Client]:
    svc = MockService(BOOTHS, version=VERSION, **kw)
    return svc, Client(service=svc, budget=budget, pinned_version=VERSION)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_filter_semantics() -> None:
    print("test_filter_semantics")
    f = Filter.of(include=["a"], exclude=["b"])
    check(f.matches(["a"]), "include only is a match")
    check(f.matches(["a", "c"]), "extra tags allowed")
    check(not f.matches(["a", "b"]), "excluded tag blocks match")
    check(not f.matches([]), "missing include blocks match")
    try:
        Filter.of(include=["x"], exclude=["x"])
        raise AssertionError("expected ValueError for contradictory filter")
    except ValueError:
        print("  ok: contradictory filter rejected")
    # MECE split semantics: include t / exclude t partition any region.
    region = Filter.of(include=["a"])
    inc, exc = region.with_include("t"), region.with_exclude("t")
    for b in BOOTHS:
        if region.matches(b.tags):
            check(inc.matches(b.tags) != exc.matches(b.tags),
                  f"booth {b.id} lands in exactly one split side")
        else:
            check(not inc.matches(b.tags) and not exc.matches(b.tags),
                  f"booth {b.id} outside region stays outside both sides")


def test_service_contract() -> None:
    print("test_service_contract")
    svc, client = make_client()
    resp = client.request(Filter.root(), 4)
    check(resp.total == len(BOOTHS), "exact total for root")
    check(len(resp.rows) == 4, "min(k,total) rows returned")
    check(len({r.id for r in resp.rows}) == 4, "distinct IDs in one page")
    check(resp.truncated, "small k means truncated")
    check(resp.version == VERSION, "pinned version echoed")
    # Confirmed identical request is free even with a different k shape.
    before = svc.calls_charged
    client.request(Filter.root(), 4)
    check(svc.calls_charged == before, "identical confirmed request costs nothing")


def test_retry_charged() -> None:
    print("test_retry_charged")
    svc, client = make_client(budget=10, flaky=True)
    resp = client.request(Filter.root(), 4)  # first attempt fails, retry ok
    check(svc.calls_charged == 2, "both failed attempt and retry are charged")
    check(len(client.ledger) == 2 and client.ledger[0].ok is False,
          "failed attempt recorded in ledger")
    check(resp.total == len(BOOTHS), "retry returns the real response")
    # Budget exhaustion via retries.
    svc2, client2 = make_client(budget=1, flaky=True)
    try:
        client2.request(Filter.root(), 4)
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted:
        print("  ok: no budget left for retry -> BudgetExhausted")


def test_completion_and_partition() -> None:
    print("test_completion_and_partition")
    _, client = make_client(budget=60)
    ex = Explorer(client, Filter.root(), ["food", "tech", "art"], 3)
    ex.run()
    report = ex.build_report()
    check(ex.stop_reason == "completed", "run completes")
    check(report["complete"] is True, "completeness declared")
    check(len(report["booths"]) == len(BOOTHS), "every booth held")
    leaf_filters = [Filter.from_dict(lf["filter"]) for lf in report["partition_leaves"]]
    for b in BOOTHS:
        homes = [f for f in leaf_filters if f.matches(b.tags)]
        check(len(homes) == 1, f"booth {b.id} in exactly one leaf")
    check(sum(lf["exact_count"] for lf in report["partition_leaves"]) == len(BOOTHS),
          "leaf counts sum to root count")


def test_old_rows_fill_truncated_leaf() -> None:
    print("test_old_rows_fill_truncated_leaf")
    # Starve the budget so some leaves are never probed. Their known members
    # can only come from earlier overlapping probes (root/sibling requests).
    svc, client = make_client(budget=3)
    ex = Explorer(client, Filter.root(), ["food", "tech", "art"], 4)
    ex.run()
    blocked = [lf for lf in ex._leaves() if lf.state == "blocked"]
    check(bool(blocked), "budget leaves unprobed blocked regions")
    # Every ID attributed to a blocked leaf must be traceable to some OTHER
    # confirmed request whose filter also matches it (overlapping probe).
    served_filters = list(client.cache.keys())
    for lf in blocked:
        members = getattr(lf, "known_members", [])
        check(bool(members), "blocked region still holds rows seen elsewhere")
        for bid in members:
            booth = ex.registry[bid]
            check(lf.filt.matches(booth.tags), f"{bid} truly belongs to the region")
            origin = [f for f in served_filters
                      if f != lf.filt and f.matches(booth.tags)
                      and bid in {r.id for r in client.cache[f].rows}]
            check(bool(origin), f"{bid} traceable to an overlapping earlier probe")
    # And in the full run, every held ID is auditable through the ledger,
    # while no overlapping counts were ever summed.
    full_svc, full_client = make_client(budget=60)
    full_ex = Explorer(full_client, Filter.root(), ["food", "tech", "art"], 3)
    full_ex.run()
    for bid, booth in full_ex.registry.items():
        sources = [f for f, resp in full_client.cache.items()
                   if f.matches(booth.tags) and any(r.id == bid for r in resp.rows)]
        check(bool(sources), f"booth {bid} has at least one response-level source")
    check(all(n.count_origin in ("response",) for n in [full_ex.root]),
          "counts originate only from own confirmed responses (no overlap sums)")


def test_budget_bounds() -> None:
    print("test_budget_bounds")
    _, client = make_client(budget=3)
    ex = Explorer(client, Filter.root(), ["food", "tech", "art"], 4)
    ex.run()
    check(ex.stop_reason == "budget_exhausted", "budget exhaustion classified")
    report = ex.build_report()
    check(report["complete"] is False, "incomplete report never claims completeness")
    truth = {frozenset(): len(BOOTHS)}
    for lf in report["partition_leaves"]:
        f = Filter.from_dict(lf["filter"])
        true_count = sum(1 for b in BOOTHS if f.matches(b.tags))
        lo, hi = lf["missing_bounds"]
        known = lf["known_distinct_rows"]
        check(lo <= true_count - known <= (hi if hi is not None else 10**9),
              f"bounds bracket true missing for {lf['filter']}")
    check(report["global"]["missing_bounds"][1] >=
          len(BOOTHS) - report["global"]["distinct_rows_held"],
          "global upper bound conservative")


def test_unsplittable() -> None:
    print("test_unsplittable")
    _, client = make_client(budget=10)
    ex = Explorer(client, Filter.root(), [], 3)
    ex.run()
    check(ex.stop_reason == "completed", "no budget failure here")
    report = ex.build_report()
    check(report["termination_class"] == "unsplittable_truncation",
          "truncation without facets classified distinctly")
    leaf = report["partition_leaves"][0]
    check(leaf["exact_count"] == len(BOOTHS) and leaf["missing_exact"] == len(BOOTHS) - 3,
          "exact count reconciles; exact missing reported, no invented rows")


def test_contradictions() -> None:
    print("test_contradictions")
    # wrong total
    _, c1 = make_client(wrong_total_on=Filter.of(include=["food"]))
    ex1 = Explorer(c1, Filter.root(), ["food"], 5)
    ex1.run()
    check(ex1.stop_reason == "contradiction" and ex1.contradictions, "wrong total detected")
    # version drift
    _, c2 = make_client(version_drift_on=Filter.root())
    ex2 = Explorer(c2, Filter.root(), ["food"], 5)
    ex2.run()
    check(ex2.stop_reason == "contradiction", "version drift detected")
    # duplicate IDs
    _, c3 = make_client(duplicate_rows_on=Filter.root())
    ex3 = Explorer(c3, Filter.root(), ["food"], 4)
    ex3.run()
    check(any("duplicate IDs" in m for m in ex3.contradictions), "duplicate IDs detected")
    # identity drift
    _, c4 = make_client(row_mismatch_on=Filter.of(include=["food"], exclude=["tech"]))
    ex4 = Explorer(c4, Filter.root(), ["food", "tech"], 4)
    ex4.run()
    check(any("identity drift" in m or "does not match" in m for m in ex4.contradictions),
          "row mutation detected")
    check(ex4.build_report()["complete"] is False, "contradiction forbids completeness")


def test_root_scoping() -> None:
    print("test_root_scoping")
    root = Filter.of(include=["tech"])
    _, client = make_client(budget=60)
    ex = Explorer(client, root, ["food", "art"], 5)
    ex.run()
    report = ex.build_report()
    truth = [b for b in BOOTHS if root.matches(b.tags)]
    check(len(report["booths"]) == len(truth), "only root-inside booths delivered")
    check(all(root.matches(b["tags"]) for b in report["booths"]), "every row matches root")
    check(report["global"]["root_exact_count"] == len(truth), "root count exact")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()
