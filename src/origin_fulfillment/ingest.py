"""把本地 CSV 交付目录追加写入落地层。

设计约束：

- 落地层只追加：业务 ``record_id`` 每次出现的载荷都按批次保存，
  后续修正不会覆盖早先观察到的内容；
- 指纹去重：与最近一次载荷完全相同的记录不重复落地，保证状态历史只在
  真实变化时新增；
- 幂等：重复摄入同一目录不会产生新批次，也不会污染历史；
- 不信任输入路径：本模块只读取显式传入目录下的白名单文件名。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .timeutil import instant_text

SOURCE_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    # 文件名 -> (来源名, 必需列)
    "content_claims.csv": (
        "content_claims",
        ("record_id", "claim_id", "post_id", "revision", "product_id",
         "claim_kind", "claim_value", "published_at"),
    ),
    "lot_records.csv": (
        "lot_records",
        ("record_id", "lot_id", "supplier_id", "product_id", "origin_region",
         "harvest_date", "certificate_ref", "record_state"),
    ),
    "orders.csv": (
        "orders",
        ("record_id", "order_id", "post_id", "post_revision", "product_id",
         "placed_at", "order_state"),
    ),
    "shipments.csv": (
        "shipments",
        ("record_id", "shipment_id", "order_id", "product_id", "lot_id",
         "shipped_at", "delivered_at", "replaces_shipment", "aftercare_state"),
    ),
}


class IngestError(ValueError):
    """来源文件结构或内容不满足入库约定。"""


@dataclass
class IngestStats:
    source_dir: str
    delivered_at: str
    batch_id: int | None = None
    created_batch: bool = False
    inserted: dict[str, int] = field(default_factory=dict)
    skipped_unchanged: dict[str, int] = field(default_factory=dict)
    missing_files: list[str] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped_unchanged.values())


def _read_source(path: Path, required_columns: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise IngestError(f"{path.name} 缺少表头")
        missing = [column for column in required_columns if column not in reader.fieldnames]
        if missing:
            raise IngestError(f"{path.name} 缺少必需列: {', '.join(missing)}")
        rows = list(reader)
    seen: set[str] = set()
    for row in rows:
        record_id = (row.get("record_id") or "").strip()
        if not record_id:
            raise IngestError(f"{path.name} 存在空 record_id")
        if record_id in seen:
            raise IngestError(f"{path.name} 中 record_id 重复: {record_id}")
        seen.add(record_id)
    return rows


def _latest_hash(conn: sqlite3.Connection, record_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT payload_hash FROM raw_record
         WHERE record_id = ?
         ORDER BY batch_id DESC LIMIT 1
        """,
        (record_id,),
    ).fetchone()
    return None if row is None else row["payload_hash"]


def ingest_delivery(
    conn: sqlite3.Connection,
    source_dir: str | Path,
    delivered_at: str | None = None,
) -> IngestStats:
    """摄入一个交付目录。

    参数:
        source_dir: 含四个来源 CSV 的目录；允许缺少其中任意文件。
        delivered_at: 业务交付时间（ISO 8601），缺省取当前 UTC 时间。
            回放历史每日交付时应显式传入对应日期。
    """
    directory = Path(source_dir)
    if not directory.is_dir():
        raise IngestError(f"来源目录不存在或不是目录: {directory}")
    delivered = instant_text(delivered_at) if delivered_at else datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    stats = IngestStats(source_dir=str(directory), delivered_at=delivered)
    pending_rows: list[tuple[str, str, str, str, str]] = []

    for filename, (source_name, required_columns) in SOURCE_SPECS.items():
        path = directory / filename
        if not path.exists():
            stats.missing_files.append(filename)
            stats.inserted[source_name] = 0
            stats.skipped_unchanged[source_name] = 0
            continue
        rows = _read_source(path, required_columns)
        inserted = skipped = 0
        for row in rows:
            record_id = row["record_id"].strip()
            payload = {key: (value.strip() if value is not None else "") for key, value in row.items()}
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            if _latest_hash(conn, record_id) == payload_hash:
                skipped += 1
                continue
            pending_rows.append(
                (record_id, source_name, payload_json, payload_hash, delivered)
            )
            inserted += 1
        stats.inserted[source_name] = inserted
        stats.skipped_unchanged[source_name] = skipped

    if not pending_rows:
        # 全量重复交付：保持幂等，不制造空批次。
        conn.commit()
        return stats

    cursor = conn.execute(
        "INSERT INTO ingest_batch (delivered_at, ingested_at, source_dir, state) "
        "VALUES (?, ?, ?, 'committed')",
        (delivered, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), str(directory)),
    )
    batch_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO raw_record (record_id, source_name, batch_id, payload_json, "
        "payload_hash, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (record_id, source_name, batch_id, payload_json, payload_hash, delivered)
            for record_id, source_name, payload_json, payload_hash, delivered in pending_rows
        ],
    )
    conn.commit()
    stats.batch_id = batch_id
    stats.created_batch = True
    return stats
