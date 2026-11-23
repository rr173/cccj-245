"""Independently verify every report in out/ against its catalog fixture.

Exit code is nonzero if ANY report fails verification.
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boothlib.verifier import verify  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    reports = sorted(glob.glob(os.path.join(ROOT, "out", "*.json")))
    if not reports:
        print("no reports found; run run_demo.py first", file=sys.stderr)
        return 2
    overall_ok = True
    for path in reports:
        name = os.path.basename(path)
        with open(path, "r", encoding="utf-8") as fh:
            catalog = json.load(fh)["catalog_file"]
        verdict = verify(path, os.path.join(ROOT, "fixtures", catalog))
        status = "PASS" if verdict.passed else "FAIL"
        print(f"[{status}] {name}  ({len(verdict.checks)} checks)")
        for c in verdict.checks:
            if not c.ok:
                print(f"    x {c.id}: {c.detail}")
        overall_ok &= verdict.passed
    print("=" * 60)
    print("OVERALL:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
