"""面向采购、内容、售后三类角色的只读查询。

所有函数只读取整合层/核验层，返回 ``sqlite3.Row`` 列表，
由 :mod:`origin_fulfillment.reports` 负责渲染，程序化调用方可直接取行。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .schema import VALID_STATES


def _validate_state(state: str) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"未知核验状态 {state!r}，允许值: {sorted(VALID_STATES)}")


def evidence_applicability(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """采购视角：每项产地/采收凭证适用于哪些批次、是否有效、被哪些订单使用。"""
    return conn.execute(
        """
        SELECT certificate_ref, lot_id, supplier_id, product_id, origin_region,
               harvest_date, evidence_valid, latest_record_state,
               shipment_rows, used_by_orders
          FROM v_evidence_applicability
         ORDER BY evidence_valid DESC, certificate_ref, lot_id
        """
    ).fetchall()


def lot_timeline(conn: sqlite3.Connection, lot_id: str) -> list[sqlite3.Row]:
    """采购视角：单个批次随交付批次变化的 SCD 状态轨迹（后补凭证可回放）。"""
    return conn.execute(
        """
        SELECT h.batch_id, b.delivered_at, h.lot_id, h.supplier_id, h.product_id,
               h.origin_region, h.harvest_date, h.certificate_ref, h.record_state,
               h.evidence_valid
          FROM lot_history h
          JOIN ingest_batch b ON b.batch_id = h.batch_id
         WHERE h.lot_id = ?
         ORDER BY h.batch_id
        """,
        (lot_id,),
    ).fetchall()


def post_versions(conn: sqlite3.Connection, post_id: str | None = None) -> list[sqlite3.Row]:
    """内容视角：按视频版本查看公开承诺及其适用商品、发布时间。"""
    sql = """
        SELECT p.post_id, r.revision, r.published_at,
               c.claim_id, c.product_id, c.claim_kind, c.claim_value
          FROM video_post p
          JOIN video_revision r ON r.post_id = p.post_id
          LEFT JOIN video_claim c
            ON c.post_id = r.post_id AND c.revision = r.revision
    """
    params: tuple[Any, ...] = ()
    if post_id is not None:
        sql += " WHERE p.post_id = ?"
        params = (post_id,)
    sql += " ORDER BY p.post_id, r.revision, c.claim_id"
    return conn.execute(sql, params).fetchall()


def order_trace(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    """售后视角：订单头部信息与整体核验状态。"""
    return conn.execute(
        "SELECT * FROM v_order_trace WHERE order_id = ?", (order_id,)
    ).fetchone()


def order_shipments(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    """售后视角：订单的每笔实际发货（含拆单、替代、缺失批次关系）。"""
    return conn.execute(
        "SELECT * FROM v_shipment_full WHERE order_id = ? ORDER BY shipped_at, shipment_id",
        (order_id,),
    ).fetchall()


def shipment_checks(conn: sqlite3.Connection, shipment_id: str) -> list[sqlite3.Row]:
    """售后视角：单笔发货对下单时全部承诺的当前核验结论。"""
    return conn.execute(
        """
        SELECT * FROM shipment_claim_check
         WHERE shipment_id = ?
         ORDER BY COALESCE(claim_id, '∅')
        """,
        (shipment_id,),
    ).fetchall()


def check_timeline(conn: sqlite3.Connection, shipment_id: str) -> list[sqlite3.Row]:
    """售后视角：单笔发货核验状态随每次交付的变化轨迹（后补凭证/更正的影响）。"""
    return conn.execute(
        """
        SELECT h.batch_id, b.delivered_at, h.shipment_id, h.claim_id,
               h.conformance_state, h.reason_code, h.reason_detail
          FROM check_history h
          JOIN ingest_batch b ON b.batch_id = h.batch_id
         WHERE h.shipment_id = ?
         ORDER BY h.batch_id, COALESCE(h.claim_id, '∅')
        """,
        (shipment_id,),
    ).fetchall()


def order_status_timeline(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    """售后视角：订单整体状态的逐批次历史。"""
    return conn.execute(
        """
        SELECT h.batch_id, b.delivered_at, h.order_id, h.overall_state,
               h.shipment_count, h.conforming_count, h.contradicted_count,
               h.pending_count, h.unshipped_count
          FROM order_status_history h
          JOIN ingest_batch b ON b.batch_id = h.batch_id
         WHERE h.order_id = ?
         ORDER BY h.batch_id
        """,
        (order_id,),
    ).fetchall()


def orders_by_state(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    """按当前整体核验状态列订单（三值之一）。"""
    _validate_state(state)
    return conn.execute(
        "SELECT * FROM v_order_trace WHERE overall_state = ? ORDER BY placed_at, order_id",
        (state,),
    ).fetchall()


def list_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM v_order_trace ORDER BY placed_at, order_id"
    ).fetchall()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """摄入批次台账：每次交付的时间、目录与落地记录数。"""
    return conn.execute(
        """
        SELECT b.batch_id, b.delivered_at, b.ingested_at, b.source_dir,
               COUNT(r.record_id) AS record_rows
          FROM ingest_batch b
          LEFT JOIN raw_record r ON r.batch_id = b.batch_id
         GROUP BY b.batch_id
         ORDER BY b.batch_id
        """
    ).fetchall()
