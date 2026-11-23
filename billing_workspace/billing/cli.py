"""离线出账命令行入口。

用法示例：
  python3 -I -m billing.cli run \
      --report ../booth_collector/out/S1_complete.json \
      --meter-log examples/meter_log.jsonl \
      --attribution examples/attribution.jsonl \
      --batch-bound 5 --from 2025-03-01 --to 2025-03-02 \
      --max-gap-seconds 14400 --out ./bill_out

  python3 -I -m billing.cli show --out ./bill_out
  python3 -I -m billing.cli verify ./bill_out            # 核验 CURRENT 指向的账单
  python3 -I -m billing.cli verify ./bill_out/bill-000001
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 同时支持「python -m billing.cli」与「python -I /abs/path/billing/cli.py」：
# 隔离模式（-I 隐含 -P）不会自动把脚本目录加入 sys.path，需要显式回退。
try:  # pragma: no cover - 两种入口的导入分支
    from . import settle
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from billing import settle


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_run(args: argparse.Namespace) -> int:
    result = settle.run_billing(
        report_path=args.report,
        log_path=args.meter_log,
        attr_path=args.attribution,
        batch_bound=args.batch_bound,
        date_from=args.date_from,
        date_to=args.date_to,
        max_gap_seconds=args.max_gap_seconds,
        out_dir=args.out,
        work_dir=args.work_dir,
    )
    manifest = result["manifest"]
    _print_json(
        {
            "ok": True,
            "bill": result["bill"],
            "bill_dir": result["bill_dir"],
            "window": [manifest["parameters"]["date_from"], manifest["parameters"]["date_to"]],
            "counts": manifest["counts"],
        }
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    bill_dir = settle._resolve_bill_dir(args.out)
    with open(os.path.join(bill_dir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    _print_json(
        {
            "ok": True,
            "bill": os.path.basename(bill_dir),
            "bill_dir": os.path.abspath(bill_dir),
            "manifest": manifest,
        }
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _print_json(settle.verify_bill(args.path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="billing.cli",
        description="离线出账：临时电表逐日电量结算与随账明细交付",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一次离线出账")
    p_run.add_argument("--report", required=True, help="已验收的全目录采集报告路径")
    p_run.add_argument("--meter-log", required=True, help="表计日志（逐行 JSON）")
    p_run.add_argument("--attribution", required=True, help="归属表（逐行 JSON）")
    p_run.add_argument("--batch-bound", required=True, type=int, help="出账批次上界（非负整数）")
    p_run.add_argument("--from", dest="date_from", required=True, help="首个 UTC 日期 YYYY-MM-DD")
    p_run.add_argument("--to", dest="date_to", required=True, help="末个 UTC 日期 YYYY-MM-DD（含）")
    p_run.add_argument("--max-gap-seconds", required=True, type=int,
                       help="财务给定的最大可结算间隔（正整数秒）")
    p_run.add_argument("--out", required=True, help="出账输出目录（版本化存放）")
    p_run.add_argument("--work-dir", help="磁盘临时空间（默认在输出目录下自建自清）")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="查看当前账单（CURRENT 指向）")
    p_show.add_argument("--out", required=True, help="出账输出目录")
    p_show.set_defaults(func=cmd_show)

    p_ver = sub.add_parser("verify", help="独立核验：明细逐项对回汇总")
    p_ver.add_argument("path", help="出账输出目录或某个 bill-NNNNNN 目录")
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except settle.BillingError as exc:
        json.dump(
            {"ok": False, "error": exc.code, "message": str(exc)},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
