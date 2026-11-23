"""Build deterministic catalog fixtures (hand-authored, fully auditable).

Run:  python3 scripts/build_fixtures.py

Tags:
  food / tech / art  -- usable facets offered to the collector
  vip                -- real booth tag NOT offered as a facet (the collector
                        may never split on it; rows still carry it)
Booths may carry any number of tags, including zero.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boothlib.model import Booth, catalog_version  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "fixtures")


def write_catalog(name: str, booths: list[Booth]) -> None:
    assert len({b.id for b in booths}) == len(booths), "duplicate ids"
    payload = {"version": catalog_version(booths),
               "booths": [b.to_dict() for b in booths]}
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"wrote {path}: {len(booths)} booths, version {payload['version']}")


# Small catalog: every tag-combination shape represented.
SMALL = [
    Booth("B01", frozenset({"food"}), "noodle stand"),
    Booth("B02", frozenset({"tech"}), "robot demo"),
    Booth("B03", frozenset({"food", "tech"}), "smart kitchen"),       # multi-tag
    Booth("B04", frozenset({"art"}), "watercolor wall"),
    Booth("B05", frozenset({"food", "art", "vip"}), "private tasting"),  # tag outside facets
    Booth("B06", frozenset(), "silent booth"),                        # no tags
    Booth("B07", frozenset({"tech", "vip"}), "invite-only VR"),
    Booth("B08", frozenset({"art", "tech"}), "generative visuals"),
]

# Bigger catalog: enough rows that small k forces truncation + deep splits.
BIG_TAGS = {
    "B01": (["food"], "noodle stand"),
    "B02": (["tech"], "robot demo"),
    "B03": (["food", "tech"], "smart kitchen"),
    "B04": (["art"], "watercolor wall"),
    "B05": (["food", "art", "vip"], "private tasting"),
    "B06": ([], "silent booth"),
    "B07": (["tech", "vip"], "invite-only VR"),
    "B08": (["art", "tech"], "generative visuals"),
    "B09": (["food"], "bakery"),
    "B10": (["art"], "sculpture garden"),
    "B11": (["food", "tech", "art"], "expo pavilion"),
    "B12": (["vip"], "lounge"),
    "B13": (["tech"], "drone cage"),
    "B14": (["food", "art"], "cake atelier"),
    "B15": ([], "info desk"),
    "B16": (["food", "vip"], "chef table"),
}
BIG = [Booth(i, frozenset(t), c) for i, (t, c) in BIG_TAGS.items()]


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    write_catalog("catalog_small.json", SMALL)
    write_catalog("catalog_big.json", BIG)


if __name__ == "__main__":
    main()
