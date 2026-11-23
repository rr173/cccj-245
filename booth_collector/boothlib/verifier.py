"""Independent verifier.

Given a collector report and the same catalog fixture, this module builds
its OWN service and client (no reuse of the collector's state), replays the
recorded requests, and checks every claim independently:

  V1  version pin: report version == fixture version, all calls agree;
  V2  ledger/budget accounting: charged calls <= budget, retries charged,
      confirmed identical requests replayed for free;
  V3  per-request contract: distinct IDs, len==min(k,total), rows<=total,
      every row matches its request filter;
  V4  identity: every booth id always carries identical tags/content;
  V5  MECE partition: the leaf filters are mutually exclusive and jointly
      exhaustive over the root filter, proven against the whole catalog;
  V6  parent/child counts and per-leaf exact counts reconcile (sum check);
  V7  seen IDs never exceed a region's exact count;
  V8  delivered booths: inside root, content/tags identical to catalog;
  V9  missing math and conservative bounds bracket the true counts;
  V10 completeness claim: only accepted when every leaf is fully known.

Exit code 0 = verdict pass (the report's coverage conclusion is trustworthy),
1 = at least one hard check failed.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from .client import BudgetExhausted, Client, Contradiction
from .model import Booth, Filter, load_catalog
from .service import MockService


@dataclass
class Check:
    id: str
    name: str
    ok: bool
    detail: str


@dataclass
class Verdict:
    checks: list[Check] = field(default_factory=list)
    hard_failures: list[str] = field(default_factory=list)

    def add(self, cid: str, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(cid, name, ok, detail))
        if not ok:
            self.hard_failures.append(f"{cid}: {detail}")

    @property
    def passed(self) -> bool:
        return not self.hard_failures


def verify(report_path: str, catalog_path: str) -> Verdict:
    with open(report_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    booths, fixture_version = load_catalog(catalog_path)
    by_id = {b.id: b for b in booths}

    v = Verdict()
    k = report["page_size_k"]
    budget = report["budget"]
    root = Filter.from_dict(report["root_filter"])
    pinned = report["catalog_version"]

    # V1 ------------------------------------------------------------------
    v.add("V1a", "report version matches fixture content hash",
          pinned == fixture_version, f"report={pinned} fixture={fixture_version}")
    ledger_versions = {e["version"] for e in report["ledger"] if e["version"]}
    expect_drift = report["stop_reason"] == "contradiction"
    v.add("V1b", "all recorded responses carry the pinned version (unless a drift is the documented contradiction)",
          (ledger_versions <= {pinned}) or expect_drift,
          f"versions seen={sorted(ledger_versions)} contradiction_run={expect_drift}")

    # Fresh, fault-free service + client: independent replay. The replay
    # client gets enough budget to issue every distinct request once; the
    # original run's budget accounting is checked separately in V2a/V2b.
    service = MockService(booths, version=fixture_version)
    replay_budget = max(budget, len(report["ledger"]) + len(report["partition_leaves"]) + 1)
    client = Client(service=service, budget=replay_budget, pinned_version=pinned)

    distinct_filters: list[Filter] = []
    seen: set[Filter] = set()
    for e in report["ledger"]:
        f = Filter.from_dict(e["filter"])
        if f not in seen:
            seen.add(f)
            distinct_filters.append(f)
    for leaf in report["partition_leaves"]:
        f = Filter.from_dict(leaf["filter"])
        if f not in seen:
            seen.add(f)
            distinct_filters.append(f)

    replay_errors: list[str] = []
    for f in distinct_filters:
        try:
            client.request(f, k)
        except (Contradiction, BudgetExhausted) as exc:  # pragma: no cover
            replay_errors.append(str(exc))
    v.add("V3", "independent replay of every recorded request satisfies the service contract",
          not replay_errors, "; ".join(replay_errors) or f"{len(distinct_filters)} filters replayed")

    # V2 budget/ledger -----------------------------------------------------
    ledger = report["ledger"]
    charged = report["charged_calls"]
    retries = sum(1 for e in ledger if not e["ok"])
    non_confirming = sum(1 for e in ledger if e["ok"] and not e.get("confirmed", True))
    # Every ledger row is one charged service call (failed retries and
    # contradiction-bearing responses included); nothing is charged twice.
    v.add("V2a", "charged calls equal ledger length and stay within budget",
          charged == len(ledger) and charged <= budget,
          f"charged={charged} ledger={len(ledger)} non_confirming={non_confirming} "
          f"budget={budget} retries={retries}")
    distinct_ok = len({json.dumps(e["filter"], sort_keys=True)
                       for e in ledger if e["ok"] and e.get("confirmed", True)})
    v.add("V2b", "retries and rejected responses were charged (confirmed calls <= charged calls)",
          distinct_ok <= charged,
          f"distinct_confirmed={distinct_ok} charged={charged}")
    v.add("V2c", "repeating a confirmed identical request costs nothing",
          client.charged_calls == len(distinct_filters),
          f"fresh replay charged {client.charged_calls} for {len(distinct_filters)} distinct filters")

    # V4 identity against the catalog --------------------------------------
    id_problems: list[str] = []
    for d in report["booths"]:
        b = Booth.from_dict(d)
        truth = by_id.get(b.id)
        if truth is None:
            id_problems.append(f"{b.id}: id not present in catalog")
        elif truth != b:
            id_problems.append(f"{b.id}: report {b.to_dict()} != catalog {truth.to_dict()}")
    v.add("V4", "every delivered booth matches the catalog exactly (id/tags/content)",
          not id_problems, "; ".join(id_problems) or f"{len(report['booths'])} booths identical")

    # V5 MECE over the whole catalog ---------------------------------------
    leaf_filters = [Filter.from_dict(lf["filter"]) for lf in report["partition_leaves"]]
    mece_problems: list[str] = []
    covered_root_ids: set[str] = set()
    for b in booths:
        inside = root.matches(b.tags)
        homes = [i for i, f in enumerate(leaf_filters) if f.matches(b.tags)]
        if inside:
            if len(homes) != 1:
                mece_problems.append(f"booth {b.id} inside root belongs to {len(homes)} leaves")
            else:
                covered_root_ids.add(b.id)
        elif homes:
            mece_problems.append(f"booth {b.id} outside root appears in {len(homes)} leaves")
    v.add("V5", "leaf filters are MECE over the root (proven booth by booth)",
          not mece_problems,
          "; ".join(mece_problems) or f"{len(leaf_filters)} leaves cover {len(covered_root_ids)} root booths")

    # V6 counts reconcile ---------------------------------------------------
    root_truth = sum(1 for b in booths if root.matches(b.tags))
    leaf_truth = {json.dumps(lf["filter"], sort_keys=True):
                  sum(1 for b in booths
                      if Filter.from_dict(lf["filter"]).matches(b.tags))
                  for lf in report["partition_leaves"]}
    sum_leaves = sum(leaf_truth.values())
    v.add("V6a", "sum of leaf exact counts equals root exact count",
          sum_leaves == root_truth, f"leaf sum={sum_leaves} root={root_truth}")
    count_problems: list[str] = []
    for lf in report["partition_leaves"]:
        f = Filter.from_dict(lf["filter"])
        resp = client.cache[f]
        if lf["exact_count"] is not None and lf["exact_count"] != resp.total:
            count_problems.append(f"{lf['filter']}: report count {lf['exact_count']} != replayed {resp.total}")
        if resp.total != leaf_truth[json.dumps(lf["filter"], sort_keys=True)]:
            count_problems.append(
                f"{lf['filter']}: service total {resp.total} != catalog truth "
                f"{leaf_truth[json.dumps(lf['filter'], sort_keys=True)]}")
    v.add("V6b", "every reported exact count matches an independent response",
          not count_problems, "; ".join(count_problems) or "all leaf counts verified")
    v.add("V6c", "reported root count equals catalog truth",
          report["global"]["root_exact_count"] in (None, root_truth),
          f"report={report['global']['root_exact_count']} truth={root_truth}")

    # V7 seen IDs within region capacity -----------------------------------
    cap_problems: list[str] = []
    for lf in report["partition_leaves"]:
        if lf["exact_count"] is not None and lf["known_distinct_rows"] > lf["exact_count"]:
            cap_problems.append(f"{lf['filter']}: known {lf['known_distinct_rows']} > count {lf['exact_count']}")
    v.add("V7", "known distinct IDs never exceed region exact counts",
          not cap_problems, "; ".join(cap_problems) or "within capacity everywhere")

    # V8 delivered booths inside root --------------------------------------
    outside = [d["id"] for d in report["booths"] if not root.matches(d["tags"])]
    v.add("V8", "all delivered booths match the root filter",
          not outside, f"outside-root ids={outside}")

    # V9 bounds and missing math -------------------------------------------
    bound_problems: list[str] = []
    total_missing_lo = 0
    total_missing_hi = 0
    any_open_hi = False
    for lf in report["partition_leaves"]:
        truth = leaf_truth[json.dumps(lf["filter"], sort_keys=True)]
        known = lf["known_distinct_rows"]
        lo, hi = lf["missing_bounds"]
        if known > truth:
            bound_problems.append(f"{lf['filter']}: known {known} > truth {truth}")
        if lo is not None and lo > max(0, truth - known):
            bound_problems.append(f"{lf['filter']}: missing lower bound {lo} too high")
        if hi is not None and hi < max(0, truth - known):
            bound_problems.append(f"{lf['filter']}: missing upper bound {hi} below true missing {truth - known}")
        if lf["missing_exact"] is not None and lf["missing_exact"] != truth - known:
            bound_problems.append(f"{lf['filter']}: exact missing wrong")
        total_missing_lo += lo or 0
        if hi is None:
            any_open_hi = True
        else:
            total_missing_hi += hi
    g = report["global"]
    true_missing = root_truth - len(report["booths"])
    if g["missing_exact"] is not None and g["missing_exact"] != true_missing:
        bound_problems.append(f"global exact missing {g['missing_exact']} != {true_missing}")
    glo, ghi = g["missing_bounds"]
    if glo is not None and glo > true_missing:
        bound_problems.append(f"global lower bound {glo} > true missing {true_missing}")
    if ghi is not None and ghi < true_missing:
        bound_problems.append(f"global upper bound {ghi} < true missing {true_missing}")
    v.add("V9", "missing values and conservative bounds bracket the truth",
          not bound_problems, "; ".join(bound_problems)
          or f"true global missing={true_missing}, bounds={g['missing_bounds']}")

    # V10 completeness ------------------------------------------------------
    all_full = all(lf["exact_count"] is not None and lf["missing_exact"] == 0
                   for lf in report["partition_leaves"])
    held_all = len(report["booths"]) == root_truth
    complete_ok = (report["complete"] is True) == (all_full and held_all and report["stop_reason"] == "completed")
    v.add("V10", "completeness flag agrees with independent full-coverage proof",
          complete_ok,
          f"report.complete={report['complete']} leaves_full={all_full} held_all={held_all}")

    # Contradiction classification consistency ------------------------------
    if report["stop_reason"] == "contradiction":
        v.add("V11", "contradiction runs report at least one concrete contradiction",
              len(report["contradictions"]) > 0,
              "; ".join(report["contradictions"]) or "missing contradiction detail")
    else:
        v.add("V11", "non-contradiction runs have no flagged contradictions",
              not report["contradictions"], str(report["contradictions"]))

    return v


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m boothlib.verifier <report.json> <catalog.json>", file=sys.stderr)
        return 2
    verdict = verify(argv[1], argv[2])
    out = {
        "verdict": "PASS" if verdict.passed else "FAIL",
        "checks": [{"id": c.id, "name": c.name, "ok": c.ok, "detail": c.detail}
                   for c in verdict.checks],
        "hard_failures": verdict.hard_failures,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
