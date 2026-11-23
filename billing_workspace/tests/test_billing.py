"""离线出账机制级测试（标准库 unittest，python3 -I 运行）。

覆盖：报告准入、参数校验、表计日志版本解析（修订/撤销/重复/冲突/越界批次）、
读数同秒与倒退检查、归属区间校验、结算语义（手工核算的示例期望值）、
单日与多日窗口一致性、输入行序无关性、失败原子性、整数分配平局规则、
缺测合并、独立核验，以及小规模随机输入上与朴素参照实现的对拍（外排
强制多轮归并）。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from billing import settle  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")
S1_REPORT = os.path.join(ROOT, "..", "booth_collector", "out", "S1_complete.json")

# 示例使用的关键时刻（UTC 整数秒）
D0 = 1740700800   # 2025-02-28 00:00
D1 = 1740787200   # 2025-03-01 00:00
D2 = 1740873600   # 2025-03-02 00:00
D3 = 1740960000   # 2025-03-03 00:00
H = 3600


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def write_jsonl(path: str, records) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            if isinstance(rec, str):
                fh.write(rec)
            else:
                fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")


def read_jsonl(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def make_report(path: str, booths=("B01", "B02", "B03", "B04", "B05"),
                complete=True, root_include=None) -> str:
    report = {
        "schema_version": 1,
        "catalog_version": "v-test",
        "root_filter": {"include": root_include or [], "exclude": []},
        "complete": complete,
        "termination_class": "completed" if complete else "budget_exhausted",
        "booths": [{"id": b, "tags": [], "content": ""} for b in booths],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False)
    return path


def upsert(meter, reading, batch, time, wh, period="P"):
    return {"meter": meter, "reading": reading, "batch": batch,
            "time": time, "period": period, "wh": wh}


def revoke(meter, reading, batch):
    return {"meter": meter, "reading": reading, "batch": batch, "revoke": True}


def attr(meter, booth, start, end):
    return {"meter": meter, "booth": booth, "start": start, "end": end}


class BillingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="billing-test-")
        self.report = make_report(os.path.join(self.tmp, "report.json"))
        self.log = os.path.join(self.tmp, "log.jsonl")
        self.attr = os.path.join(self.tmp, "attr.jsonl")
        self.out = os.path.join(self.tmp, "out")
        write_jsonl(self.log, [])
        write_jsonl(self.attr, [])

    def run_billing(self, date_from="2025-03-01", date_to="2025-03-01",
                    batch_bound=5, max_gap=H, **kwargs):
        return settle.run_billing(
            report_path=kwargs.pop("report_path", self.report),
            log_path=kwargs.pop("log_path", self.log),
            attr_path=kwargs.pop("attr_path", self.attr),
            batch_bound=batch_bound,
            date_from=date_from,
            date_to=date_to,
            max_gap_seconds=max_gap,
            out_dir=self.out,
            **kwargs,
        )

    def bill_files(self, bill_dir):
        return (
            read_jsonl(os.path.join(bill_dir, "summary.jsonl")),
            read_jsonl(os.path.join(bill_dir, "known_details.jsonl")),
            read_jsonl(os.path.join(bill_dir, "missing_details.jsonl")),
        )


# --------------------------------------------------------------------------- #
# 报告准入与参数
# --------------------------------------------------------------------------- #
class ReportAndParameterTest(BillingTestBase):
    def test_reject_incomplete_report(self):
        bad = make_report(os.path.join(self.tmp, "bad.json"), complete=False)
        with self.assertRaises(settle.ReportRejected):
            self.run_billing(report_path=bad)

    def test_reject_root_filter(self):
        bad = make_report(os.path.join(self.tmp, "bad.json"), root_include=["food"])
        with self.assertRaises(settle.ReportRejected):
            self.run_billing(report_path=bad)

    def test_reject_duplicate_booth_ids(self):
        path = os.path.join(self.tmp, "bad.json")
        make_report(path, booths=("B01", "B01"))
        with self.assertRaises(settle.ReportRejected):
            self.run_billing(report_path=path)

    def test_bad_parameters(self):
        with self.assertRaises(settle.BadParameter):
            self.run_billing(batch_bound=-1)
        with self.assertRaises(settle.BadParameter):
            self.run_billing(max_gap=0)
        with self.assertRaises(settle.BadParameter):
            self.run_billing(date_from="2025-03-02", date_to="2025-03-01")
        with self.assertRaises(settle.BadParameter):
            self.run_billing(date_from="2025-3-1")
        with self.assertRaises(settle.BadParameter):
            self.run_billing(date_to="2025-02-30")


# --------------------------------------------------------------------------- #
# 表计日志版本解析
# --------------------------------------------------------------------------- #
class LogResolutionTest(BillingTestBase):
    def test_out_of_order_and_latest_visible_wins(self):
        # r1 批次 2 把抄表时间从 01:00 改到 02:00；批次 9 越界忽略
        write_jsonl(self.log, [
            upsert("M", "r2", 0, D1 + 4 * H, 200),
            upsert("M", "r1", 9, D1, 999),              # 越界批次，忽略
            upsert("M", "r1", 2, D1 + 2 * H, 150),      # 最新可见：改时间
            upsert("M", "r1", 0, D1 + H, 100),
        ])
        self.run_billing(max_gap=2 * H)
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["interval_start"], D1 + 2 * H)
        self.assertEqual(known[0]["interval_end"], D1 + 4 * H)
        self.assertEqual(known[0]["wh"], 50)
        self.assertEqual(known[0]["batch_left"], 2)

    def test_revoke_hides_reading(self):
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 100),
            upsert("M", "r2", 0, D1 + H, 200),
            revoke("M", "r2", 1),                       # r2 撤销 → 无区间
        ])
        self.run_billing()
        summary, known, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(known, [])
        self.assertEqual(summary[0]["missing_meter_seconds"], 24 * H)

    def test_revoke_then_revised_again(self):
        write_jsonl(self.log, [
            revoke("M", "r1", 2),
            upsert("M", "r1", 0, D1, 100),
            upsert("M", "r1", 3, D1, 100),              # 撤销后又修订 → 有效
            upsert("M", "r2", 0, D1 + H, 200),
        ])
        self.run_billing()
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["batch_left"], 3)

    def test_duplicate_identical_lines_count_once(self):
        line = upsert("M", "r1", 0, D1, 100)
        write_jsonl(self.log, [line, dict(line), upsert("M", "r2", 0, D1 + H, 300)])
        result = self.run_billing()
        self.assertEqual(result["manifest"]["counts"]["visible_revisions"], 3)
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["wh"], 200)

    def test_conflicting_same_batch_rejected(self):
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 100),
            upsert("M", "r1", 0, D1, 101),
        ])
        with self.assertRaises(settle.LogConflict):
            self.run_billing()

    def test_invisible_lines_never_conflict(self):
        # 越界批次（>上界）内容再乱也不参与冲突检查
        write_jsonl(self.log, [
            upsert("M", "r1", 8, D1, 100),
            upsert("M", "r1", 9, D1 + H, 200),
            upsert("M", "r1", 0, D1, 100),
            upsert("M", "r2", 0, D1 + H, 200),
        ])
        self.run_billing(batch_bound=5)
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)

    def test_log_format_errors(self):
        bad_lines = [
            {"meter": "M", "reading": "r", "batch": 0, "revoke": True, "time": D1},
            {"meter": "M", "reading": "r", "batch": 0, "time": D1, "period": "P"},
            {"meter": "M", "reading": "r", "batch": -1, "time": D1, "period": "P", "wh": 0},
            {"meter": "M", "reading": "r", "batch": True, "time": D1, "period": "P", "wh": 0},
            {"meter": "M", "reading": "r", "batch": 0, "time": D1, "period": "P", "wh": -1},
            {"meter": "M", "reading": "r", "batch": 0, "time": D1, "period": "P", "wh": 0,
             "comment": "unknown"},
            {"meter": "", "reading": "r", "batch": 0, "time": D1, "period": "P", "wh": 0},
        ]
        for i, obj in enumerate(bad_lines):
            write_jsonl(self.log, [obj])
            with self.assertRaises(settle.LogFormatError, msg=f"case {i}"):
                self.run_billing()
        write_jsonl(self.log, ["not json"])
        with self.assertRaises(settle.LogFormatError):
            self.run_billing()


# --------------------------------------------------------------------------- #
# 读数检查与归属表校验
# --------------------------------------------------------------------------- #
class ReadingAndAttributionTest(BillingTestBase):
    def test_same_second_rejected(self):
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1 + H, 100),
            upsert("M", "r2", 0, D1 + H, 200),
        ])
        with self.assertRaises(settle.ReadingSameSecond):
            self.run_billing()

    def test_regression_rejected(self):
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 200),
            upsert("M", "r2", 0, D1 + H, 100),
        ])
        with self.assertRaises(settle.ReadingRegression):
            self.run_billing()

    def test_cross_period_drop_allowed(self):
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 200, period="P"),
            upsert("M", "r2", 0, D1 + H, 100, period="Q"),
            upsert("M", "r3", 0, D1 + 2 * H, 150, period="Q"),
        ])
        self.run_billing()
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)   # 只有 Q 周期内的一段
        self.assertEqual(known[0]["wh"], 50)

    def test_unknown_booth_rejected(self):
        write_jsonl(self.attr, [attr("M", "B99", D1, D1 + H)])
        with self.assertRaises(settle.UnknownBooth):
            self.run_billing()

    def test_invalid_interval_rejected(self):
        write_jsonl(self.attr, [attr("M", "B01", D1 + H, D1)])
        with self.assertRaises(settle.AttributionFormatError):
            self.run_billing()

    def test_overlap_rejected(self):
        write_jsonl(self.attr, [
            attr("M", "B01", D1, D1 + 2 * H),
            attr("M", "B02", D1 + H, D1 + 3 * H),
        ])
        with self.assertRaises(settle.AttributionOverlap):
            self.run_billing()

    def test_duplicate_attribution_lines_are_overlap(self):
        write_jsonl(self.attr, [attr("M", "B01", D1, D1 + H), attr("M", "B01", D1, D1 + H)])
        with self.assertRaises(settle.AttributionOverlap):
            self.run_billing()

    def test_adjacent_attribution_allowed(self):
        write_jsonl(self.attr, [
            attr("M", "B01", D1, D1 + H),
            attr("M", "B02", D1 + H, D1 + 2 * H),
        ])
        self.run_billing()


# --------------------------------------------------------------------------- #
# 结算语义：手工核算的示例期望值
# --------------------------------------------------------------------------- #
class DemoFixtureTest(BillingTestBase):
    def run_demo(self, date_from="2025-03-01", date_to="2025-03-02"):
        return settle.run_billing(
            report_path=S1_REPORT,
            log_path=os.path.join(EXAMPLES, "meter_log.jsonl"),
            attr_path=os.path.join(EXAMPLES, "attribution.jsonl"),
            batch_bound=5,
            date_from=date_from,
            date_to=date_to,
            max_gap_seconds=4 * H,
            out_dir=self.out,
        )

    def test_summary_matches_hand_computed(self):
        self.run_demo()
        summary, known, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(
            summary,
            [
                {"date": "2025-03-01", "booth": "B01", "known_wh": 3600, "missing_meter_seconds": 0},
                {"date": "2025-03-01", "booth": "B02", "known_wh": 300, "missing_meter_seconds": 32400},
                {"date": "2025-03-01", "booth": "B03", "known_wh": 900, "missing_meter_seconds": 7200},
                {"date": "2025-03-01", "booth": "B04", "known_wh": 0, "missing_meter_seconds": 86400},
                {"date": "2025-03-01", "booth": None, "known_wh": 120, "missing_meter_seconds": 104400},
                {"date": "2025-03-02", "booth": "B01", "known_wh": 3600, "missing_meter_seconds": 0},
                {"date": "2025-03-02", "booth": "B03", "known_wh": 600, "missing_meter_seconds": 75600},
                {"date": "2025-03-02", "booth": "B04", "known_wh": 0, "missing_meter_seconds": 86400},
                {"date": "2025-03-02", "booth": None, "known_wh": 120, "missing_meter_seconds": 82800},
            ],
        )
        self.assertEqual(len(known), 22)
        self.assertEqual(len(missing), 11)
        # 2025-03-02 没有 B02 的键：只交付实际有明细的汇总键
        self.assertNotIn(("2025-03-02", "B02"), {(r["date"], r["booth"]) for r in summary})

    def test_revision_and_zero_interval_in_details(self):
        self.run_demo()
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        by_interval = {(r["meter"], r["interval_start"]): r for r in known}
        # M1 的 r3 被批次 2 修订到 06:00，区间 [02:00,06:00) 用 batch_right=2
        self.assertEqual(by_interval[("M1", D1 + 2 * H)]["batch_right"], 2)
        self.assertEqual(by_interval[("M1", D1 + 2 * H)]["wh"], 600)
        # M2 的 m2→m3 读数相同：实测到零（已知 0），不是缺测
        zero = by_interval[("M2", D1 + 1 * H)]
        self.assertEqual(zero["wh"], 0)
        self.assertEqual(zero["seconds"], 2 * H)
        # 窗口外读数构成的区间只发布窗口内部分：M1 [D0 22:00, D1 02:00)
        edge = by_interval[("M1", D0 + 22 * H)]
        self.assertEqual(edge["date"], "2025-03-01")
        self.assertEqual(edge["seconds"], 2 * H)
        self.assertEqual(edge["wh"], 300)

    def test_missing_segments_split_by_attribution(self):
        self.run_demo()
        _, _, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        m2 = [r for r in missing if r["meter"] == "M2" and r["date"] == "2025-03-01"]
        # 明细按 (日期, 归属, 表号, 起止) 规范排序：未归属排在实际展位之后
        self.assertEqual(
            [(r["booth"], r["start"], r["end"]) for r in m2],
            [
                ("B02", D1 + 3 * H, D1 + 12 * H),
                ("B03", D1 + 18 * H, D1 + 20 * H),
                (None, D1 + 12 * H, D1 + 18 * H),
            ],
        )

    def test_window_consistency_single_vs_multi_day(self):
        self.run_demo()
        self.run_demo(date_from="2025-03-01", date_to="2025-03-01")
        self.run_demo(date_from="2025-03-02", date_to="2025-03-02")
        two_day = os.path.join(self.out, "bill-000001")
        for day, bill in (("2025-03-01", "bill-000002"), ("2025-03-02", "bill-000003")):
            for name in ("summary.jsonl", "known_details.jsonl", "missing_details.jsonl"):
                multi = [l for l in read_text(os.path.join(two_day, name)).splitlines()
                         if f'"date":"{day}"' in l]
                single = read_text(os.path.join(self.out, bill, name)).splitlines()
                self.assertEqual(multi, single, f"{day} {name}")

    def test_verify_passes(self):
        self.run_demo()
        result = settle.verify_bill(self.out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"]["summary_rows"], 9)


# --------------------------------------------------------------------------- #
# 确定性、原子性
# --------------------------------------------------------------------------- #
class DeterminismAndAtomicityTest(BillingTestBase):
    def test_input_line_order_does_not_matter(self):
        log_lines = read_text(os.path.join(EXAMPLES, "meter_log.jsonl")).splitlines()
        attr_lines = read_text(os.path.join(EXAMPLES, "attribution.jsonl")).splitlines()
        rng = random.Random(20251123)
        outputs = []
        for _ in range(3):
            rng.shuffle(log_lines)
            rng.shuffle(attr_lines)
            write_jsonl(self.log, log_lines)
            write_jsonl(self.attr, attr_lines)
            out_dir = os.path.join(self.tmp, f"out-{len(outputs)}")
            settle.run_billing(S1_REPORT, self.log, self.attr, 5,
                               "2025-03-01", "2025-03-02", 4 * H, out_dir)
            outputs.append(out_dir)
        for name in ("summary.jsonl", "known_details.jsonl", "missing_details.jsonl"):
            blobs = [read_bytes(os.path.join(d, "bill-000001", name)) for d in outputs]
            self.assertEqual(blobs[0], blobs[1])
            self.assertEqual(blobs[0], blobs[2])

    def test_failed_run_keeps_previous_result(self):
        write_jsonl(self.log, [upsert("M", "r1", 0, D1, 1), upsert("M", "r2", 0, D1 + H, 2)])
        self.run_billing()
        good = read_bytes(os.path.join(self.out, "bill-000001", "summary.jsonl"))
        self.assertEqual(read_text(os.path.join(self.out, "CURRENT")).strip(), "bill-000001")

        write_jsonl(self.log, [upsert("M", "r1", 0, D1, 1), upsert("M", "r1", 0, D1, 2)])
        with self.assertRaises(settle.LogConflict):
            self.run_billing()
        # 上次成功结果原样保留，没有半份账单
        self.assertEqual(read_text(os.path.join(self.out, "CURRENT")).strip(), "bill-000001")
        self.assertEqual(read_bytes(os.path.join(self.out, "bill-000001", "summary.jsonl")), good)
        self.assertFalse(os.path.exists(os.path.join(self.out, "bill-000002")))
        self.assertEqual(
            [n for n in os.listdir(self.out) if n.startswith((".staging-", ".work-"))],
            [],
        )
        # 之后成功运行正常递增，不覆盖历史
        write_jsonl(self.log, [upsert("M", "r1", 0, D1, 1), upsert("M", "r2", 0, D1 + H, 2)])
        self.run_billing()
        self.assertEqual(read_text(os.path.join(self.out, "CURRENT")).strip(), "bill-000002")
        self.assertEqual(read_bytes(os.path.join(self.out, "bill-000001", "summary.jsonl")), good)

    def test_failed_first_run_leaves_nothing(self):
        write_jsonl(self.log, [upsert("M", "r1", 0, D1, 1), upsert("M", "r1", 0, D1, 2)])
        with self.assertRaises(settle.LogConflict):
            self.run_billing()
        self.assertFalse(os.path.exists(os.path.join(self.out, "CURRENT")))
        self.assertEqual([n for n in os.listdir(self.out) if n.startswith("bill-")], [])


# --------------------------------------------------------------------------- #
# 整数分配与缺测语义
# --------------------------------------------------------------------------- #
class AllocationTest(BillingTestBase):
    def test_floor_and_remainder(self):
        alloc = settle._allocate(3, 8, {("d", "A"): 5, ("d", "B"): 3})
        self.assertEqual(alloc, {("d", "A"): 2, ("d", "B"): 1})

    def test_remainder_tie_goes_to_earlier_date(self):
        alloc = settle._allocate(1, 100, {("2025-01-02", "A"): 50, ("2025-01-01", "B"): 50})
        self.assertEqual(alloc[("2025-01-01", "B")], 1)
        self.assertEqual(alloc[("2025-01-02", "A")], 0)

    def test_remainder_tie_same_date_lexicographic_unattributed_last(self):
        alloc = settle._allocate(1, 100, {("d", "B"): 50, ("d", "A"): 50})
        self.assertEqual(alloc[("d", "A")], 1)
        alloc = settle._allocate(1, 100, {("d", None): 50, ("d", "Z"): 50})
        self.assertEqual(alloc[("d", "Z")], 1)
        self.assertEqual(alloc[("d", None)], 0)

    def test_energy_above_span_and_zero(self):
        alloc = settle._allocate(7, 3, {("d", "A"): 1, ("d", "B"): 1, ("d", "C"): 1})
        self.assertEqual(sum(alloc.values()), 7)
        self.assertEqual(alloc, {("d", "A"): 3, ("d", "B"): 2, ("d", "C"): 2})
        alloc = settle._allocate(0, 10, {("d", "A"): 4, ("d", "B"): 6})
        self.assertEqual(alloc, {("d", "A"): 0, ("d", "B"): 0})

    def test_end_to_end_split_across_midnight(self):
        # 区间 [23:00, 次日01:00)，E=5 → 两天各 3600 秒，余量相同先给较早日期
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1 + 23 * H, 0),
            upsert("M", "r2", 0, D2 + H, 5),
        ])
        write_jsonl(self.attr, [attr("M", "B01", D1, D3)])
        self.run_billing(date_from="2025-03-01", date_to="2025-03-02", max_gap=2 * H)
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(
            [(r["date"], r["wh"]) for r in known],
            [("2025-03-01", 3), ("2025-03-02", 2)],
        )

    def test_same_combo_seconds_summed_before_allocation(self):
        # 归属 B01→空档→B01：同一 (日期,展位) 组合分开出现，先合计秒数
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 0),
            upsert("M", "r2", 0, D1 + 3 * H, 10),   # 3 小时区间，E=10
        ])
        write_jsonl(self.attr, [
            attr("M", "B01", D1, D1 + H),
            attr("M", "B01", D1 + 2 * H, D1 + 3 * H),
        ])
        self.run_billing(max_gap=3 * H)
        _, known, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        # B01 合计 7200 秒、空档 3600 秒：10*7200/10800=6.67→6，10*3600/10800=3.33→3，
        # 余量 0.67>0.33 → B01 补一 → 7 与 3
        self.assertEqual(len(known), 2)
        by_booth = {r["booth"]: r for r in known}
        self.assertEqual(by_booth["B01"]["seconds"], 2 * H)
        self.assertEqual(by_booth["B01"]["wh"], 7)
        self.assertEqual(by_booth[None]["wh"], 3)

    def test_adjacent_missing_merged(self):
        write_jsonl(self.attr, [
            attr("M", "B01", D1, D1 + 10 * H),
            attr("M", "B01", D1 + 10 * H, D1 + 20 * H),
        ])
        self.run_billing()
        _, _, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(missing), 2)
        b01 = [r for r in missing if r["booth"] == "B01"]
        self.assertEqual(len(b01), 1)
        self.assertEqual((b01[0]["start"], b01[0]["end"]), (D1, D1 + 20 * H))

    def test_missing_meter_seconds_count_per_meter(self):
        # 两只表同时缺测同一秒算两表秒
        write_jsonl(self.log, [])
        write_jsonl(self.attr, [attr("M1", "B01", D1, D2), attr("M2", "B01", D1, D2)])
        self.run_billing()
        summary, _, _ = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["missing_meter_seconds"], 2 * 24 * H)

    def test_meter_only_in_attribution_is_fully_missing(self):
        write_jsonl(self.attr, [attr("M", "B02", D1 + 6 * H, D1 + 12 * H)])
        self.run_billing()
        summary, _, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(sum(r["missing_meter_seconds"] for r in summary), 24 * H)
        booths = {(r["booth"]) for r in missing}
        self.assertEqual(booths, {"B02", None})

    def test_missing_attribution_not_stolen_by_post_window_interval(self):
        # 窗口之后还有可结算区间时，窗口内的缺测归属不能被游标掏空
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1 + 10 * H, 0),
            upsert("M", "r2", 0, D1 + 11 * H, 100),      # 窗口内可结算
            upsert("M", "r3", 0, D3 + 10 * H, 100),
            upsert("M", "r4", 0, D3 + 11 * H, 200),      # 完全在窗口之后的区间
        ])
        write_jsonl(self.attr, [attr("M", "B01", D1, D2)])
        self.run_billing()
        _, known, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(len(known), 1)
        self.assertEqual(
            [(r["booth"], r["start"], r["end"]) for r in missing],
            [("B01", D1, D1 + 10 * H), ("B01", D1 + 11 * H, D2)],
        )

    def test_empty_universe(self):
        self.run_billing()
        summary, known, missing = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual((summary, known, missing), ([], [], []))
        self.assertTrue(settle.verify_bill(self.out)["ok"])


# --------------------------------------------------------------------------- #
# 独立核验
# --------------------------------------------------------------------------- #
class VerifyTest(BillingTestBase):
    def setUp(self):
        super().setUp()
        write_jsonl(self.log, [
            upsert("M", "r1", 0, D1, 0),
            upsert("M", "r2", 0, D1 + H, 100),
        ])
        write_jsonl(self.attr, [attr("M", "B01", D1, D2)])
        self.run_billing()
        self.bill = os.path.join(self.out, "bill-000001")

    def _rewrite(self, name, mutate):
        path = os.path.join(self.bill, name)
        rows = read_jsonl(path)
        mutate(rows)
        write_jsonl(path, rows)

    def test_tampered_summary_detected(self):
        self._rewrite("summary.jsonl", lambda rows: rows[0].update(known_wh=999))
        with self.assertRaises(settle.VerifyError):
            settle.verify_bill(self.out)

    def test_tampered_known_detail_detected(self):
        self._rewrite("known_details.jsonl", lambda rows: rows[0].update(wh=12345))
        with self.assertRaises(settle.VerifyError):
            settle.verify_bill(self.out)

    def test_summary_key_without_details_detected(self):
        def mutate(rows):
            rows.append({"date": "2025-03-01", "booth": "B05",
                         "known_wh": 0, "missing_meter_seconds": 0})
            rows.sort(key=lambda r: (r["date"], settle._booth_sort(r["booth"])))
        self._rewrite("summary.jsonl", mutate)
        with self.assertRaises(settle.VerifyError):
            settle.verify_bill(self.out)

    def test_detail_without_summary_detected(self):
        self._rewrite("summary.jsonl", lambda rows: rows.pop())
        with self.assertRaises(settle.VerifyError):
            settle.verify_bill(self.out)


# --------------------------------------------------------------------------- #
# 朴素参照实现（独立逻辑，用于随机对拍）
# --------------------------------------------------------------------------- #
def naive_settle(log_records, attr_records, bound, dfrom, dto, max_gap):
    """逐秒级朴素结算：与引擎独立实现，只共享日期换算与分配规则本身。"""
    W0 = settle.parse_date(dfrom)
    W1 = settle.parse_date(dto) + 86400
    visible = {}
    for rec in log_records:
        if rec["batch"] > bound:
            continue
        key = (rec["meter"], rec["reading"], rec["batch"])
        if key in visible and visible[key] != rec:
            raise settle.LogConflict("conflict")
        visible[key] = rec
    selected = {}
    for rec in visible.values():
        k = (rec["meter"], rec["reading"])
        if k not in selected or rec["batch"] > selected[k]["batch"]:
            selected[k] = rec
    readings: dict = {}
    for rec in selected.values():
        if rec.get("revoke"):
            continue
        readings.setdefault(rec["meter"], []).append(rec)
    for meter, rs in readings.items():
        rs.sort(key=lambda r: (r["time"], r["reading"]))
        for a, b in zip(rs, rs[1:]):
            if a["time"] == b["time"]:
                raise settle.ReadingSameSecond("same second")
            if a["period"] == b["period"] and b["wh"] < a["wh"]:
                raise settle.ReadingRegression("regression")
    attrs: dict = {}
    for rec in attr_records:
        attrs.setdefault(rec["meter"], []).append(rec)
    for meter, ivs in attrs.items():
        ivs.sort(key=lambda r: (r["start"], r["end"], r["booth"]))
        for a, b in zip(ivs, ivs[1:]):
            if b["start"] < a["end"]:
                raise settle.AttributionOverlap("overlap")

    def booth_at(meter, t):
        for iv in attrs.get(meter, []):
            if iv["start"] <= t < iv["end"]:
                return iv["booth"]
        return None

    known, missing = [], []
    for meter in sorted(set(readings) | set(attrs)):
        rs = readings.get(meter, [])
        ivs = attrs.get(meter, [])
        covered = []
        for a, b in zip(rs, rs[1:]):
            if a["period"] != b["period"] or b["time"] - a["time"] > max_gap:
                continue
            t1, t2 = a["time"], b["time"]
            energy = b["wh"] - a["wh"]
            span = t2 - t1
            combos: dict = {}
            for t in range(t1, t2):
                key = (settle.date_of(t), booth_at(meter, t))
                combos[key] = combos.get(key, 0) + 1
            alloc = settle._allocate(energy, span, combos)
            for (d, booth), secs in combos.items():
                if dfrom <= d <= dto:
                    known.append({
                        "meter": meter, "date": d, "booth": booth,
                        "interval_start": t1, "interval_end": t2,
                        "reading_left": a["reading"], "reading_right": b["reading"],
                        "batch_left": a["batch"], "batch_right": b["batch"],
                        "seconds": secs, "wh": alloc[(d, booth)],
                    })
            covered.append((t1, t2))
        segments = []
        cursor = W0
        for t1, t2 in covered:
            lo, hi = max(t1, W0), min(t2, W1)
            if lo < hi:
                if lo > cursor:
                    segments.append((cursor, lo))
                cursor = max(cursor, hi)
        if cursor < W1:
            segments.append((cursor, W1))
        for s, e in segments:
            cuts = {s, e}
            boundary = (s // 86400 + 1) * 86400
            while boundary < e:
                cuts.add(boundary)
                boundary += 86400
            for iv in ivs:
                if s < iv["start"] < e:
                    cuts.add(iv["start"])
                if s < iv["end"] < e:
                    cuts.add(iv["end"])
            points = sorted(cuts)
            pend = None
            for a_, b_ in zip(points, points[1:]):
                date = settle.date_of(a_)
                booth = booth_at(meter, a_)
                if pend and pend[0] == date and pend[1] == booth and pend[3] == a_:
                    pend[3] = b_
                else:
                    if pend:
                        missing.append({"meter": meter, "date": pend[0], "booth": pend[1],
                                        "start": pend[2], "end": pend[3],
                                        "seconds": pend[3] - pend[2]})
                    pend = [date, booth, a_, b_]
            if pend:
                missing.append({"meter": meter, "date": pend[0], "booth": pend[1],
                                "start": pend[2], "end": pend[3],
                                "seconds": pend[3] - pend[2]})
    totals: dict = {}
    for rec in known:
        totals.setdefault((rec["date"], rec["booth"]), [0, 0])[0] += rec["wh"]
    for rec in missing:
        totals.setdefault((rec["date"], rec["booth"]), [0, 0])[1] += rec["seconds"]
    summary = [{"date": d, "booth": b, "known_wh": v[0], "missing_meter_seconds": v[1]}
               for (d, b), v in totals.items()]
    summary.sort(key=lambda r: (r["date"], settle._booth_sort(r["booth"])))
    known.sort(key=settle._known_sort_key)
    missing.sort(key=settle._missing_sort_key)
    return summary, known, missing


def random_case(rng: random.Random):
    """构造一份合法时间线的随机输入（修订/撤销/重复/越界批次都覆盖）。"""
    booths = ["B01", "B02", "B03"]
    bound = rng.choice([0, 1, 2, 3])
    max_gap = rng.choice([600, 1800, 3600])
    log_records, attr_records = [], []
    for m in range(rng.randint(1, 5)):
        meter = f"M{m}"
        t = D1 - rng.randint(0, 4 * H)
        for _ in range(rng.randint(0, 3)):
            length = rng.randint(H, 20000)
            attr_records.append(attr(meter, rng.choice(booths), t, t + length))
            t += length + rng.randint(0, 2 * H)
        n = rng.randint(0, 10)
        t = D1 - rng.randint(0, 4 * H)
        period = rng.choice(["P", "Q"])
        wh = rng.randint(0, 1000)
        for i in range(n):
            t += rng.randint(60, 5000)
            if rng.random() < 0.2:
                period = "Q" if period == "P" else "P"   # 换周期（累计值不重置，
            wh += rng.randint(0, 500)                    # 保证撤销中间读数后仍不倒退）
            reading = f"r{i}"
            final_batch = rng.randint(0, bound) if bound else 0
            if rng.random() < 0.15:
                # 先建后撤：最新可见是撤销
                log_records.append(upsert(meter, reading, final_batch, t, wh, period))
                if final_batch + 1 <= bound:
                    log_records.append(revoke(meter, reading, final_batch + 1))
                else:
                    log_records.append(upsert(meter, reading, final_batch, t, wh, period))
                continue
            # 被取代的旧修订（内容随意，不影响结果）
            if final_batch > 0 and rng.random() < 0.5:
                old_batch = rng.randint(0, final_batch - 1)
                log_records.append(upsert(meter, reading, old_batch,
                                          t - rng.randint(0, 500), rng.randint(0, 2000), period))
            log_records.append(upsert(meter, reading, final_batch, t, wh, period))
            if rng.random() < 0.3:   # 完全相同的重复行
                log_records.append(upsert(meter, reading, final_batch, t, wh, period))
            if rng.random() < 0.3:   # 越界批次，忽略
                log_records.append(upsert(meter, reading, bound + 1 + rng.randint(0, 3),
                                          t, rng.randint(0, 9999), period))
    rng.shuffle(log_records)
    rng.shuffle(attr_records)
    return log_records, attr_records, bound, max_gap


class NaiveCrossCheckTest(BillingTestBase):
    def test_random_cases_match_naive(self):
        for seed in range(30):
            rng = random.Random(seed)
            log_records, attr_records, bound, max_gap = random_case(rng)
            write_jsonl(self.log, log_records)
            write_jsonl(self.attr, attr_records)
            expected = naive_settle(log_records, attr_records, bound,
                                    "2025-03-01", "2025-03-02", max_gap)
            out_dir = os.path.join(self.tmp, f"out-{seed}")
            settle.run_billing(self.report, self.log, self.attr, bound,
                               "2025-03-01", "2025-03-02", max_gap, out_dir,
                               run_size=7)   # 强制外排多轮归并
            got = self.bill_files(os.path.join(out_dir, "bill-000001"))
            self.assertEqual(got[0], expected[0], f"seed={seed} summary")
            self.assertEqual(got[1], expected[1], f"seed={seed} known")
            self.assertEqual(got[2], expected[2], f"seed={seed} missing")
            self.assertTrue(settle.verify_bill(out_dir)["ok"], f"seed={seed} verify")

    def test_stress_external_sort_multi_pass(self):
        rng = random.Random(777)
        log_records = []
        for m in range(40):
            meter = f"M{m:02d}"
            t = D1 - 2 * H
            wh = 0
            for i in range(75):
                t += rng.randint(300, 600)
                wh += rng.randint(0, 50)
                log_records.append(upsert(meter, f"r{i}", 0, t, wh))
                if rng.random() < 0.5:
                    log_records.append(upsert(meter, f"r{i}", 0, t, wh))  # 重复行
        rng.shuffle(log_records)
        write_jsonl(self.log, log_records)
        write_jsonl(self.attr, [])
        expected = naive_settle(log_records, [], 0, "2025-03-01", "2025-03-02", 3600)
        settle.run_billing(self.report, self.log, self.attr, 0,
                           "2025-03-01", "2025-03-02", 3600, self.out, run_size=50)
        got = self.bill_files(os.path.join(self.out, "bill-000001"))
        self.assertEqual(got[0], expected[0])
        self.assertEqual(got[1], expected[1])
        self.assertEqual(got[2], expected[2])
        self.assertTrue(settle.verify_bill(self.out)["ok"])


# --------------------------------------------------------------------------- #
# CLI 冒烟（隔离模式子进程）
# --------------------------------------------------------------------------- #
class CliTest(BillingTestBase):
    def test_cli_run_show_verify_and_conflict_exit_code(self):
        bill = os.path.join(ROOT, "scripts", "bill")
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, "-I", bill, "run",
             "--report", S1_REPORT,
             "--meter-log", os.path.join(EXAMPLES, "meter_log.jsonl"),
             "--attribution", os.path.join(EXAMPLES, "attribution.jsonl"),
             "--batch-bound", "5", "--from", "2025-03-01", "--to", "2025-03-02",
             "--max-gap-seconds", "14400", "--out", self.out],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bill"], "bill-000001")

        proc = subprocess.run([sys.executable, "-I", bill, "verify", self.out],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

        proc = subprocess.run([sys.executable, "-I", bill, "show", "--out", self.out],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["bill"], "bill-000001")

        proc = subprocess.run(
            [sys.executable, "-I", bill, "run",
             "--report", S1_REPORT,
             "--meter-log", os.path.join(EXAMPLES, "meter_log_conflict.jsonl"),
             "--attribution", os.path.join(EXAMPLES, "attribution.jsonl"),
             "--batch-bound", "5", "--from", "2025-03-01", "--to", "2025-03-02",
             "--max-gap-seconds", "14400", "--out", self.out],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "log_conflict")
        self.assertEqual(read_text(os.path.join(self.out, "CURRENT")).strip(), "bill-000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
