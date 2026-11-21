"""离线命令行入口。

示例：

    python -m origin_fulfillment ingest --dir sources --at 2026-07-08T00:00:00Z
    python -m origin_fulfillment evidence
    python -m origin_fulfillment posts --post POST-4
    python -m origin_fulfillment trace ORD-91
    python -m origin_fulfillment history --shipment SHIP-52
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .db import connect
from .ingest import ingest_delivery
from .integrate import rebuild
from . import queries
from .queries import (
    check_timeline,
    evidence_applicability,
    list_batches,
    list_orders,
    order_shipments,
)
from .reports import render_json, render_table

DEFAULT_DB = Path("data/warehouse.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="origin_fulfillment",
        description="助农产地承诺离线事实资料库：摄入、核验与追溯查询",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 仓库文件路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出查询结果")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="摄入一个 CSV 交付目录并重建整合层")
    ingest_parser.add_argument("--dir", required=True, dest="source_dir", help="来源 CSV 所在目录")
    ingest_parser.add_argument("--at", dest="delivered_at", default=None,
                               help="交付时间（ISO 8601），回放每日交付时传入")

    subparsers.add_parser("batches", help="查看摄入批次台账")
    subparsers.add_parser("evidence", help="采购：凭证适用的批次与订单范围")

    lots_parser = subparsers.add_parser("lot", help="采购：查看批次状态轨迹")
    lots_parser.add_argument("lot_id")

    posts_parser = subparsers.add_parser("posts", help="内容：按视频版本查看公开承诺")
    posts_parser.add_argument("--post", dest="post_id", default=None)

    subparsers.add_parser("orders", help="售后：全部订单与当前整体核验状态")

    states_parser = subparsers.add_parser("orders-state", help="按核验状态筛选订单")
    states_parser.add_argument("state", choices=["conforming", "contradicted", "pending_verification"])

    trace_parser = subparsers.add_parser("trace", help="售后：订单争议追溯（头部、发货、每笔核验）")
    trace_parser.add_argument("order_id")

    history_parser = subparsers.add_parser("history", help="售后：发货或订单的核验变化轨迹")
    history_parser.add_argument("--shipment", dest="shipment_id", default=None)
    history_parser.add_argument("--order", dest="order_id", default=None)

    subparsers.add_parser("version", help="输出版本号")
    return parser


def _emit(rows, as_json: bool) -> None:
    if as_json:
        print(render_json(rows))
    else:
        print(render_table(rows))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = connect(args.db)
    try:
        if args.command == "version":
            print(__version__)
            return 0

        if args.command == "ingest":
            stats = ingest_delivery(conn, args.source_dir, args.delivered_at)
            batch_id = rebuild(conn)
            print(f"交付目录: {stats.source_dir}")
            print(f"声明交付时间: {stats.delivered_at}")
            if stats.missing_files:
                print(f"缺失文件（跳过）: {', '.join(stats.missing_files)}")
            for source_name in sorted(stats.inserted):
                print(
                    f"  {source_name}: 新增落地 {stats.inserted[source_name]} 行，"
                    f"未变化跳过 {stats.skipped_unchanged[source_name]} 行"
                )
            if stats.created_batch:
                print(f"已创建摄入批次 #{stats.batch_id}，整合层已重建至批次 #{batch_id}")
            else:
                print("未发现新增或变化记录，保持幂等；整合层已按当前落地事实重建。")
            return 0

        if args.command == "batches":
            _emit(list_batches(conn), args.json)
        elif args.command == "evidence":
            _emit(evidence_applicability(conn), args.json)
        elif args.command == "lot":
            _emit(queries.lot_timeline(conn, args.lot_id), args.json)
        elif args.command == "posts":
            _emit(queries.post_versions(conn, args.post_id), args.json)
        elif args.command == "orders":
            _emit(list_orders(conn), args.json)
        elif args.command == "orders-state":
            _emit(queries.orders_by_state(conn, args.state), args.json)
        elif args.command == "trace":
            header = queries.order_trace(conn, args.order_id)
            if header is None:
                print(f"未找到订单 {args.order_id}", file=sys.stderr)
                return 2
            shipments = order_shipments(conn, args.order_id)
            if args.json:
                payload = {
                    "order": dict(header),
                    "shipments": [dict(row) for row in shipments],
                    "checks": {
                        row["shipment_id"]: [dict(item) for item in queries.shipment_checks(conn, row["shipment_id"])]
                        for row in shipments
                    },
                }
                print(render_json([payload]))
            else:
                print("订单概览")
                print(render_table([header]))
                print("\n实际发货（含拆单/替代/批次关系）")
                print(render_table(shipments))
                for shipment in shipments:
                    print(f"\n发货 {shipment['shipment_id']} 的承诺核验")
                    print(render_table(queries.shipment_checks(conn, shipment["shipment_id"])))
        elif args.command == "history":
            if not args.shipment_id and not args.order_id:
                parser.error("history 需要 --shipment 或 --order 参数")
            if args.shipment_id:
                _emit(check_timeline(conn, args.shipment_id), args.json)
            else:
                _emit(queries.order_status_timeline(conn, args.order_id), args.json)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
