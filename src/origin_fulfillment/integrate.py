"""从落地层重建整合层并重算承诺核验。

整合层是落地层的纯函数：每次摄入后整层重建，结果只取决于落地事实，
因此可重复、可回放。核验历史表只追加，记录每一批次下的判定轨迹。

双时间轴口径：

- *事件时间*：``published_at`` / ``placed_at`` / ``shipped_at`` 等业务时间；
- *记录时间*：``ingest_batch.batch_id`` 代表资料进入仓库的先后。
  订单通过 ``post_revision`` 钉住下单时看到的视频版本，之后的内容更正
  只会形成新版本，绝不会回溯修改该订单核验所依据的承诺；
- 后补凭证改变批次状态后，重算可能翻转未完成发货的判定，而每次翻转
  都在 ``check_history`` 中留痕，已发生过的判定不会被抹掉。
"""

from __future__ import annotations

import json
import sqlite3

from .schema import CONFORMING, CONTRADICTED, EVIDENCE_VERIFIED_STATE, PENDING
from .verify import ShipmentFacts, aggregate_order, evaluate_shipment

# 重建时整体清空的整合/核验表（视图与历史表除外）。
_REBUILT_TABLES = (
    "video_post", "video_revision", "video_claim",
    "supplier", "product", "lot", "lot_history", "certificate_evidence",
    "customer_order", "order_pinned_claim", "shipment",
    "shipment_claim_check", "order_status_snapshot",
)

_APPEND_ONLY_HISTORY = ("check_history", "order_status_history")


def _latest_records(conn: sqlite3.Connection, source_name: str) -> list[dict[str, str]]:
    """取每个业务 ``record_id`` 在最新批次中的载荷（按 record_id 排序保证确定性）。"""
    rows = conn.execute(
        """
        SELECT rr.record_id, rr.payload_json, rr.batch_id
          FROM raw_record rr
          JOIN (
                SELECT record_id, MAX(batch_id) AS max_batch
                  FROM raw_record
                 WHERE source_name = ?
                 GROUP BY record_id
               ) latest
            ON latest.record_id = rr.record_id
           AND latest.max_batch = rr.batch_id
         WHERE rr.source_name = ?
         ORDER BY rr.record_id
        """,
        (source_name, source_name),
    ).fetchall()
    return [
        {**json.loads(row["payload_json"]), "_batch_id": row["batch_id"]}
        for row in rows
    ]


def _all_observations(conn: sqlite3.Connection, source_name: str) -> list[dict[str, str]]:
    """取某来源全部历史观察（按批次、record_id 排序），用于 SCD 历史。"""
    rows = conn.execute(
        """
        SELECT payload_json, batch_id FROM raw_record
         WHERE source_name = ?
         ORDER BY batch_id, record_id
        """,
        (source_name,),
    ).fetchall()
    return [
        {**json.loads(row["payload_json"]), "_batch_id": row["batch_id"]}
        for row in rows
    ]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def rebuild(conn: sqlite3.Connection) -> int:
    """重建整合层并写入本批次核验结果，返回当前最新批次号。

    整合层各表间存在外键，整层清空-重填过程中必然出现暂时悬空引用，
    因此在单个事务内临时关闭外键强制；事务提交后重新开启。
    任何失败都会回滚，落地层与历史表不受影响。
    """
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        _rebuild(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute("SELECT COALESCE(MAX(batch_id), 0) AS b FROM ingest_batch").fetchone()
    return int(row["b"])


def _rebuild(conn: sqlite3.Connection) -> None:
    for table in _REBUILT_TABLES:
        conn.execute(f"DELETE FROM {table}")

    claims_raw = _latest_records(conn, "content_claims")
    lots_raw = _latest_records(conn, "lot_records")
    orders_raw = _latest_records(conn, "orders")
    shipments_raw = _latest_records(conn, "shipments")
    current_batch = int(
        conn.execute("SELECT COALESCE(MAX(batch_id), 0) AS b FROM ingest_batch").fetchone()["b"]
    )

    _build_content(conn, claims_raw)
    _build_products_and_suppliers(conn, lots_raw, claims_raw, orders_raw, shipments_raw)
    _build_lots(conn, lots_raw)
    _build_orders_and_shipments(conn, orders_raw, shipments_raw, lots_raw, current_batch)


def _build_content(conn: sqlite3.Connection, claims_raw: list[dict[str, str]]) -> None:
    posts: dict[str, dict[str, int]] = {}
    revisions: set[tuple[str, int]] = set()

    for row in claims_raw:
        post_id = row["post_id"]
        revision = int(row["revision"])
        published_at = row["published_at"]
        batch_id = int(row["_batch_id"])
        info = posts.setdefault(post_id, {"first": batch_id, "last": batch_id})
        info["first"] = min(info["first"], batch_id)
        info["last"] = max(info["last"], batch_id)
        revisions.add((post_id, revision, published_at, batch_id))

    conn.executemany(
        "INSERT INTO video_post (post_id, first_seen_batch, last_seen_batch) VALUES (?, ?, ?)",
        [(post_id, info["first"], info["last"]) for post_id, info in sorted(posts.items())],
    )
    conn.executemany(
        "INSERT INTO video_revision (post_id, revision, published_at, first_seen_batch) "
        "VALUES (?, ?, ?, ?)",
        sorted(revisions),
    )
    conn.executemany(
        "INSERT INTO video_claim (claim_id, post_id, revision, product_id, claim_kind, "
        "claim_value, published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["claim_id"], row["post_id"], int(row["revision"]), row["product_id"],
                row["claim_kind"], row["claim_value"], row["published_at"],
            )
            for row in claims_raw
        ],
    )


def _build_products_and_suppliers(
    conn: sqlite3.Connection,
    lots_raw: list[dict[str, str]],
    claims_raw: list[dict[str, str]],
    orders_raw: list[dict[str, str]],
    shipments_raw: list[dict[str, str]],
) -> None:
    product_batches: dict[str, int] = {}
    for row in (*claims_raw, *lots_raw, *orders_raw, *shipments_raw):
        product_id = _clean(row.get("product_id"))
        if product_id:
            product_batches[product_id] = min(
                product_batches.get(product_id, 10**18), int(row["_batch_id"])
            )
    conn.executemany(
        "INSERT INTO product (product_id, first_seen_batch) VALUES (?, ?)",
        sorted(product_batches.items()),
    )

    supplier_batches: dict[str, int] = {}
    for row in lots_raw:
        supplier_id = _clean(row.get("supplier_id"))
        if supplier_id:
            supplier_batches[supplier_id] = min(
                supplier_batches.get(supplier_id, 10**18), int(row["_batch_id"])
            )
    conn.executemany(
        "INSERT INTO supplier (supplier_id, first_seen_batch) VALUES (?, ?)",
        sorted(supplier_batches.items()),
    )


def _build_lots(conn: sqlite3.Connection, lots_raw: list[dict[str, str]]) -> None:
    # 当前状态表。
    conn.executemany(
        "INSERT INTO lot (lot_id, supplier_id, product_id, origin_region, harvest_date, "
        "certificate_ref, record_state, evidence_valid, first_seen_batch, last_seen_batch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["lot_id"], _clean(row.get("supplier_id")), row["product_id"],
                _clean(row.get("origin_region")), _clean(row.get("harvest_date")),
                _clean(row.get("certificate_ref")), row["record_state"],
                1 if row["record_state"] == EVIDENCE_VERIFIED_STATE else 0,
                int(row["_batch_id"]), int(row["_batch_id"]),
            )
            for row in lots_raw
        ],
    )

    # SCD2 风格历史：同一 lot 每次载荷变化都在对应批次留一行（含首次出现）。
    history_rows: list[tuple] = []
    last_signature_by_lot: dict[str, tuple] = {}
    for row in _all_observations(conn, "lot_records"):
        lot_id = row["lot_id"]
        signature = (
            _clean(row.get("supplier_id")), row["product_id"],
            _clean(row.get("origin_region")), _clean(row.get("harvest_date")),
            _clean(row.get("certificate_ref")), row["record_state"],
        )
        if last_signature_by_lot.get(lot_id) == signature:
            continue
        last_signature_by_lot[lot_id] = signature
        history_rows.append(
            (
                lot_id, int(row["_batch_id"]), signature[0], signature[1], signature[2],
                signature[3], signature[4], signature[5],
                1 if signature[5] == EVIDENCE_VERIFIED_STATE else 0,
            )
        )
    conn.executemany(
        "INSERT INTO lot_history (lot_id, batch_id, supplier_id, product_id, origin_region, "
        "harvest_date, certificate_ref, record_state, evidence_valid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        history_rows,
    )

    # 凭证适用范围：一张凭证号可关联多个批次；无效凭证同样保留并明确标记。
    evidence: dict[tuple[str, str], tuple] = {}
    for row in lots_raw:
        cert = _clean(row.get("certificate_ref"))
        if not cert:
            continue
        evidence[(cert, row["lot_id"])] = (
            cert, row["lot_id"], row["product_id"], _clean(row.get("origin_region")),
            _clean(row.get("harvest_date")),
            1 if row["record_state"] == EVIDENCE_VERIFIED_STATE else 0,
            row["record_state"],
        )
    conn.executemany(
        "INSERT INTO certificate_evidence (certificate_ref, lot_id, product_id, origin_region, "
        "harvest_date, evidence_valid, latest_record_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
        list(evidence.values()),
    )


def _build_orders_and_shipments(
    conn: sqlite3.Connection,
    orders_raw: list[dict[str, str]],
    shipments_raw: list[dict[str, str]],
    lots_raw: list[dict[str, str]],
    current_batch: int,
) -> None:
    conn.executemany(
        "INSERT INTO customer_order (order_id, post_id, post_revision, product_id, placed_at, "
        "order_state) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                row["order_id"], row["post_id"], int(row["post_revision"]), row["product_id"],
                row["placed_at"], row["order_state"],
            )
            for row in orders_raw
        ],
    )

    # 钉住下单时版本：只关联该 post 在该 revision 及更早发布、且当时存在的承诺。
    # 内容团队后续发布的更正版本不会出现在这里。
    claim_lookup: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for claim in conn.execute(
        "SELECT * FROM video_claim ORDER BY claim_id"
    ).fetchall():
        claim_lookup.setdefault((claim["post_id"], claim["revision"]), []).append(claim)

    pinned_rows: list[tuple[str, str]] = []
    for row in orders_raw:
        key = (row["post_id"], int(row["post_revision"]))
        for claim in claim_lookup.get(key, ()):  # 同版本内商品可不同，核验时按商品再过滤
            pinned_rows.append((row["order_id"], claim["claim_id"]))
    conn.executemany(
        "INSERT OR IGNORE INTO order_pinned_claim (order_id, claim_id) VALUES (?, ?)",
        pinned_rows,
    )

    conn.executemany(
        "INSERT INTO shipment (shipment_id, order_id, product_id, lot_id, shipped_at, "
        "delivered_at, replaces_shipment, aftercare_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["shipment_id"], row["order_id"], row["product_id"], _clean(row.get("lot_id")),
                row["shipped_at"], _clean(row.get("delivered_at")),
                _clean(row.get("replaces_shipment")), row["aftercare_state"],
            )
            for row in shipments_raw
        ],
    )

    lots_by_id = {row["lot_id"]: row for row in lots_raw}
    _build_checks(conn, orders_raw, shipments_raw, lots_by_id, claim_lookup, current_batch)


def _build_checks(
    conn: sqlite3.Connection,
    orders_raw: list[dict[str, str]],
    shipments_raw: list[dict[str, str]],
    lots_by_id: dict[str, dict[str, str]],
    claim_lookup: dict[tuple[str, int], list[sqlite3.Row]],
    current_batch: int,
) -> None:
    shipments_by_order: dict[str, list[dict[str, str]]] = {}
    for row in shipments_raw:
        shipments_by_order.setdefault(row["order_id"], []).append(row)

    check_rows: list[tuple] = []
    history_rows: list[tuple] = []
    snapshot_rows: list[tuple] = []
    order_history_rows: list[tuple] = []

    for order in orders_raw:
        order_id = order["order_id"]
        post_id = order["post_id"]
        revision = int(order["post_revision"])
        product_id = order["product_id"]

        # 仅取同商品承诺；不同商品的承诺不参与该订单核验。
        pinned_claims = [
            dict(claim)
            for claim in claim_lookup.get((post_id, revision), ())
            if claim["product_id"] == product_id
        ]
        order_shipments = sorted(shipments_by_order.get(order_id, []), key=lambda r: r["shipment_id"])

        order_states: list[str] = []
        counts = {CONFORMING: 0, CONTRADICTED: 0, PENDING: 0}

        for shipment in order_shipments:
            lot_id = _clean(shipment.get("lot_id"))
            lot = lots_by_id.get(lot_id) if lot_id else None
            facts = ShipmentFacts(
                shipment_id=shipment["shipment_id"],
                product_id=shipment["product_id"],
                lot_id=lot_id,
                lot_product_id=lot["product_id"] if lot else None,
                origin_region=_clean(lot.get("origin_region")) if lot else None,
                harvest_date=_clean(lot.get("harvest_date")) if lot else None,
                lot_state=lot["record_state"] if lot else None,
            )
            evaluations = evaluate_shipment(pinned_claims, facts)
            for result in evaluations:
                check_id = f"{shipment['shipment_id']}|{result['claim_id'] or '∅'}"
                check_rows.append(
                    (
                        check_id, shipment["shipment_id"], order_id, post_id, revision,
                        product_id, lot_id, result["claim_id"], result["claim_kind"],
                        result["claim_value"], result["state"], result["reason_code"],
                        result["reason_detail"], current_batch,
                    )
                )
                history_rows.append(
                    (
                        current_batch, shipment["shipment_id"], result["claim_id"],
                        result["state"], result["reason_code"], result["reason_detail"],
                    )
                )
                counts[result["state"]] += 1
                order_states.append(result["state"])

        unshipped = 0 if order_shipments else 1
        overall = aggregate_order(order_states, unshipped)
        snapshot_rows.append(
            (
                order_id, post_id, revision, product_id, order["placed_at"],
                len(order_shipments), counts[CONFORMING], counts[CONTRADICTED],
                counts[PENDING], unshipped, overall, current_batch,
            )
        )
        order_history_rows.append(
            (
                current_batch, order_id, overall, len(order_shipments),
                counts[CONFORMING], counts[CONTRADICTED], counts[PENDING], unshipped,
            )
        )

    conn.executemany(
        "INSERT INTO shipment_claim_check (check_id, shipment_id, order_id, post_id, "
        "post_revision, product_id, lot_id, claim_id, claim_kind, claim_value, "
        "conformance_state, reason_code, reason_detail, evaluated_batch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        check_rows,
    )
    # 历史表只追加：同一批次重跑（无新数据则不会产生新批次）用 INSERT OR IGNORE 防御。
    conn.executemany(
        "INSERT OR IGNORE INTO check_history (batch_id, shipment_id, claim_id, "
        "conformance_state, reason_code, reason_detail) VALUES (?, ?, ?, ?, ?, ?)",
        history_rows,
    )
    conn.executemany(
        "INSERT INTO order_status_snapshot (order_id, post_id, post_revision, product_id, "
        "placed_at, shipment_count, conforming_count, contradicted_count, pending_count, "
        "unshipped_count, overall_state, evaluated_batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        snapshot_rows,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO order_status_history (batch_id, order_id, overall_state, "
        "shipment_count, conforming_count, contradicted_count, pending_count, unshipped_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        order_history_rows,
    )
