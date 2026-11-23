"""Domain model: booths, conjunctive tag filters, fixed catalog versions.

A filter is a conjunction of clauses:
  * include tags: a booth must carry EVERY include tag ("must have");
  * exclude tags: a booth must carry NONE of the exclude tags ("must not have").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Filter:
    include: frozenset[str]
    exclude: frozenset[str]

    def __post_init__(self) -> None:
        inter = self.include & self.exclude
        if inter:
            raise ValueError(f"filter requires and forbids tags at once: {sorted(inter)}")

    @classmethod
    def root(cls) -> "Filter":
        return cls(frozenset(), frozenset())

    @classmethod
    def of(cls, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> "Filter":
        return cls(frozenset(include), frozenset(exclude))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Filter":
        return cls(
            frozenset(d.get("include", [])),
            frozenset(d.get("exclude", [])),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {"include": sorted(self.include), "exclude": sorted(self.exclude)}

    def matches(self, tags: Iterable[str]) -> bool:
        tagset = frozenset(tags)
        return self.include <= tagset and not (self.exclude & tagset)

    def with_include(self, tag: str) -> "Filter":
        if tag in self.exclude:
            raise ValueError(f"tag {tag!r} already excluded")
        return Filter(self.include | {tag}, self.exclude)

    def with_exclude(self, tag: str) -> "Filter":
        if tag in self.include:
            raise ValueError(f"tag {tag!r} already included")
        return Filter(self.include, self.exclude | {tag})

    def is_valid(self) -> bool:
        return not (self.include & self.exclude)


@dataclass(frozen=True)
class Booth:
    id: str
    tags: frozenset[str]
    content: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Booth":
        return cls(id=str(d["id"]), tags=frozenset(d.get("tags", [])),
                   content=d.get("content", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tags": sorted(self.tags), "content": self.content}


def catalog_version(booths: list[Booth]) -> str:
    """Deterministic version id: hash of the fully canonicalized catalog.

    Any change to any booth id / tags / content changes the version, so a
    version mismatch between responses is directly observable.
    """
    blob = json.dumps(
        [b.to_dict() for b in sorted(booths, key=lambda b: b.id)],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "v-" + hashlib.sha256(blob).hexdigest()[:16]


def load_catalog(path: str) -> tuple[list[Booth], str]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    booths = [Booth.from_dict(d) for d in data["booths"]]
    ids = [b.id for b in booths]
    if len(ids) != len(set(ids)):
        raise ValueError("catalog contains duplicate booth ids")
    declared = data.get("version")
    actual = catalog_version(booths)
    if declared is not None and declared != actual:
        raise ValueError(f"catalog file version {declared!r} disagrees with content hash {actual!r}")
    return booths, actual
