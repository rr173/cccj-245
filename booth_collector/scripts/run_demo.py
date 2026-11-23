"""Reproducible scenario runner.

Each scenario builds a fresh local mock service from a fixture file, runs
the collector under a fixed call budget and page size, writes the full
report to out/<scenario>.json and prints a compact summary.

Scenarios:
  S1 complete ........ small catalog, splits reach exhaustive leaves
  S2 fill-truncated .. truncated leaves are completed by rows already seen
                       in overlapping probes (old rows fill proven counts)
  S3 unsplittable .... a facet-free region stays truncated: exact missing
                       count per leaf, no invented rows
  S4 budget .......... budget runs out mid-tree: conservative bounds only
  S5 contradiction ... injected service faults, each detected and classified
  S6 retry-cache ..... transient retry costs budget; confirmed identical
                       replays cost nothing
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boothlib.client import Client  # noqa: E402
from boothlib.explorer import Explorer  # noqa: E402
from boothlib.model import Filter, load_catalog  # noqa: E402
from boothlib.service import MockService  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")
OUT = os.path.join(ROOT, "out")
FACETS = ["food", "tech", "art"]


def run_scenario(name: str, catalog: str, *, k: int, budget: int,
                 root: Filter | None = None, facets=None,
                 flaky: bool = False,
                 wrong_total_on=None, version_drift_on=None,
                 row_mismatch_on=None, duplicate_rows_on=None) -> dict:
    booths, version = load_catalog(os.path.join(FIX, catalog))
    service = MockService(
        booths, version=version, flaky=flaky,
        wrong_total_on=wrong_total_on, version_drift_on=version_drift_on,
        row_mismatch_on=row_mismatch_on, duplicate_rows_on=duplicate_rows_on,
    )
    client = Client(service=service, budget=budget, pinned_version=version)
    explorer = Explorer(client, root or Filter.root(),
                        list(FACETS) if facets is None else facets, k)
    explorer.run()
    # Demonstration: an already confirmed identical request is free.
    # Only possible when the root response was actually confirmed; after a
    # contradiction it is deliberately not cached and must not be replayed.
    root_filt = root or Filter.root()
    if root_filt in client.cache:
        before = client.service.calls_charged
        replay_resp = client.request(root_filt, k)
        assert client.service.calls_charged == before
        assert replay_resp.total == explorer.root.count
    report = explorer.build_report()
    report["scenario"] = name
    report["catalog_file"] = catalog
    report["cache_hits"] = client.cache_hits
    report["service_calls_charged"] = service.calls_charged

    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    leaves = report["partition_leaves"]
    print("=" * 78)
    print(f"[{name}] catalog={catalog} k={k} budget={budget}")
    print(f"  stop={report['stop_reason']:<18} class={report['termination_class']}")
    print(f"  complete={report['complete']}  root_count={report['global']['root_exact_count']} "
          f"held={report['global']['distinct_rows_held']} "
          f"missing={report['global']['missing_bounds']}")
    print(f"  charged_calls={report['charged_calls']} (budget {budget}), "
          f"cache_hits={client.cache_hits}, leaves={len(leaves)}")
    for lf in leaves:
        print(f"    - {json.dumps(lf['filter'], ensure_ascii=False)} "
              f"exact={lf['exact_count']} known={lf['known_distinct_rows']} "
              f"missing={lf['missing_bounds']} state={lf['state']}"
              + (f"  [{lf['note']}]" if lf['note'] else ""))
    if report["contradictions"]:
        for c in report["contradictions"]:
            print(f"  ! CONTRADICTION: {c}")
    print(f"  report -> {path}")
    return report


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # S1: enough budget, small k, everything reconciles to zero missing.
    run_scenario("S1_complete", "catalog_small.json", k=5, budget=30)

    # S2: tiny pages force truncation; overlapping probes have already
    # returned rows that later fill the truncated leaves exactly.
    run_scenario("S2_fill_truncated", "catalog_big.json", k=3, budget=60)

    # S3: root region with NO usable facet (facets offered as empty list):
    # the root truncates and cannot be subdivided at all. Exact missing only.
    run_scenario("S3_unsplittable", "catalog_big.json", k=3, budget=5, facets=[])

    # S4: generous page but a hard, small budget: tree partially explored,
    # only conservative parent/sibling bounds for unprobed leaves.
    run_scenario("S4_budget", "catalog_big.json", k=4, budget=3)

    # S5a: service inflates one total -> in-request count failure.
    # The injection filter is one the DFS actually probes (root first child).
    run_scenario("S5a_wrong_total", "catalog_small.json", k=5, budget=30,
                 wrong_total_on=Filter.of(include=["food"]))
    # S5b: version drift on one response.
    run_scenario("S5b_version_drift", "catalog_small.json", k=5, budget=30,
                 version_drift_on=Filter.root())
    # S5c: a row's content/tags mutate between requests (identity drift),
    # plus a duplicate-ID injection variant in S5d.
    run_scenario("S5c_row_mismatch", "catalog_big.json", k=4, budget=30,
                 row_mismatch_on=Filter.of(include=["food"], exclude=["tech"]))
    run_scenario("S5d_duplicate_ids", "catalog_big.json", k=4, budget=30,
                 duplicate_rows_on=Filter.root())

    # S6: first new call fails transiently (charged), retry succeeds;
    # afterwards identical confirmed requests replay for free.
    run_scenario("S6_retry_cache", "catalog_small.json", k=5, budget=30, flaky=True)


if __name__ == "__main__":
    main()
