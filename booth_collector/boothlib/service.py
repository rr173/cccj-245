"""Local mock exhibition-catalog service.

Contract (the only API the collector is allowed to touch):

    query(filter, k) -> ServiceResponse

Each response contains:
  * the *canonical* filter echoed back (sorted include/exclude),
  * the catalog version (identical for every response of one catalog),
  * the *exact* total number of booths matching the filter,
  * min(k, total) booths with distinct IDs, each with its tags and content.

Order is deliberately unspecified. Booth selection is deterministic but
keyed on the canonical filter, so different filters return different
subsets and overlapping probes are realistic. No network access happens.

Fault injection is explicit and off by default:
  * flaky: every *new* request first raises TransientError; the retry
    (same canonical request) succeeds, and the retry costs budget too.
  * wrong_total: one chosen canonical request gets an inflated total.
  * version_drift: one chosen canonical request reports a bad version.
  * row_mismatch: one chosen canonical request mutates a returned row's
    content/tags relative to the canonical catalog.
  * duplicate_rows: one chosen request returns repeated IDs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .model import Booth, Filter, catalog_version


class ServiceError(Exception):
    """Base class for service-side failures."""


class TransientError(ServiceError):
    """Retryable transport failure. Retries count against the budget."""


@dataclass(frozen=True)
class ServiceResponse:
    filter: Filter
    version: str
    total: int
    rows: tuple[Booth, ...]
    truncated: bool
    service_call_index: int  # 1-based index among *charged* calls


class MockService:
    def __init__(
        self,
        booths: list[Booth],
        *,
        version: str | None = None,
        page_cap: int = 1000,
        flaky: bool = False,
        wrong_total_on: Filter | None = None,
        version_drift_on: Filter | None = None,
        row_mismatch_on: Filter | None = None,
        duplicate_rows_on: Filter | None = None,
    ) -> None:
        self._booths = list(booths)
        self.version = version or catalog_version(self._booths)
        self.page_cap = page_cap
        self.flaky = flaky
        self.wrong_total_on = wrong_total_on
        self.version_drift_on = version_drift_on
        self.row_mismatch_on = row_mismatch_on
        self.duplicate_rows_on = duplicate_rows_on
        self.calls_charged = 0
        # Canonical requests already fully served: repeating them is free.
        self._served: set[Filter] = set()

    # -- deterministic subset selection -------------------------------------
    def _select(self, filt: Filter, k: int, matching: list[Booth]) -> list[Booth]:
        """Pick min(k, total) distinct rows deterministically per filter.

        The permutation key mixes booth id with the *canonical filter*, so
        two different probes return different orderings and subsets may
        overlap without being identical.
        """
        key = repr(filt.to_dict()).encode("utf-8")
        ranked = sorted(
            matching,
            key=lambda b: hashlib.sha256(key + b"|" + b.id.encode("utf-8")).hexdigest(),
        )
        return ranked[:k]

    def query(self, filt: Filter, k: int) -> ServiceResponse:
        if not filt.is_valid():
            raise ValueError("invalid filter: tag both required and forbidden")
        if k <= 0:
            raise ValueError("k must be positive")
        k = min(k, self.page_cap)

        # A request whose *canonical* form was already answered is served
        # from the service-side confirmation cache for free.
        if filt in self._served:
            matching = [b for b in self._booths if filt.matches(b.tags)]
            rows = tuple(self._select(filt, k, matching))
            return ServiceResponse(filt, self.version, len(matching), rows, len(rows) < len(matching), 0)

        # New request: charge it, then possibly fail transiently.
        self.calls_charged += 1
        idx = self.calls_charged
        if self.flaky:
            self.flaky = False  # exactly one injected failure per run
            raise TransientError("simulated transport failure (retry allowed, retry costs budget)")

        matching = [b for b in self._booths if filt.matches(b.tags)]
        total = len(matching)
        rows = list(self._select(filt, k, matching))
        version = self.version

        if self.wrong_total_on is not None and filt == self.wrong_total_on:
            total = total + 3
        if self.version_drift_on is not None and filt == self.version_drift_on:
            version = "v-DRIFT"
        if self.row_mismatch_on is not None and filt == self.row_mismatch_on and rows:
            rows[0] = Booth(rows[0].id, frozenset({"TAMPERED"}), rows[0].content + " [mutated]")
        if self.duplicate_rows_on is not None and filt == self.duplicate_rows_on and len(rows) >= 2:
            rows[1] = rows[0]

        self._served.add(filt)
        distinct = {r.id for r in rows}
        if len(distinct) != len(rows):
            # Service broken contract: distinct IDs not guaranteed.
            # Still return what the (buggy) service produced.
            pass
        truncated = len(rows) < total
        return ServiceResponse(filt, version, total, tuple(rows), truncated, idx)
