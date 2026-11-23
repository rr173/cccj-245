"""Partition-tree explorer.

The root filter is partitioned into mutually exclusive, jointly exhaustive
regions. Splitting one region on a facet tag t replaces it by:
  * region + "must have t"
  * region + "must not have t"
which is a MECE split of exactly the booths inside the region, independent
of how many tags a booth carries (multi-tag, zero-tag booths included).

Exploration is depth-first. Every region is *probed* (one charged request,
unless an identical request is already confirmed). Probes may overlap:
a booth seen in any earlier probe is kept in a global identity-checked
registry and can later fill a truncated leaf whose exact count is known.

We never add counts from overlapping probes. A region count is only:
  (a) the exact total returned in that region's own confirmed response, or
  (b) an arithmetic consequence of confirmed sibling/parent counts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import BudgetExhausted, Client, Contradiction
from .model import Booth, Filter
from .service import ServiceResponse


@dataclass
class Node:
    filt: Filter
    parent: "Node | None"
    facet_tag: str | None          # tag this level split on (None for root)
    side: str | None               # "include" / "exclude" / None (root)
    children: list["Node"] = field(default_factory=list)
    depth: int = 0
    # confirmed exact count of this region (own response or arithmetic)
    count: int | None = None
    count_origin: str | None = None       # "response" / "arithmetic" / None
    truncated: bool = False
    rows_from_own_probe: set[str] = field(default_factory=set)
    service_call_index: int | None = None
    state: str = "unexplored"      # unexplored|explored|blocked|contradicted
    note: str | None = None

    def is_leaf(self) -> bool:
        return not self.children


class Explorer:
    def __init__(self, client: Client, root: Filter, facets: list[str], k: int) -> None:
        self.client = client
        self.root_filter = root
        self.facets = list(facets)
        self.k = k
        self.root = Node(root, None, None, None)
        self.used_facets: set[str] = set()
        # global registry: id -> Booth, identity/content checked in client
        self.registry: dict[str, Booth] = {}
        self.contradictions: list[str] = []
        self.stop_reason: str = "in_progress"   # completed|budget_exhausted|contradiction
        self.budget_note: str | None = None

    # -- main entry ----------------------------------------------------------
    def run(self) -> None:
        try:
            self._explore(self.root)
            if self.stop_reason == "in_progress":
                self.stop_reason = "completed"
        except BudgetExhausted as exc:
            self.stop_reason = "budget_exhausted"
            self.budget_note = str(exc)
            self._mark_remaining_blocked(self.root)
        except Contradiction as exc:
            self.stop_reason = "contradiction"
            self.budget_note = "; ".join(exc.problems)
            self._record_contradiction(exc.problems)
            self._mark_remaining_blocked(self.root)

        self._reconcile()

    # -- DFS -----------------------------------------------------------------
    def _explore(self, node: Node) -> None:
        resp = self._probe(node)
        if resp is None:
            return  # budget exhausted; node stays blocked (marked later)
        node.count = resp.total
        node.count_origin = "response"
        node.truncated = resp.truncated
        node.service_call_index = resp.service_call_index
        node.rows_from_own_probe = {r.id for r in resp.rows}
        node.state = "explored"

        if not resp.truncated:
            # Exhaustive probe: every matching row was returned.
            return

        # Truncated region: try to split it on a usable facet.
        tag = self._pick_facet(node)
        if tag is None:
            node.note = "truncated and no usable facet remains: cannot subdivide"
            return

        inc = Node(node.filt.with_include(tag), node, tag, "include", depth=node.depth + 1)
        exc = Node(node.filt.with_exclude(tag), node, tag, "exclude", depth=node.depth + 1)
        node.children = [inc, exc]
        self._explore(inc)
        if self.stop_reason != "in_progress":
            return
        self._explore(exc)
        if self.stop_reason != "in_progress":
            return
        self._check_parent_child(node)

    def _probe(self, node: Node) -> ServiceResponse | None:
        try:
            resp = self.client.request(node.filt, self.k)
        except BudgetExhausted:
            node.state = "blocked"
            node.note = "not probed: call budget exhausted"
            raise
        except Contradiction as exc:
            node.state = "contradicted"
            node.note = "; ".join(exc.problems)
            raise
        for row in resp.rows:
            self.registry[row.id] = row
        return resp

    def _pick_facet(self, node: Node) -> str | None:
        used = set()
        cur: Node | None = node
        while cur is not None:
            if cur.facet_tag is not None:
                used.add(cur.facet_tag)
            cur = cur.parent
        for tag in self.facets:
            if tag not in used:
                return tag
        return None

    # -- structural count checks ---------------------------------------------
    def _check_parent_child(self, node: Node) -> None:
        if node.count is None or any(c.count is None for c in node.children):
            return
        child_sum = sum(c.count for c in node.children)  # MECE: safe to sum
        if child_sum != node.count:
            self.contradictions.append(
                f"parent/child count mismatch at {node.filt.to_dict()}: "
                f"parent={node.count} but children sum={child_sum}"
            )
            node.state = "contradicted"
            return
        # A zero child needs no further work; derive nothing beyond its 0.

    def _mark_remaining_blocked(self, n: Node) -> None:
        if n.state == "unexplored":
            n.state = "blocked"
            n.note = (n.note or "") + " | not reached before run stopped"
        for c in n.children:
            self._mark_remaining_blocked(c)

    def _record_contradiction(self, problems: list[str]) -> None:
        self.contradictions.extend(problems)

    # -- reconciliation over the final MECE partition ------------------------
    def _reconcile(self) -> None:
        leaves = self._leaves()
        # Every observed row must match the root filter (only root data kept).
        for bid, booth in self.registry.items():
            if not self.root_filter.matches(booth.tags):
                self.contradictions.append(
                    f"booth {bid!r} outside root filter {self.root_filter.to_dict()}"
                )

        for leaf in leaves:
            members = sorted(
                bid for bid, booth in self.registry.items() if leaf.filt.matches(booth.tags)
            )
            leaf.known_members = members  # type: ignore[attr-defined]
            if leaf.count is not None and len(members) > leaf.count:
                self.contradictions.append(
                    f"seen IDs exceed region count at {leaf.filt.to_dict()}: "
                    f"{len(members)} distinct known IDs but exact count={leaf.count}"
                )

        # MECE membership proof: each observed booth lands in exactly one leaf.
        for bid, booth in self.registry.items():
            homes = [lf for lf in leaves if lf.filt.matches(booth.tags)]
            if len(homes) != 1:
                self.contradictions.append(
                    f"booth {bid!r} belongs to {len(homes)} partition leaves (need exactly 1)"
                )

        if self.contradictions:
            self.stop_reason = "contradiction"

    def _leaves(self) -> list[Node]:
        out: list[Node] = []

        def walk(n: Node) -> None:
            if n.is_leaf():
                out.append(n)
            for c in n.children:
                walk(c)

        walk(self.root)
        return out

    # -- conservative bounds -------------------------------------------------
    def _upper_bound(self, n: Node) -> int | None:
        """Upper bound on booths in region n supported by observed evidence.

        * own confirmed count  -> exact;
        * otherwise, if parent and all siblings are confirmed -> residual;
        * otherwise walk up to the nearest confirmed ancestor and take the
          loosest residual available there.
        """
        if n.count is not None:
            return n.count
        parent = n.parent
        if parent is None:
            return None
        sibs = [c for c in parent.children if c is not n]
        if parent.count is not None and all(s.count is not None for s in sibs):
            residual = parent.count - sum(s.count for s in sibs)  # type: ignore[arg-type]
            return max(0, residual)
        anc = parent
        while anc is not None:
            if anc.count is not None:
                # loosest defensible bound: the whole ancestor region
                return anc.count
            anc = anc.parent
        return None

    def _lower_bound(self, n: Node) -> int:
        return len(getattr(n, "known_members", []))

    # -- report --------------------------------------------------------------
    def build_report(self) -> dict[str, Any]:
        leaves = self._leaves()
        leaf_reports: list[dict[str, Any]] = []
        total_known = 0
        miss_lo = 0
        miss_hi = 0
        all_exact = True

        for lf in leaves:
            known = getattr(lf, "known_members", [])
            ub = self._upper_bound(lf)
            lb = len(known)
            if lf.count is not None:
                missing = lf.count - len(known)
                if missing < 0:
                    all_exact = False
                entry = {
                    "filter": lf.filt.to_dict(),
                    "exact_count": lf.count,
                    "count_origin": lf.count_origin,
                    "known_distinct_rows": len(known),
                    "missing_exact": max(0, missing),
                    "missing_bounds": [max(0, missing), max(0, missing)],
                    "truncated_probe": lf.truncated,
                    "state": lf.state,
                    "note": lf.note,
                    "evidence_call_index": lf.service_call_index,
                }
                total_known += len(known)
                miss_lo += max(0, missing)
                miss_hi += max(0, missing)
            else:
                all_exact = False
                hi = None if ub is None else max(lb, ub)
                entry = {
                    "filter": lf.filt.to_dict(),
                    "exact_count": None,
                    "count_origin": None,
                    "known_distinct_rows": lb,
                    "missing_exact": None,
                    "missing_bounds": [0, None if hi is None else max(0, hi - lb)],
                    "truncated_probe": lf.truncated,
                    "state": lf.state,
                    "note": lf.note,
                    "evidence_call_index": lf.service_call_index,
                }
                total_known += lb
                miss_lo += 0
                if hi is not None:
                    miss_hi += max(0, hi - lb)
                else:
                    miss_hi = None  # type: ignore[assignment]
            leaf_reports.append(entry)

        root_confirmed = self.root.count is not None
        if root_confirmed:
            global_missing = [self.root.count - total_known, self.root.count - total_known]
        else:
            global_missing = [None, None]

        complete = (
            self.stop_reason == "completed"
            and not self.contradictions
            and all_exact
            and root_confirmed
            and all(e["missing_exact"] == 0 for e in leaf_reports)
        )

        termination = self._termination()

        return {
            "schema_version": 1,
            "catalog_version": self.client.pinned_version,
            "root_filter": self.root_filter.to_dict(),
            "facets": self.facets,
            "page_size_k": self.k,
            "budget": self.client.budget,
            "charged_calls": self.client.charged_calls,
            "free_replays": self.client.cache_hits,
            "stop_reason": self.stop_reason,
            "termination_class": termination,
            "complete": complete,
            "global": {
                "root_exact_count": self.root.count,
                "distinct_rows_held": len(self.registry),
                "missing_exact": (self.root.count - len(self.registry)) if root_confirmed else None,
                "missing_bounds": global_missing,
            },
            "partition_leaves": leaf_reports,
            "contradictions": self.contradictions,
            "ledger": [
                {
                    "call_index": e.call_index,
                    "filter": e.filter.to_dict(),
                    "k": e.k,
                    "ok": e.ok,
                    "confirmed": e.confirmed,
                    "error": e.error,
                    "total": e.total,
                    "returned": e.returned,
                    "version": e.version,
                }
                for e in self.client.ledger
            ],
            "booths": [self.registry[i].to_dict() for i in sorted(self.registry)],
        }

    def _termination(self) -> str:
        if self.contradictions or self.stop_reason == "contradiction":
            return "response_contradiction"
        if self.stop_reason == "budget_exhausted":
            truncated_leaves = [
                lf for lf in self._leaves()
                if lf.state == "explored" and lf.truncated and not lf.children
            ]
            if truncated_leaves:
                return "budget_exhausted_with_unsplittable_truncation"
            return "budget_exhausted"
        if self.stop_reason == "completed":
            truncated_leaves = [
                lf for lf in self._leaves() if lf.truncated
            ]
            if truncated_leaves:
                return "unsplittable_truncation"
            return "completed"
        return self.stop_reason
