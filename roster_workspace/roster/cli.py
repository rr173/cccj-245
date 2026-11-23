"""名册工作区命令行入口。

用法示例：
  python3 -I -m roster.cli init ./ws \
      --edition 2024春=../booth_collector/out/S1_complete.json \
      --edition 2024秋=../booth_collector/out/S2_fill_truncated.json \
      --edition 2025春=../booth_collector/out/S6_retry_cache.json

  python3 -I -m roster.cli correct ./ws --commit c1 --base commit-0 \
      --register e1:1/B01:2/B01 --register e2:2/B03:3/B03

  python3 -I -m roster.cli replay ./ws --commit c1 --base commit-0 \
      --register e1:1/B01:2/B01
  python3 -I -m roster.cli changes ./ws --commit c1
  python3 -I -m roster.cli show ./ws
  python3 -I -m roster.cli log ./ws
  python3 -I -m roster.cli verify ./ws

更正操作也可通过 --ops ops.json 批量给出，文件形如：
  {"operations": [
    {"action": "register", "evidence_id": "e1", "pair": ["1/B01", "2/B01"]},
    {"action": "withdraw", "evidence_id": "e2", "pair": ["2/B03", "3/B03"]}
  ]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 同时支持「python -m roster.cli」与「python -I /abs/path/roster/cli.py」：
# 隔离模式（-I 隐含 -P）不会自动把脚本目录加入 sys.path，需要显式回退。
try:  # pragma: no cover - 两种入口的导入分支
    from . import workspace as ws
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from roster import workspace as ws


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _load_ops_file(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        ops = data.get("operations")
    else:
        ops = data
    if not isinstance(ops, list):
        raise ws.BadOperation("--ops 文件必须包含 operations 数组")
    return ops


def _parse_inline(token: str, action: str) -> dict:
    parts = token.split(":")
    if len(parts) != 3:
        raise ws.BadOperation(
            f"{action} 参数格式应为 '依据编号:届次/展位:届次/展位'，收到 {token!r}"
        )
    ev_id, a, b = parts
    return {"action": action, "evidence_id": ev_id, "pair": [a, b]}


def _collect_ops(args: argparse.Namespace) -> list[dict]:
    ops: list[dict] = []
    for token in getattr(args, "register", []) or []:
        ops.append(_parse_inline(token, "register"))
    for token in getattr(args, "withdraw", []) or []:
        ops.append(_parse_inline(token, "withdraw"))
    if getattr(args, "ops", None):
        ops.extend(_load_ops_file(args.ops))
    return ops


def cmd_init(args: argparse.Namespace) -> int:
    specs: list[tuple[str, str]] = []
    for item in args.edition:
        if "=" not in item:
            raise ws.BadOperation(
                f"--edition 格式应为 '届次名=报告路径'，收到 {item!r}"
            )
        name, _, path = item.partition("=")
        specs.append((name, path))
    result = ws.init_workspace(args.workdir, specs, reset=args.reset)
    state = result["state"]
    _print_json(
        {
            "ok": True,
            "workdir": os.path.abspath(args.workdir),
            "head_commit": state["head_commit"],
            "editions": [
                {"order": e["order"], "edition": e["edition"],
                 "booth_count": e["booth_count"],
                 "catalog_version": e["catalog_version"]}
                for e in state["editions"]
            ],
            "booths_total": len(state["booths"]),
            "roster_rows": len(state["rows"]),
            "changeset": result["changeset"],
        }
    )
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    ops = _collect_ops(args)
    result = ws.correct(args.workdir, args.commit, args.base, ops)
    _print_json({"ok": True, "scope": result["scope"],
                 "changeset": result["changeset"]})
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    ops = _collect_ops(args)
    base = None if args.base in (None, "null") else args.base
    changeset = ws.replay(args.workdir, args.commit, base, ops)
    _print_json({"ok": True, "replayed": True, "changeset": changeset})
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    path = ws.changeset_path(args.workdir, args.commit)
    if not os.path.exists(path):
        raise ws.RosterError(f"找不到提交 {args.commit} 的变更集：{path}")
    with open(path, "r", encoding="utf-8") as fh:
        _print_json(json.load(fh))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    state = ws.load_state(args.workdir)
    _print_json(
        {
            "head_commit": state["head_commit"],
            "editions": state["editions"],
            "evidence": sorted(
                state["evidence"].values(),
                key=lambda e: e["evidence_id"],
            ),
            "roster_rows": state["rows"],
        }
    )
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    state = ws.load_state(args.workdir)
    _print_json(
        {
            "head_commit": state["head_commit"],
            "commits": [
                {
                    "commit": c["commit"],
                    "base_commit": c["base_commit"],
                    "kind": c["kind"],
                    "digest": c["digest"],
                    "changeset": c["changeset"],
                }
                for c in state["commits"]
            ],
        }
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _print_json(ws.verify_workspace(args.workdir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roster.cli",
        description="跨届复展名册本地工作区：报告准入、身份更正、变更集交付",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="从多届已验收报告初始化工作区")
    p_init.add_argument("workdir")
    p_init.add_argument(
        "--edition", action="append", required=True,
        help="届次名=报告路径，可重复；给出顺序即从早到晚的届次顺序",
    )
    p_init.add_argument("--reset", action="store_true",
                        help="允许重建已存在的工作区")
    p_init.set_defaults(func=cmd_init)

    p_corr = sub.add_parser("correct", help="提交一批身份更正")
    p_corr.add_argument("workdir")
    p_corr.add_argument("--commit", required=True, help="本批新提交号")
    p_corr.add_argument("--base", required=True, help="预期上一提交号")
    p_corr.add_argument("--register", action="append",
                        help="登记依据，格式 依据编号:届次/展位:届次/展位，可重复")
    p_corr.add_argument("--withdraw", action="append",
                        help="撤回依据，格式 依据编号:届次/展位:届次/展位，可重复")
    p_corr.add_argument("--ops", help="从 JSON 文件读取 operations 数组")
    p_corr.set_defaults(func=cmd_correct)

    p_re = sub.add_parser("replay", help="按提交号重送，取回原变更集")
    p_re.add_argument("workdir")
    p_re.add_argument("--commit", required=True)
    p_re.add_argument("--base", required=True)
    p_re.add_argument("--register", action="append")
    p_re.add_argument("--withdraw", action="append")
    p_re.add_argument("--ops")
    p_re.set_defaults(func=cmd_replay)

    p_ch = sub.add_parser("changes", help="查看某个提交的变更集")
    p_ch.add_argument("workdir")
    p_ch.add_argument("--commit", required=True)
    p_ch.set_defaults(func=cmd_changes)

    p_show = sub.add_parser("show", help="查看当前名册与依据台账")
    p_show.add_argument("workdir")
    p_show.set_defaults(func=cmd_show)

    p_log = sub.add_parser("log", help="查看提交链")
    p_log.add_argument("workdir")
    p_log.set_defaults(func=cmd_log)

    p_ver = sub.add_parser("verify", help="全量重放核验工作区")
    p_ver.add_argument("workdir")
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ws.RosterError as exc:
        json.dump(
            {"ok": False, "error": exc.code, "message": str(exc)},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
