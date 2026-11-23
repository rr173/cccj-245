#!/usr/bin/env python3
"""机制级单元测试：纯标准库 unittest，python3 -I 直接运行。

覆盖：
  T1 小样例端到端段结果（含同秒互换、外箱随动、截止秒排除、顺序无关）；
  T2 路径/凭据不变不拆段、地点未变换路径必须拆段；
  T3 全部拒绝情形（未知展品、清册成环/悬挂/重复、记录号重复、
     物件/去向不存在、原去向不符、同秒重复移动、交接后成环、
     交接不晚于盘点、查询起点过早、空区间）；
  T4 失败不产生新结果、不覆盖上次成功结果；
  T5 外排序与输入行序无关性。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expotrace import engine, storage  # noqa: E402
from expotrace.errors import (  # noqa: E402
    HandoverError,
    ManifestError,
    QueryError,
    TraceError,
)
from expotrace.extmem import dump_record  # noqa: E402

CATALOG = {"accepted": True, "catalog_version": "t", "booth_ids": ["B1", "B2"]}


class TraceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="trace-test-")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.catalog_path = os.path.join(self.tmp, "catalog.json")
        self.manifest_path = os.path.join(self.tmp, "manifest.jsonl")
        self.handover_path = os.path.join(self.tmp, "handovers.jsonl")
        self.query_path = os.path.join(self.tmp, "query.json")
        self.out_dir = os.path.join(self.tmp, "out")
        with open(self.catalog_path, "w", encoding="utf-8") as fh:
            json.dump(CATALOG, fh)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_manifest(self, rows) -> None:
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(dump_record(r) + "\n")

    def write_handovers(self, rows) -> None:
        with open(self.handover_path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(dump_record(r) + "\n")

    def write_query(self, start=1000, end=2000, targets=("E1",)) -> None:
        with open(self.query_path, "w", encoding="utf-8") as fh:
            json.dump({"start": start, "end": end, "targets": list(targets)}, fh)

    def run_trace(self, inventory_ts=1000):
        if not os.path.exists(self.query_path):
            self.write_query()
        return engine.run_trace(
            self.catalog_path,
            self.manifest_path,
            self.handover_path,
            self.query_path,
            inventory_ts=inventory_ts,
            out_dir=self.out_dir,
            work_dir=self.work,
        )

    def segments_of(self, result, exhibit_id):
        path = os.path.join(result["trace_dir"], "segments.jsonl")
        return [
            json.loads(line)
            for line in open(path, encoding="utf-8")
            if json.loads(line)["exhibit_id"] == exhibit_id
        ]

    def all_segments(self, result):
        path = os.path.join(result["trace_dir"], "segments.jsonl")
        return [json.loads(line) for line in open(path, encoding="utf-8")]

    def basic_manifest(self):
        return [
            {"obj_id": "E1", "kind": "exhibit", "parent_id": "B1", "parent_kind": "location"},
            {"obj_id": "E2", "kind": "exhibit", "parent_id": "C2", "parent_kind": "box"},
            {"obj_id": "E3", "kind": "exhibit", "parent_id": "C3", "parent_kind": "box"},
            {"obj_id": "C1", "kind": "box", "parent_id": "B1", "parent_kind": "location"},
            {"obj_id": "C2", "kind": "box", "parent_id": "C1", "parent_kind": "box"},
            {"obj_id": "C3", "kind": "box", "parent_id": "B2", "parent_kind": "location"},
        ]


class TestEndToEnd(TraceTestBase):
    def test_demo_segments_and_evidence(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [
                {"record_id": "R3", "ts": 1100, "obj_id": "C1",
                 "old_parent": "B1", "new_parent": "B2", "new_kind": "location"},
                {"record_id": "R1", "ts": 1050, "obj_id": "E1",
                 "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
                {"record_id": "R5", "ts": 1200, "obj_id": "C1",
                 "old_parent": "B2", "new_parent": "C2", "new_kind": "box"},
                {"record_id": "R4", "ts": 1200, "obj_id": "C2",
                 "old_parent": "C1", "new_parent": "B2", "new_kind": "location"},
                {"record_id": "R6", "ts": 1300, "obj_id": "C3",
                 "old_parent": "B2", "new_parent": "RECEIVING", "new_kind": "location"},
                {"record_id": "R7", "ts": 1400, "obj_id": "E1",
                 "old_parent": "C3", "new_parent": "RECEIVING", "new_kind": "location"},
            ]
        )
        self.write_query(1000, 1400, ("E2", "E1", "E3"))
        result = self.run_trace()

        e1 = self.segments_of(result, "E1")
        self.assertEqual([(s["start"], s["end"]) for s in e1], [(1000, 1050), (1050, 1300), (1300, 1400)])
        self.assertEqual([e["to"] for e in e1[0]["path"]], ["B1"])
        self.assertEqual(e1[0]["path"][0]["evidence"], "E1")  # 初始凭据=物件号
        self.assertEqual([e["to"] for e in e1[1]["path"]], ["C3", "B2"])
        self.assertEqual([e["evidence"] for e in e1[1]["path"]], ["R1", "C3"])
        self.assertEqual([e["to"] for e in e1[2]["path"]], ["C3", "RECEIVING"])
        self.assertEqual(e1[2]["path"][1]["evidence"], "R6")

        e2 = self.segments_of(result, "E2")
        self.assertEqual([(s["start"], s["end"]) for s in e2], [(1000, 1100), (1100, 1200), (1200, 1400)])
        # 同秒互换后的终态：C2 落地 B2、C1 装进 C2（C1 成为 C2 的子）；
        # E2 仍在 C2 内，祖先链 E2 -> C2 -> B2，C2 的边凭据由初始 C2 变为 R4。
        self.assertEqual([e["to"] for e in e2[2]["path"]], ["C2", "B2"])
        self.assertEqual(
            [e["evidence"] for e in e2[2]["path"]], ["E2", "R4"]
        )

        e3 = self.segments_of(result, "E3")
        # E3 一直在 C3 里，但 R6 把 C3 从 B2 搬到收货区：地点与凭据变化，拆两段
        self.assertEqual([(s["start"], s["end"]) for s in e3], [(1000, 1300), (1300, 1400)])
        self.assertEqual([e["to"] for e in e3[0]["path"]], ["C3", "B2"])
        self.assertEqual([e["to"] for e in e3[1]["path"]], ["C3", "RECEIVING"])
        self.assertEqual(e3[1]["path"][1]["evidence"], "R6")

        # 输出按展品编号、起始秒排序
        keys = [(s["exhibit_id"], s["start"]) for s in self.all_segments(result)]
        self.assertEqual(keys, sorted(keys))

    def test_input_row_order_independence(self):
        rows = [
            {"record_id": "R1", "ts": 1050, "obj_id": "E1",
             "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
            {"record_id": "R2", "ts": 1100, "obj_id": "C3",
             "old_parent": "B2", "new_parent": "B1", "new_kind": "location"},
        ]
        self.write_manifest(self.basic_manifest())
        self.write_query(1000, 2000, ("E1",))
        outputs = []
        for ordered in (rows, list(reversed(rows))):
            self.write_handovers(ordered)
            result = self.run_trace()
            outputs.append(
                open(os.path.join(result["trace_dir"], "segments.jsonl"), encoding="utf-8").read()
            )
        self.assertEqual(outputs[0], outputs[1])

    def test_no_split_when_path_unchanged(self):
        # 移动一只与 E1 完全无关的箱子：E1 只有一段
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1100, "obj_id": "C3",
              "old_parent": "B2", "new_parent": "B1", "new_kind": "location"}]
        )
        self.write_query(1000, 2000, ("E1",))
        result = self.run_trace()
        e1 = self.segments_of(result, "E1")
        self.assertEqual(len(e1), 1)
        self.assertEqual((e1[0]["start"], e1[0]["end"]), (1000, 2000))

    def test_split_when_route_changes_but_location_same(self):
        # E1 先在 C3@B2；把 E1 改挂到另一只同样位于 B2 的箱 C4：
        # 最终地点仍是 B2，但装箱路径变了，必须拆段。
        rows = self.basic_manifest()
        rows.append({"obj_id": "C4", "kind": "box", "parent_id": "B2", "parent_kind": "location"})
        self.write_manifest(rows)
        self.write_handovers(
            [{"record_id": "R1", "ts": 1100, "obj_id": "E1",
              "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
             {"record_id": "R2", "ts": 1200, "obj_id": "E1",
              "old_parent": "C3", "new_parent": "C4", "new_kind": "box"}]
        )
        self.write_query(1000, 2000, ("E1",))
        result = self.run_trace()
        e1 = self.segments_of(result, "E1")
        self.assertEqual([(s["start"], s["end"]) for s in e1], [(1000, 1100), (1100, 1200), (1200, 2000)])
        self.assertEqual(e1[1]["path"][-1]["to"], "B2")
        self.assertEqual(e1[2]["path"][-1]["to"], "B2")
        self.assertNotEqual(
            [e["to"] for e in e1[1]["path"]], [e["to"] for e in e1[2]["path"]]
        )

    def test_pre_query_handover_sets_first_segment(self):
        # 起点之前的交接决定第一段路径与凭据
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "E1",
              "old_parent": "B1", "new_parent": "C3", "new_kind": "box"}]
        )
        self.write_query(1100, 2000, ("E1",))
        result = self.run_trace()
        e1 = self.segments_of(result, "E1")
        self.assertEqual(len(e1), 1)
        self.assertEqual([e["to"] for e in e1[0]["path"]], ["C3", "B2"])
        self.assertEqual(e1[0]["path"][0]["evidence"], "R1")


class TestRejections(TraceTestBase):
    def _expect(self, error_cls):
        return self.assertRaises(error_cls)

    def test_unknown_target(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(targets=("GHOST",))
        with self._expect(QueryError):
            self.run_trace()

    def test_target_is_box(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(targets=("C1",))
        with self._expect(QueryError):
            self.run_trace()

    def test_manifest_cycle(self):
        rows = self.basic_manifest()
        rows = [r for r in rows if r["obj_id"] != "E2"]
        rows.append({"obj_id": "E2", "kind": "exhibit", "parent_id": "C2", "parent_kind": "box"})
        # 制造 C1 <-> C2 环
        rows = [
            {**r, "parent_id": "C2", "parent_kind": "box"} if r["obj_id"] == "C1" else r
            for r in rows
        ]
        self.write_manifest(rows)
        self.write_handovers([])
        with self._expect(ManifestError):
            self.run_trace()

    def test_manifest_missing_parent_box(self):
        rows = [r for r in self.basic_manifest() if r["obj_id"] != "C2"]
        self.write_manifest(rows)
        self.write_handovers([])
        with self._expect(ManifestError):
            self.run_trace()

    def test_manifest_duplicate_id(self):
        rows = self.basic_manifest() + [
            {"obj_id": "E1", "kind": "box", "parent_id": "B1", "parent_kind": "location"}
        ]
        self.write_manifest(rows)
        self.write_handovers([])
        with self._expect(ManifestError):
            self.run_trace()

    def test_manifest_unknown_location(self):
        rows = [
            {**r, "parent_id": "B9"} if r["obj_id"] == "E1" else r
            for r in self.basic_manifest()
        ]
        self.write_manifest(rows)
        self.write_handovers([])
        with self._expect(ManifestError):
            self.run_trace()

    def test_duplicate_record_id_before_cutoff(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [
                {"record_id": "RX", "ts": 1050, "obj_id": "E1",
                 "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
                {"record_id": "RX", "ts": 1060, "obj_id": "C3",
                 "old_parent": "B2", "new_parent": "B1", "new_kind": "location"},
            ]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_duplicate_record_id_after_cutoff_ignored(self):
        # 截止秒之后（含截止秒）的记录不纳入台账，重复记录号也应被忽略
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [
                {"record_id": "RX", "ts": 2000, "obj_id": "E1",
                 "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
                {"record_id": "RX", "ts": 2001, "obj_id": "C3",
                 "old_parent": "B2", "new_parent": "B1", "new_kind": "location"},
            ]
        )
        result = self.run_trace()  # 不应抛错
        self.assertEqual(len(self.all_segments(result)), 1)

    def test_handover_unknown_object(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "X9",
              "old_parent": "B1", "new_parent": "B2", "new_kind": "location"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_handover_unknown_new_location(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "C3",
              "old_parent": "B2", "new_parent": "B9", "new_kind": "location"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_handover_into_exhibit_rejected(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "C3",
              "old_parent": "B2", "new_parent": "E1", "new_kind": "box"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_old_parent_mismatch(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "E1",
              "old_parent": "B2", "new_parent": "C3", "new_kind": "box"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_duplicate_move_same_second(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [
                {"record_id": "R1", "ts": 1050, "obj_id": "E1",
                 "old_parent": "B1", "new_parent": "C3", "new_kind": "box"},
                {"record_id": "R2", "ts": 1050, "obj_id": "E1",
                 "old_parent": "C3", "new_parent": "B2", "new_kind": "location"},
            ]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_cycle_after_handover(self):
        # 初始 C2 在 C1 里；同一秒把 C1 装进 C2，却不把 C2 取出 => 终态成环
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "C1",
              "old_parent": "B1", "new_parent": "C2", "new_kind": "box"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_self_containment_rejected(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "C1",
              "old_parent": "B1", "new_parent": "C1", "new_kind": "box"}]
        )
        with self._expect(HandoverError):
            self.run_trace()

    def test_swap_same_second_is_legal(self):
        # 题目给定的合法互换：C2 落地、C1 装进 C2，整组终态无环。
        # 先在 1100 把 C1 整体搬到 B2（C2 的旧父仍是 C1），1200 同秒互换。
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [
                {"record_id": "R3", "ts": 1100, "obj_id": "C1",
                 "old_parent": "B1", "new_parent": "B2", "new_kind": "location"},
                {"record_id": "R5", "ts": 1200, "obj_id": "C1",
                 "old_parent": "B2", "new_parent": "C2", "new_kind": "box"},
                {"record_id": "R4", "ts": 1200, "obj_id": "C2",
                 "old_parent": "C1", "new_parent": "B2", "new_kind": "location"},
            ]
        )
        self.write_query(targets=("E2",))
        result = self.run_trace()
        e2 = self.segments_of(result, "E2")
        # 互换终态：C2 落地 B2、C1 装进 C2（C1 成为 C2 的子，与 E2 平级），
        # E2 的祖先链仍是 E2 -> C2 -> B2，但 C2 的边凭据变为 R4。
        self.assertEqual([e["to"] for e in e2[-1]["path"]], ["C2", "B2"])
        self.assertEqual([e["evidence"] for e in e2[-1]["path"]], ["E2", "R4"])

    def test_handover_not_after_inventory(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1000, "obj_id": "E1",
              "old_parent": "B1", "new_parent": "C3", "new_kind": "box"}]
        )
        with self._expect(HandoverError):
            self.run_trace(inventory_ts=1000)

    def test_query_start_before_inventory(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(start=999, end=2000)
        with self._expect(QueryError):
            self.run_trace()

    def test_empty_query_range(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(start=1500, end=1500)
        with self._expect(QueryError):
            self.run_trace()

    def test_error_in_unqueried_box_still_rejects(self):
        # 问题发生在未查询的箱子 C3 上，整次查询仍必须拒绝
        self.write_manifest(self.basic_manifest())
        self.write_handovers(
            [{"record_id": "R1", "ts": 1050, "obj_id": "C3",
              "old_parent": "B2", "new_parent": "E1", "new_kind": "box"}]
        )
        self.write_query(targets=("E2",))  # E2 在 C1/C2 里，与 C3 无关
        with self._expect(HandoverError):
            self.run_trace()


class TestAtomicity(TraceTestBase):
    def test_failure_keeps_previous_success(self):
        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(targets=("E1",))
        first = self.run_trace()
        self.assertTrue(os.path.isdir(first["trace_dir"]))

        # 第二次查询非法：不得留下新目录，CURRENT 仍指向第一次结果
        self.write_query(targets=("GHOST",))
        with self.assertRaises(TraceError):
            self.run_trace()
        current = open(os.path.join(self.out_dir, "CURRENT"), encoding="utf-8").read().strip()
        self.assertEqual(current, os.path.basename(first["trace_dir"]))
        entries = sorted(os.listdir(self.out_dir))
        self.assertFalse(any(e.startswith((".staging", ".work")) for e in entries))
        self.assertEqual([e for e in entries if e.startswith("trace-")], [current])


class TestVerifier(TraceTestBase):
    def test_verifier_accepts_and_rejects_tampering(self):
        from expotrace.verifier import verify_trace
        from expotrace.errors import VerifyError

        self.write_manifest(self.basic_manifest())
        self.write_handovers([])
        self.write_query(targets=("E1",))
        result = self.run_trace()
        self.assertTrue(verify_trace(result["trace_dir"])["ok"])

        seg = os.path.join(result["trace_dir"], "segments.jsonl")
        original = open(seg, encoding="utf-8").read()
        rec = json.loads(original)
        # 篡改 1：段不再连续（删掉唯一一段后覆盖缺口）
        rec["start"] = 1200
        with open(seg, "w", encoding="utf-8") as fh:
            fh.write(dump_record(rec) + "\n")
        with self.assertRaises(VerifyError):
            verify_trace(result["trace_dir"])

        # 篡改 2：路径落到非法地点
        rec = json.loads(original)
        rec["path"][-1]["to"] = "B9"
        with open(seg, "w", encoding="utf-8") as fh:
            fh.write(dump_record(rec) + "\n")
        with self.assertRaises(VerifyError):
            verify_trace(result["trace_dir"])

        # 恢复后核验重新通过
        with open(seg, "w", encoding="utf-8") as fh:
            fh.write(original)
        self.assertTrue(verify_trace(result["trace_dir"])["ok"])


class TestExternalSort(unittest.TestCase):
    def test_sort_jsonl_runs_and_merges(self):
        from expotrace.extmem import sort_jsonl, iter_jsonl

        tmp = tempfile.mkdtemp(prefix="trace-sort-")
        try:
            src = os.path.join(tmp, "src.jsonl")
            dst = os.path.join(tmp, "dst.jsonl")
            with open(src, "w", encoding="utf-8") as fh:
                for i in range(1000):
                    fh.write(dump_record({"k": (999 - i) % 7, "i": i}) + "\n")
            n = sort_jsonl(src, dst, key=lambda r: (r["k"], r["i"]),
                           tmpdir=os.path.join(tmp, "runs"), run_size=50)
            self.assertEqual(n, 1000)
            rows = list(iter_jsonl(dst))
            keys = [(r["k"], r["i"]) for r in rows]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len(os.listdir(os.path.join(tmp, "runs"))), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
