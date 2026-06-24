#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.orchestrator import run_job
from src.store import ProcessedMailStore


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _print_summary(summary) -> None:
    total_downloaded = 0
    failed = 0
    for r in summary.results:
        if r.error:
            print(f"{r.account_name}: FAILED — {r.error}")
            failed += 1
        else:
            print(
                f"{r.account_name}: scanned={r.scanned} matched={r.matched} "
                f"downloaded={r.downloaded} skipped_processed={r.skipped_processed}"
            )
            total_downloaded += r.downloaded
    print(
        f"汇总: {len(summary.results)} 账号, {failed} 失败, "
        f"共下载 {total_downloaded} 个附件"
    )


def cmd_parse(config_path: Path) -> int:
    from src.parse_orchestrator import run_parse_job
    from src.parse_store import ParseRecordStore
    from src.push_store import PushRecordStore

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    if not config.parser:
        print("配置错误: config.yaml 中未配置 parser 段", file=sys.stderr)
        return 1

    _setup_logging(config.log_path)
    store = ParseRecordStore(config.db_path)
    push_summaries: list[dict] = []
    try:
        result = run_parse_job(
            config.parser,
            config.accounts,
            store,
            push_cfg=config.push,
            push_store_factory=lambda: PushRecordStore(config.db_path),
        )
        push_summaries = result.push_summaries
    finally:
        store.close()

    if result.error:
        print(f"解析失败: {result.error}", file=sys.stderr)
        return 1
    print(
        f"解析完成: 扫描={result.total} 成功={result.ok} "
        f"失败={result.failed} 跳过={result.skipped}"
    )
    if result.excel_path:
        print(f"Excel: {result.excel_path}")
    if push_summaries:
        ok_push = sum(1 for s in push_summaries if s.get("status") == "success")
        print(f"推送完成: {ok_push}/{len(push_summaries)} 批次成功")
        for s in push_summaries:
            if s.get("status") != "success":
                print(f"  推送失败 tenant={s.get('tenant_code')}: {s.get('error')}")
    return 0


def cmd_run(config_path: Path, account: str | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    _setup_logging(config.log_path)
    store = ProcessedMailStore(config.db_path)
    try:
        summary = run_job(config, store=store, account_filter=account)
    finally:
        store.close()

    _print_summary(summary)
    return 1 if summary.any_failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="邮箱简历附件自动抓取")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="配置文件路径 (default: config.yaml)",
    )
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="立即执行一轮抓取")
    run_parser.add_argument("--account", help="仅处理指定 account name")
    sub.add_parser("parse", help="立即执行一轮简历解析")

    if not argv:
        return cmd_run(Path("config.yaml"), account=None)

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args.config, account=args.account)
    if args.command == "parse":
        return cmd_parse(args.config)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
