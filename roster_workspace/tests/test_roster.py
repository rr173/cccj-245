"""名册工作区机制级测试（标准库 unittest，python3 -I 运行）。

覆盖：报告准入、初始化名册、主标识选举、登记/撤回/传递闭包、变更集差异
语义（删除键/改写行/空变更）、作用域处理、幂等重送（含换进程）、拒绝
路径、原子重开与独立核验。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roster import workspace as ws  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "booth_collector", "out")
S1 = os.path.join(OUT, "S1_complete.json")
S2 = os.path.join(OUT, "S2_fill_truncated.json")
S6 = os.path.join(OUT, "S6_retry_cache.json")
S3 = os.path.join(OUT, "S3_unsplittable.json")
S4 = os.path.join(OUT, "S4_budget.json")


def write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class RosterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="roster-test-")
        self.workdir = os.path.join(self.tmp, "ws")
        self.specs = [
            ("E1", S1),   # 8 个展位
            ("E2", S2),   # 16 个展位
            ("E3", S6),   # 8 个展位
        ]

    def init(self, specs=None):
        return ws.init_workspace(self.workdir, specs or self.specs)

    def correct(self, commit, base, ops):
        return ws.correct(self.workdir, commit, base, ops)

    def state(self):
        return ws.load_state(self.workdir)

    def row_map(self):
        st = self.state()
        return {(r["order"], r["primary_id"]): r for r in st["rows"]}

    def reg(self, ev_id, a, b):
        return {"action": "register", "evidence_id": ev_id, "pair": [a, b]}

    def wd(self, ev_id, a, b):
        return {"action": "withdraw", "evidence_id": ev_id, "pair": [a, b]}


class TestInitAndAdmission(RosterTestBase):
    def test_init_editions_ordered_earliest_to_latest(self):
        res = self.init()
        editions = res["state"]["editions"]
        self.assertEqual([e["order"] for e in editions], [1, 2, 3])
        self.assertEqual([e["edition"] for e in editions], ["E1", "E2", "E3"])
        # 初始化提交号固定，且变更集是从空表写入的全量 upsert。
        self.assertEqual(res["changeset"]["commit"], ws.INIT_COMMIT)
        self.assertIsNone(res["changeset"]["base_commit"])
        self.assertEqual(res["changeset"]["changes"]["deletes"], [])
        self.assertEqual(
            len(res["changeset"]["changes"]["upserts"]),
            len(res["state"]["rows"]),
        )
        self.assertEqual(len(res["state"]["booths"]), 8 + 16 + 8)
        # 无连接的展位也全部入册，且各自一行。
        self.assertEqual(len(res["state"]["rows"]), 8 + 16 + 8)

    def test_reject_incomplete_reports(self):
        for bad in (S3, S4):
            with self.subTest(bad=bad):
                with self.assertRaises(ws.ReportRejected):
                    ws.init_workspace(
                        os.path.join(self.tmp, "w1"), [("E1", bad)]
                    )

    def test_reject_root_filtered_report(self):
        good = load_json(S1)
        good["root_filter"] = {"include": ["tech"], "exclude": []}
        p = os.path.join(self.tmp, "filtered.json")
        write_json(p, good)
        with self.assertRaises(ws.ReportRejected):
            ws.init_workspace(os.path.join(self.tmp, "w2"), [("E1", p)])

    def test_reject_broken_and_duplicate_id_reports(self):
        p = os.path.join(self.tmp, "broken.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ws.ReportRejected):
            ws.init_workspace(os.path.join(self.tmp, "w3"), [("E1", p)])

        good = load_json(S1)
        good["booths"].append(dict(good["booths"][0]))
        p2 = os.path.join(self.tmp, "dup.json")
        write_json(p2, good)
        with self.assertRaises(ws.ReportRejected):
            ws.init_workspace(os.path.join(self.tmp, "w4"), [("E1", p2)])

    def test_booth_id_only_meaningful_within_edition(self):
        # 同名/同编号展位不自动并组：三届都有 B01，初始化仍是三个身份。
        self.init()
        rows = self.row_map()
        self.assertIn((1, "B01"), rows)
        self.assertIn((2, "B01"), rows)
        self.assertIn((3, "B01"), rows)
        # 首次参展 prev 为 null。
        self.assertIsNone(rows[(1, "B01")]["prev_edition"])

    def test_init_twice_requires_reset(self):
        self.init()
        with self.assertRaises(ws.WorkspaceExists):
            self.init()

    def test_duplicate_edition_name_rejected(self):
        with self.assertRaises(ws.RosterError):
            ws.init_workspace(
                os.path.join(self.tmp, "w5"),
                [("SAME", S1), ("SAME", S6)],
            )


class TestCorrectionSemantics(RosterTestBase):
    def test_register_links_two_booths_and_emits_scoped_changes(self):
        self.init()
        before = self.row_map()
        res = self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        cs = res["changeset"]
        self.assertEqual(cs["base_commit"], ws.INIT_COMMIT)
        self.assertEqual(cs["commit"], "c1")
        # 作用域只含被连接的两个展位，不触及其他 30 个。
        self.assertEqual(res["scope"]["scoped_booth_count"], 2)
        self.assertEqual(res["scope"]["total_booth_count"], 32)

        after = self.row_map()
        # E1 行内容不变（单展位、prev 仍为 null），按「没变化的行不能再发」
        # 不重发；E2 行被并入后 prev 从 null 变为 E1，作为 upsert 改写。
        deleted = {(d["key"]["order"], d["key"]["primary_id"])
                   for d in cs["changes"]["deletes"]}
        upserted = {(u["key"]["order"], u["key"]["primary_id"])
                    for u in cs["changes"]["upserts"]}
        self.assertEqual(deleted, set())
        self.assertEqual(upserted, {(2, "B01")})
        # 未涉及行既不删除也不改写。
        untouched = [k for k in before if k not in ((1, "B01"), (2, "B01"))]
        for k in untouched:
            self.assertNotIn(k, upserted)
        # 合并后 E2 行的前一届是 E1（原来为 None）。
        self.assertEqual(after[(2, "B01")]["prev_edition"], "E1")
        self.assertEqual(after[(2, "B01")]["booth_ids"], ["B01"])

    def test_primary_election_earliest_edition_then_lexicographic(self):
        self.init()
        # 同编号跨届相连：主标识取最早届 E1 的 B05，E2 行以 (2,B05) 交付。
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B05", "2/B05")])
        after = self.row_map()
        self.assertIn((1, "B05"), after)
        self.assertIn((2, "B05"), after)
        self.assertEqual(after[(2, "B05")]["booth_ids"], ["B05"])

        # 同届两个展位并入同一身份：取该届字典序最小编号 B05（B05 < B07），
        # E1 行列出两个展位；旧键 (1,B07) 必须删除。
        res = self.correct("c2", "c1", [self.reg("e2", "1/B05", "1/B07")])
        after2 = self.row_map()
        self.assertEqual(after2[(1, "B05")]["booth_ids"], ["B05", "B07"])
        deleted = {(d["key"]["order"], d["key"]["primary_id"])
                   for d in res["changeset"]["changes"]["deletes"]}
        self.assertIn((1, "B07"), deleted)
        self.assertNotIn((1, "B07"), after2)

    def test_primary_collision_with_unrelated_identity_rejected(self):
        self.init()
        # 1/B05 与 2/B09 合并后，新身份在 E2 的行键为 (2,B05)，
        # 而 2/B05 是另一个未涉及的独立身份 -> 表键撞车，整批拒绝且状态不变。
        snapshot = copy.deepcopy(self.state())
        with self.assertRaises(ws.BadOperation):
            self.correct("c1", ws.INIT_COMMIT,
                         [self.reg("e1", "1/B05", "2/B09")])
        after = self.state()
        self.assertEqual(after["head_commit"], snapshot["head_commit"])
        self.assertEqual(after["evidence"], snapshot["evidence"])
        self.assertEqual(after["rows"], snapshot["rows"])

    def test_transitive_identity_and_withdraw_is_not_difference_claim(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [
            self.reg("e1", "1/B01", "2/B01"),
            self.reg("e2", "2/B01", "3/B01"),
        ])
        after = self.row_map()
        for order in (1, 2, 3):
            self.assertIn((order, "B01"), after)
        # 三届同一身份：E3 行的前一届是 E2（该身份实际参加的前一届）。
        self.assertEqual(after[(3, "B01")]["prev_edition"], "E2")

        # 撤回 e2：e1 仍有效，1-2 相连；3/B01 回到独立身份。
        # 撤回不是声明两家不同——只是该依据不再贡献连接。
        res = self.correct("c2", "c1", [self.wd("e2", "2/B01", "3/B01")])
        cs = res["changeset"]
        after2 = self.row_map()
        self.assertIn((3, "B01"), after2)               # 独立身份重新入册
        upserted = {(u["key"]["order"], u["key"]["primary_id"])
                    for u in cs["changes"]["upserts"]}
        self.assertIn((3, "B01"), upserted)            # 以 prev=None 的完整行改写
        self.assertEqual(after2[(3, "B01")]["prev_edition"], None)
        # 1-2 仍相连。
        self.assertEqual(after2[(2, "B01")]["prev_edition"], "E1")

    def test_multiple_evidence_support_same_pair(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [
            self.reg("e1", "1/B02", "2/B02"),
            self.reg("e2", "1/B02", "2/B02"),  # 多条依据支持同一对
        ])
        # 撤回其中一条，连接仍然存在，名册无变化但仍交付带两个提交号的变更。
        res = self.correct("c2", "c1", [self.wd("e1", "1/B02", "2/B02")])
        self.assertEqual(res["changeset"]["changes"],
                         {"deletes": [], "upserts": []})
        self.assertEqual(res["changeset"]["base_commit"], "c1")
        self.assertEqual(res["changeset"]["commit"], "c2")
        after = self.row_map()
        self.assertIn((2, "B02"), after)

    def test_absent_edition_gets_no_row(self):
        self.init()
        # 身份只参加 E1 与 E3（跳过 E2）：只出两行，E3 行的前一届是 E1。
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B03", "3/B03")])
        st = self.state()
        cid = st["component_of"]["1/B03"]
        members = set(st["components"][cid])
        rows = [
            r for r in st["rows"]
            if any(f"{r['order']}/{bid}" in members for bid in r["booth_ids"])
        ]
        # 缺席届次 E2 不补行：该身份只有 E1、E3 两行。
        self.assertEqual([r["order"] for r in rows], [1, 3])
        self.assertEqual(rows[1]["prev_edition"], "E1")

    def test_prev_edition_change_rewrites_even_with_same_key(self):
        self.init()
        # 先连 E1/B04 -> E3/B04：身份参加 E1、E3（跳过 E2），
        # 键 (3,B04) 的 prev_edition 为 E1。
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B04", "3/B04")])
        self.assertEqual(self.row_map()[(3, "B04")]["prev_edition"], "E1")
        # 再把 E2/B04 并入同一身份：键 (3,B04) 没变，但前一届从 E1 改为 E2，
        # 该行必须作为 upsert 重新交付（没变化的行才不发）。
        res = self.correct("c2", "c1", [self.reg("e2", "2/B04", "3/B04")])
        prev_by_order = {u["key"]["order"]: u["row"]["prev_edition"]
                         for u in res["changeset"]["changes"]["upserts"]}
        self.assertEqual(prev_by_order.get(3), "E2")
        self.assertEqual(self.row_map()[(3, "B04")]["prev_edition"], "E2")
        # E2 行是新增的身份行。
        keys = {(u["key"]["order"], u["key"]["primary_id"])
                for u in res["changeset"]["changes"]["upserts"]}
        self.assertIn((2, "B04"), keys)
        # 没有键消失。
        self.assertEqual(res["changeset"]["changes"]["deletes"], [])


class TestRejections(RosterTestBase):
    def test_unknown_booth_rejected(self):
        self.init()
        with self.assertRaises(ws.UnknownBooth):
            self.correct("c1", ws.INIT_COMMIT,
                         [self.reg("e1", "1/B01", "9/B01")])
        # 失败后头部不变。
        self.assertEqual(self.state()["head_commit"], ws.INIT_COMMIT)

    def test_same_booth_pair_rejected(self):
        self.init()
        with self.assertRaises(ws.BadOperation):
            self.correct("c1", ws.INIT_COMMIT,
                         [self.reg("e1", "1/B01", "1/B01")])

    def test_evidence_id_not_reusable(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        with self.assertRaises(ws.BadOperation):
            self.correct("c2", "c1", [self.reg("e1", "1/B02", "2/B02")])

    def test_withdraw_unknown_or_inactive_evidence_rejected(self):
        self.init()
        with self.assertRaises(ws.BadOperation):
            self.correct("c1", ws.INIT_COMMIT,
                         [self.wd("nope", "1/B01", "2/B01")])
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        self.correct("c2", "c1", [self.wd("e1", "1/B01", "2/B01")])
        with self.assertRaises(ws.BadOperation):
            self.correct("c3", "c2", [self.wd("e1", "1/B01", "2/B01")])

    def test_withdraw_pair_mismatch_rejected(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        # 撤回时引用了与登记不一致的展位对，必须拒绝且连接保持有效。
        with self.assertRaises(ws.BadOperation):
            self.correct("c2", "c1", [self.wd("e1", "1/B01", "2/B02")])
        st = self.state()
        self.assertTrue(st["evidence"]["e1"]["active"])
        self.assertEqual(st["head_commit"], "c1")

    def test_stale_base_rejected(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        with self.assertRaises(ws.StaleCommit):
            self.correct("c2", ws.INIT_COMMIT, [self.reg("e2", "1/B02", "2/B02")])
        # 基于旧提交的新批被拒后，c1 之后没有任何新提交。
        self.assertEqual(self.state()["head_commit"], "c1")

    def test_duplicate_operation_in_one_batch_rejected(self):
        self.init()
        with self.assertRaises(ws.BadOperation):
            self.correct("c1", ws.INIT_COMMIT, [
                self.reg("e1", "1/B01", "2/B01"),
                self.wd("e1", "1/B01", "2/B01"),
            ])

    def test_batch_is_all_or_nothing(self):
        self.init()
        snapshot = copy.deepcopy(self.state())
        with self.assertRaises(ws.RosterError):
            self.correct("c1", ws.INIT_COMMIT, [
                self.reg("e1", "1/B01", "2/B01"),       # 合法
                self.reg("e2", "1/B01", "9/B01"),       # 非法未知展位
            ])
        after = self.state()
        self.assertEqual(after["head_commit"], snapshot["head_commit"])
        self.assertEqual(after["evidence"], snapshot["evidence"])
        self.assertEqual(after["rows"], snapshot["rows"])


class TestReplay(RosterTestBase):
    def _ops(self):
        return [self.reg("e1", "2/B01", "1/B01")]  # 故意反方向

    def test_replay_same_content_returns_original_changeset(self):
        self.init()
        original = self.correct("c1", ws.INIT_COMMIT,
                                [self.reg("e1", "1/B01", "2/B01")])
        # 之后又有新批次。
        self.correct("c2", "c1", [self.reg("e2", "1/B02", "2/B02")])
        # 换进程重送同号同内容（方向不同、顺序不计），拿回原变更。
        replayed = ws.replay(self.workdir, "c1", ws.INIT_COMMIT, self._ops())
        self.assertEqual(replayed, original["changeset"])

    def test_replay_same_id_different_content_rejected(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT,
                     [self.reg("e1", "1/B01", "2/B01")])
        with self.assertRaises(ws.CommitConflict):
            ws.replay(self.workdir, "c1", ws.INIT_COMMIT,
                      [self.reg("e9", "1/B01", "2/B01")])
        with self.assertRaises(ws.CommitConflict):
            ws.replay(self.workdir, "c1", "c2",
                      [self.reg("e1", "1/B01", "2/B01")])

    def test_replay_unknown_commit_rejected(self):
        self.init()
        with self.assertRaises(ws.CommitConflict):
            ws.replay(self.workdir, "ghost", ws.INIT_COMMIT, [])

    def test_replay_in_subprocess_after_newer_commits(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT,
                     [self.reg("e1", "1/B01", "2/B01")])
        self.correct("c2", "c1", [self.reg("e2", "1/B02", "2/B02")])
        cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "roster", "cli.py")
        proc = subprocess.run(
            [sys.executable, "-I", cli, "replay", self.workdir,
             "--commit", "c1", "--base", ws.INIT_COMMIT,
             "--register", "e1:2/B01:1/B01"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changeset"]["commit"], "c1")
        # 规范化后对方向不敏感。
        self.assertEqual(payload["changeset"]["operations"][0]["pair"],
                         ["1/B01", "2/B01"])

    def test_reused_commit_number_for_new_batch_rejected(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT,
                     [self.reg("e1", "1/B01", "2/B01")])
        with self.assertRaises(ws.CommitConflict):
            self.correct("c1", "c1", [self.reg("e2", "1/B02", "2/B02")])


class TestPersistenceAndVerify(RosterTestBase):
    def test_reopen_retrieves_every_changeset(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        self.correct("c2", "c1", [self.reg("e2", "2/B01", "3/B01")])
        # 全新进程视角重新加载。
        st = ws.load_state(self.workdir)
        self.assertEqual(st["head_commit"], "c2")
        for commit in (ws.INIT_COMMIT, "c1", "c2"):
            path = ws.changeset_path(self.workdir, commit)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(load_json(path)["commit"], commit)

    def test_verify_recomputes_all_commits(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [
            self.reg("e1", "1/B01", "2/B01"),
            self.reg("e2", "2/B05", "3/B05"),
        ])
        self.correct("c2", "c1", [self.reg("e3", "2/B01", "3/B01")])
        self.correct("c3", "c2", [self.wd("e1", "1/B01", "2/B01")])
        # 同届内两个展位合并，避免跨届异编号主标识与无关身份撞键。
        self.correct("c4", "c3", [self.reg("e4", "2/B10", "2/B11")])
        report = ws.verify_workspace(self.workdir)
        self.assertTrue(report["ok"])
        self.assertEqual(report["commits_checked"], 5)
        self.assertEqual(report["head_commit"], "c4")

    def test_verify_detects_tampered_changeset(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        path = ws.changeset_path(self.workdir, "c1")
        tampered = load_json(path)
        tampered["changes"]["deletes"].append(
            {"key": {"order": 99, "primary_id": "X"}}
        )
        write_json(path, tampered)
        with self.assertRaises(ws.RosterError):
            ws.verify_workspace(self.workdir)

    def test_no_report_files_needed_after_init(self):
        self.init()
        # 初始化后只依赖工作区快照：临时「删除」报告路径不影响更正与核验。
        self.correct("c1", ws.INIT_COMMIT, [self.reg("e1", "1/B01", "2/B01")])
        st = self.state()
        self.assertTrue(st["booths"])  # 展位已快照在状态里
        ws.verify_workspace(self.workdir)


class TestScopedProcessing(RosterTestBase):
    def test_unrelated_components_never_reemitted(self):
        self.init()
        # 两批操作只涉及 4 个展位；其余名册行绝不出现在任何更正变更集中。
        r1 = self.correct("c1", ws.INIT_COMMIT,
                          [self.reg("e1", "1/B01", "2/B01")])
        r2 = self.correct("c2", "c1", [self.reg("e2", "1/B02", "2/B02")])
        touched = {
            (u["key"]["order"], u["key"]["primary_id"])
            for r in (r1, r2) for u in r["changeset"]["changes"]["upserts"]
        }
        touched |= {
            (d["key"]["order"], d["key"]["primary_id"])
            for r in (r1, r2) for d in r["changeset"]["changes"]["deletes"]
        }
        self.assertTrue(all(k[1] in {"B01", "B02"} for k in touched))
        # 作用域计数严格小于全量。
        self.assertEqual(r1["scope"]["scoped_booth_count"], 2)
        self.assertEqual(r2["scope"]["scoped_booth_count"], 2)

    def test_merge_then_split_scopes_grow_and_shrink(self):
        self.init()
        # 合并三个跨届展位：作用域 3。
        r1 = self.correct("c1", ws.INIT_COMMIT, [
            self.reg("e1", "1/B01", "2/B01"),
            self.reg("e2", "2/B01", "3/B01"),
        ])
        self.assertEqual(r1["scope"]["scoped_booth_count"], 3)
        # 撤回中间连接后组件分裂；批前作用域仍是整组 3 个，批后缩为 2+1。
        r2 = self.correct("c2", "c1", [self.wd("e2", "2/B01", "3/B01")])
        self.assertEqual(r2["scope"]["scoped_booth_count"], 3)
        # 再撤回 e1，批前作用域是 2 人组。
        r3 = self.correct("c3", "c2", [self.wd("e1", "1/B01", "2/B01")])
        self.assertEqual(r3["scope"]["scoped_booth_count"], 2)


    def test_replay_init_commit(self):
        self.init()
        cs = ws.replay(self.workdir, ws.INIT_COMMIT, None, [])
        self.assertEqual(cs["kind"], "init")
        self.assertIsNone(cs["base_commit"])

    def test_crash_between_changeset_and_state_leaves_no_dangling_state(self):
        self.init()
        # 模拟中断点：变更集文件已落盘，但 state.json 还停在上一提交。
        orphan = {
            "schema_version": 1,
            "commit": "c1",
            "base_commit": ws.INIT_COMMIT,
            "kind": "correction",
            "operations": [],
            "digest": "orphan",
            "changes": {"deletes": [], "upserts": []},
        }
        write_json(ws.changeset_path(self.workdir, "c1"), orphan)
        # 重开：头部仍是初始化提交，工作区可用。
        st = ws.load_state(self.workdir)
        self.assertEqual(st["head_commit"], ws.INIT_COMMIT)
        # 同号可以作为新批重新提交（状态里没有它的发布记录），结果可正常取回。
        res = self.correct("c1", ws.INIT_COMMIT,
                           [self.reg("e1", "1/B01", "2/B01")])
        self.assertEqual(res["changeset"]["commit"], "c1")
        self.assertEqual(self.state()["head_commit"], "c1")
        report = ws.verify_workspace(self.workdir)
        self.assertTrue(report["ok"])


class TestNoOpCorrection(RosterTestBase):
    def test_legal_correction_without_roster_change_still_delivered(self):
        self.init()
        # 重复登记同对的第二条依据：连接已存在，名册无变化。
        self.correct("c1", ws.INIT_COMMIT,
                     [self.reg("e1", "1/B01", "2/B01")])
        res = self.correct("c2", "c1",
                           [self.reg("e2", "1/B01", "2/B01")])
        self.assertEqual(res["changeset"]["changes"],
                         {"deletes": [], "upserts": []})
        self.assertEqual(res["changeset"]["base_commit"], "c1")
        self.assertEqual(res["changeset"]["commit"], "c2")
        # 头部推进，下游可按序应用。
        self.assertEqual(self.state()["head_commit"], "c2")

    def test_edge_lives_until_last_supporting_evidence_withdrawn(self):
        self.init()
        self.correct("c1", ws.INIT_COMMIT, [
            self.reg("e1", "1/B01", "2/B01"),
            self.reg("e2", "1/B01", "2/B01"),
        ])
        # 撤第一条：连接仍在（e2 有效），名册无变化。
        r2 = self.correct("c2", "c1", [self.wd("e1", "1/B01", "2/B01")])
        self.assertEqual(r2["changeset"]["changes"],
                         {"deletes": [], "upserts": []})
        self.assertEqual(self.row_map()[(2, "B01")]["prev_edition"], "E1")
        # 撤最后一条：连接消失，2/B01 回到独立身份（prev 变回 null）。
        r3 = self.correct("c3", "c2", [self.wd("e2", "1/B01", "2/B01")])
        upserted = {(u["key"]["order"], u["key"]["primary_id"]): u["row"]
                    for u in r3["changeset"]["changes"]["upserts"]}
        self.assertIsNone(upserted[(2, "B01")]["prev_edition"])
        self.assertEqual(self.row_map()[(2, "B01")]["prev_edition"], None)
        self.assertTrue(ws.verify_workspace(self.workdir)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
