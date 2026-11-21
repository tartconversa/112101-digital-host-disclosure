"""SQLite 表结构定义。

仓库分三层，全部位于同一个离线 SQLite 文件中：

落地层（只追加，永不更新）
    ``ingest_batch`` 记录每次 CSV 交付；``raw_record`` 按业务 ``record_id``
    保存每一次观察到的完整载荷与指纹，重复交付不会抹掉历史。

整合层（每次摄入后从落地层确定性重建）
    视频版本承诺、供应方/批次/凭证（含 SCD2 状态历史）、订单与发货。

核验层（重建后重算，状态变化另写只追加历史表）
    ``shipment_claim_check`` 是当前判定；``check_history`` 与
    ``order_status_history`` 保留每次摄入的判定轨迹，使后补凭证与
    内容更正的影响可回放。
"""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ------------------------------------------------------------------ 落地层
    """
    CREATE TABLE IF NOT EXISTS ingest_batch (
        batch_id        INTEGER PRIMARY KEY,
        delivered_at    TEXT NOT NULL,          -- 交付声明时间（来源目录元数据）
        ingested_at     TEXT NOT NULL,          -- 仓库实际摄入时间（UTC）
        source_dir      TEXT NOT NULL,
        state           TEXT NOT NULL           -- committed
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_record (
        record_id       TEXT NOT NULL,
        source_name     TEXT NOT NULL,          -- content_claims / lot_records / ...
        batch_id        INTEGER NOT NULL REFERENCES ingest_batch(batch_id),
        payload_json    TEXT NOT NULL,
        payload_hash    TEXT NOT NULL,
        ingested_at     TEXT NOT NULL,
        PRIMARY KEY (record_id, batch_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_record_source
        ON raw_record(source_name, batch_id)
    """,
    # ------------------------------------------------------------------ 整合层
    """
    CREATE TABLE IF NOT EXISTS video_post (
        post_id         TEXT PRIMARY KEY,
        first_seen_batch INTEGER NOT NULL,
        last_seen_batch  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_revision (
        post_id         TEXT NOT NULL REFERENCES video_post(post_id),
        revision        INTEGER NOT NULL,
        published_at    TEXT NOT NULL,
        first_seen_batch INTEGER NOT NULL,
        PRIMARY KEY (post_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_claim (
        claim_id        TEXT PRIMARY KEY,
        post_id         TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        product_id      TEXT NOT NULL,
        claim_kind      TEXT NOT NULL,          -- origin / harvest_window / ...
        claim_value     TEXT NOT NULL,
        published_at    TEXT NOT NULL,
        FOREIGN KEY (post_id, revision)
            REFERENCES video_revision(post_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS supplier (
        supplier_id     TEXT PRIMARY KEY,
        first_seen_batch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product (
        product_id      TEXT PRIMARY KEY,
        first_seen_batch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lot (
        lot_id          TEXT PRIMARY KEY,
        supplier_id     TEXT,
        product_id      TEXT NOT NULL,
        origin_region   TEXT,
        harvest_date    TEXT,
        certificate_ref TEXT,
        record_state    TEXT NOT NULL,          -- verified / pending / corrected / ...
        evidence_valid  INTEGER NOT NULL,       -- 1 仅当 record_state = 'verified'
        first_seen_batch INTEGER NOT NULL,
        last_seen_batch  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lot_history (
        lot_id          TEXT NOT NULL,
        batch_id        INTEGER NOT NULL,
        supplier_id     TEXT,
        product_id      TEXT NOT NULL,
        origin_region   TEXT,
        harvest_date    TEXT,
        certificate_ref TEXT,
        record_state    TEXT NOT NULL,
        evidence_valid  INTEGER NOT NULL,
        PRIMARY KEY (lot_id, batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS certificate_evidence (
        certificate_ref TEXT NOT NULL,
        lot_id          TEXT NOT NULL,
        product_id      TEXT NOT NULL,
        origin_region   TEXT,
        harvest_date    TEXT,
        evidence_valid  INTEGER NOT NULL,
        latest_record_state TEXT NOT NULL,
        PRIMARY KEY (certificate_ref, lot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_order (
        order_id        TEXT PRIMARY KEY,
        post_id         TEXT NOT NULL,
        post_revision   INTEGER NOT NULL,       -- 钉住用户下单时看到的视频版本
        product_id      TEXT NOT NULL,
        placed_at       TEXT NOT NULL,
        order_state     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_pinned_claim (
        order_id        TEXT NOT NULL REFERENCES customer_order(order_id),
        claim_id        TEXT NOT NULL REFERENCES video_claim(claim_id),
        PRIMARY KEY (order_id, claim_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shipment (
        shipment_id     TEXT PRIMARY KEY,
        order_id        TEXT NOT NULL REFERENCES customer_order(order_id),
        product_id      TEXT NOT NULL,
        lot_id          TEXT,                   -- 缺失批次关系时为 NULL
        shipped_at      TEXT NOT NULL,
        delivered_at    TEXT,
        replaces_shipment TEXT,                 -- 替代/补发指向的原发货
        aftercare_state TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------------------ 核验层
    """
    CREATE TABLE IF NOT EXISTS shipment_claim_check (
        check_id        TEXT PRIMARY KEY,       -- shipment_id || '|' || 承诺或占位符
        shipment_id     TEXT NOT NULL REFERENCES shipment(shipment_id),
        order_id        TEXT NOT NULL,
        post_id         TEXT NOT NULL,
        post_revision   INTEGER NOT NULL,
        product_id      TEXT NOT NULL,
        lot_id          TEXT,
        claim_id        TEXT,                   -- NULL：订单钉住版本没有可核验承诺
        claim_kind      TEXT,
        claim_value     TEXT,
        conformance_state TEXT NOT NULL,        -- conforming/contradicted/pending_verification
        reason_code     TEXT NOT NULL,
        reason_detail   TEXT NOT NULL,
        evaluated_batch INTEGER NOT NULL
    )
    """,
    """
    -- 历史表只追加；SQLite 普通表主键不允许表达式，用表达式唯一索引保证幂等。
    CREATE TABLE IF NOT EXISTS check_history (
        batch_id        INTEGER NOT NULL,
        shipment_id     TEXT NOT NULL,
        claim_id        TEXT,
        conformance_state TEXT NOT NULL,
        reason_code     TEXT NOT NULL,
        reason_detail   TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_check_history
        ON check_history(batch_id, shipment_id, COALESCE(claim_id, '∅'))
    """,
    """
    CREATE TABLE IF NOT EXISTS order_status_snapshot (
        order_id        TEXT PRIMARY KEY REFERENCES customer_order(order_id),
        post_id         TEXT NOT NULL,
        post_revision   INTEGER NOT NULL,
        product_id      TEXT NOT NULL,
        placed_at       TEXT NOT NULL,
        shipment_count  INTEGER NOT NULL,
        conforming_count    INTEGER NOT NULL,
        contradicted_count  INTEGER NOT NULL,
        pending_count       INTEGER NOT NULL,
        unshipped_count     INTEGER NOT NULL,
        overall_state   TEXT NOT NULL,
        evaluated_batch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_status_history (
        batch_id        INTEGER NOT NULL,
        order_id        TEXT NOT NULL,
        overall_state   TEXT NOT NULL,
        shipment_count  INTEGER NOT NULL,
        conforming_count    INTEGER NOT NULL,
        contradicted_count  INTEGER NOT NULL,
        pending_count       INTEGER NOT NULL,
        unshipped_count     INTEGER NOT NULL,
        PRIMARY KEY (batch_id, order_id)
    )
    """,
    # ------------------------------------------------------------------ 视图
    """
    CREATE VIEW IF NOT EXISTS v_evidence_applicability AS
    SELECT
        c.certificate_ref,
        c.lot_id,
        c.product_id,
        c.origin_region,
        c.harvest_date,
        c.evidence_valid,
        c.latest_record_state,
        l.supplier_id,
        (SELECT COUNT(*) FROM shipment s WHERE s.lot_id = c.lot_id) AS shipment_rows,
        (SELECT GROUP_CONCAT(DISTINCT s.order_id)
           FROM shipment s WHERE s.lot_id = c.lot_id) AS used_by_orders
    FROM certificate_evidence c
    JOIN lot l ON l.lot_id = c.lot_id
    """,
    """
    CREATE VIEW IF NOT EXISTS v_shipment_full AS
    SELECT
        s.shipment_id, s.order_id, s.product_id, s.lot_id,
        s.shipped_at, s.delivered_at, s.replaces_shipment,
        s.aftercare_state,
        o.post_id, o.post_revision,
        l.supplier_id, l.origin_region AS lot_origin,
        l.harvest_date AS lot_harvest_date,
        l.certificate_ref, l.record_state AS lot_state,
        l.evidence_valid
    FROM shipment s
    JOIN customer_order o ON o.order_id = s.order_id
    LEFT JOIN lot l ON l.lot_id = s.lot_id
    """,
    """
    CREATE VIEW IF NOT EXISTS v_order_trace AS
    SELECT
        o.order_id, o.post_id, o.post_revision, o.product_id,
        o.placed_at, o.order_state,
        snap.overall_state, snap.shipment_count,
        snap.conforming_count, snap.contradicted_count,
        snap.pending_count, snap.unshipped_count
    FROM customer_order o
    LEFT JOIN order_status_snapshot snap ON snap.order_id = o.order_id
    """,
)

# 状态词表：只允许三值，缺证据必须落 pending_verification。
CONFORMING = "conforming"
CONTRADICTED = "contradicted"
PENDING = "pending_verification"
VALID_STATES = frozenset({CONFORMING, CONTRADICTED, PENDING})

# 批次记录中唯一可作为有效产地/采收凭证的状态。
EVIDENCE_VERIFIED_STATE = "verified"
