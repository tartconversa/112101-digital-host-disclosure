#!/usr/bin/env bash
# 离线回放 examples/daily_deliveries 下的四天交付，并打印三角色查询结果。
# 仅依赖 Python 标准库；可重复运行（先删除旧仓库文件）。
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src"
DB="data/demo.sqlite3"
rm -f "$DB" "$DB-wal" "$DB-shm"

for dir in examples/daily_deliveries/day*; do
    day="$(basename "$dir")"
    at="${day#day*-}T00:00:00Z"
    python -m origin_fulfillment --db "$DB" ingest --dir "$dir" --at "$at"
done

echo
echo "== 采购：凭证适用范围 =="
python -m origin_fulfillment --db "$DB" evidence
echo
echo "== 内容：POST-4 版本承诺 =="
python -m origin_fulfillment --db "$DB" posts --post POST-4
echo
echo "== 售后：ORD-93 争议追溯 =="
python -m origin_fulfillment --db "$DB" trace ORD-93
echo
echo "== 售后：SHIP-52 核验变化轨迹 =="
python -m origin_fulfillment --db "$DB" history --shipment SHIP-52
