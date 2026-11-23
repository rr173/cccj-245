"""Budgeted client around the mock service.

Responsibilities:
  * enforce the call budget (retries included);
  * keep a ledger of every charged call and every served request;
  * cache *confirmed* identical canonical requests (re-asking is free);
  * perform low-level response checks shared by collector and verifier:
      - row filter match,
      - in-request count (len(rows) == min(k,total) and rows <= total),
      - distinct row IDs,
      - version agreement with the pinned version,
      - stable identity/content of an ID across responses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Booth, Filter
from .service import MockService, ServiceError, ServiceResponse, TransientError


class BudgetExhausted(Exception):
    def __init__(self, budget: int) -> None:
        super().__init__(f"call budget exhausted after {budget} charged call(s)")
        self.budget = budget


@dataclass
class LedgerEntry:
    call_index: int
    filter: Filter
    k: int
    ok: bool
    error: str | None = None
    total: int | None = None
    returned: int | None = None
    version: str | None = None
    confirmed: bool = True  # False: response failed checks and was not cached


@dataclass
class Client:
    service: MockService
    budget: int
    pinned_version: str
    ledger: list[LedgerEntry] = field(default_factory=list)
    # canonical filter -> the confirmed response (free replay afterwards)
    cache: dict[Filter, ServiceResponse] = field(default_factory=dict)
    # booth id -> canonical Booth record, for cross-response identity checks
    known_rows: dict[str, Booth] = field(default_factory=dict)
    charged_calls: int = 0
    cache_hits: int = 0

    @property
    def budget_left(self) -> int:
        return self.budget - self.charged_calls

    def request(self, filt: Filter, k: int) -> ServiceResponse:
        """Serve one request; raise BudgetExhausted or Contradiction.

        Replaying a previously confirmed identical request never charges
        budget and never raises BudgetExhausted.
        """
        if filt in self.cache:
            self.cache_hits += 1
            return self.cache[filt]
        if self.budget_left <= 0:
            raise BudgetExhausted(self.budget)

        try:
            resp = self.service.query(filt, k)
        except TransientError as exc:
            # The failed attempt still consumed a charged service call.
            self.charged_calls = self.service.calls_charged
            self.ledger.append(
                LedgerEntry(self.charged_calls, filt, k, ok=False, error=str(exc))
            )
            if self.budget_left <= 0:
                raise BudgetExhausted(self.budget)
            # Retry: same canonical request, and it costs budget as well.
            try:
                resp = self.service.query(filt, k)
            except TransientError as exc2:  # pragma: no cover - defensive
                self.charged_calls = self.service.calls_charged
                self.ledger.append(
                    LedgerEntry(self.charged_calls, filt, k, ok=False, error=str(exc2))
                )
                raise
        except ServiceError as exc:  # pragma: no cover - defensive
            self.charged_calls = self.service.calls_charged
            self.ledger.append(
                LedgerEntry(self.charged_calls, filt, k, ok=False, error=str(exc))
            )
            raise

        self.charged_calls = self.service.calls_charged
        entry = LedgerEntry(
            self.charged_calls, filt, k, ok=True,
            total=resp.total, returned=len(resp.rows), version=resp.version,
        )
        self.ledger.append(entry)
        try:
            self._check_response(filt, k, resp)
        except Contradiction:
            entry.confirmed = False
            raise
        self.cache[filt] = resp
        return resp

    # -- low-level contradiction checks -------------------------------------
    def _check_response(self, filt: Filter, k: int, resp: ServiceResponse) -> None:
        problems: list[str] = []

        if resp.version != self.pinned_version:
            problems.append(
                f"version mismatch on {filt.to_dict()}: "
                f"{resp.version!r} != pinned {self.pinned_version!r}"
            )

        if not filt.is_valid():
            problems.append(f"filter contradicts itself: {filt.to_dict()}")

        ids = [r.id for r in resp.rows]
        if len(ids) != len(set(ids)):
            problems.append(f"service returned duplicate IDs within one request {filt.to_dict()}")

        if len(resp.rows) > resp.total:
            problems.append(
                f"in-request count violated on {filt.to_dict()}: "
                f"{len(resp.rows)} rows but total={resp.total}"
            )
        expected = min(k, resp.total)
        if len(resp.rows) != expected:
            problems.append(
                f"in-request count violated on {filt.to_dict()}: "
                f"returned {len(resp.rows)}, expected min(k={k}, total={resp.total})={expected}"
            )

        for row in resp.rows:
            if not filt.matches(row.tags):
                problems.append(
                    f"row {row.id!r} does not match its own request filter {filt.to_dict()}: "
                    f"tags {sorted(row.tags)}"
                )
            prior = self.known_rows.get(row.id)
            if prior is not None and prior != row:
                problems.append(
                    f"identity drift for booth {row.id!r}: earlier {prior.to_dict()} "
                    f"vs later {row.to_dict()}"
                )
            else:
                self.known_rows[row.id] = row

        if problems:
            # The bad response stays in the ledger but is never confirmed,
            # so it does not enter the cache and does not count as a
            # reusable confirmed request.
            self.ledger[-1].confirmed = False
            raise Contradiction(problems)


class Contradiction(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
