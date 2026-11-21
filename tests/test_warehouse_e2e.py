from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

import _path  # noqa: F401

from origin_fulfillment.db import connect
from origin_fulfillment.ingest import IngestError, ingest_delivery
from origin_fulfillment.integrate import rebuild
from origin_fulfillment import queries
from origin_fulfillment.schema import CONFORMING, CONTRADICTED, PENDING

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "daily_deliveries"
DAY_DIRS = [
    "day1-2026-07-08",
    "day2-2026-07-10",
    "day3-2026-07-12",
    "day4-2026-07-14",
]
DELIVERED_AT = {
    "day1-2026-07-08": "2026-07-08T00:00:00Z",
    "day2-2026-07-10": "2026-07-10T00:00:00Z",
    "day3-2026-07-12": "2026-07-12T00:00:00Z",
    "day4-2026-07-14": "2026-07-14T00:00:00Z",
}

SOURCE_FILES = ["content_claims.csv", "lot_records.csv", "orders.csv", "shipments.csv"]


def _load_all_days() -> dict[str, dict[str, list[dict[str, str]]]]:
    data: dict[str, dict[str, list[dict[str, str]]]] = {}
    for day in DAY_DIRS:
        data[day] = {}
        for filename in SOURCE_FILES:
            with (EXAMPLES / day / filename).open(encoding="utf-8", newline="") as stream:
                data[day][filename] = list(csv.DictReader(stream))
    return data


FIXTURE = _load_all_days()


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "warehouse.sqlite3"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)

    def _ingest_day(self, day: str) -> None:
        stats = ingest_delivery(self.conn, EXAMPLES / day, DELIVERED_AT[day])
        rebuild(self.conn)
        self.assertTrue(stats.created_batch)

    def _ingest_all(self) -> None:
        for day in DAY_DIRS:
            self._ingest_day(day)

    def test_four_days_create_four_batches_with_raw_history_retained(self) -> None:
        self._ingest_all()
        batches = queries.list_batches(self.conn)
        self.assertEqual([row["batch_id"] for row in batches], [1, 2, 3, 4])
        # LOT-32 在 day1（pending）与 day3（verified）各有一条原始观察，旧观察不被覆盖。
        rows = self.conn.execute(
            "SELECT batch_id, payload_json FROM raw_record WHERE record_id = ? ORDER BY batch_id",
            ("lot_records-02",),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn('"record_state": "pending"', rows[0]["payload_json"])
        self.assertIn('"record_state": "verified"', rows[1]["payload_json"])

    def test_reingest_same_directory_is_idempotent(self) -> None:
        self._ingest_day(DAY_DIRS[0])
        stats = ingest_delivery(self.conn, EXAMPLES / DAY_DIRS[0], DELIVERED_AT[DAY_DIRS[0]])
        self.assertFalse(stats.created_batch)
        self.assertEqual(stats.total_inserted, 0)
        # day1 快照共 7 条业务记录（2 承诺/批次 + 2 订单 + 2 发货中的实际行数）。
        self.assertEqual(stats.total_skipped, 7)
        # 重建仍然成功且批次号不变。
        self.assertEqual(rebuild(self.conn), 1)
        self.assertEqual(len(queries.list_batches(self.conn)), 1)

    def test_late_certificate_flips_pending_to_contradicted_with_history(self) -> None:
        self._ingest_day(DAY_DIRS[0])
        # day1：LOT-32 凭证 pending，即使产地 REGION-B 与承诺 REGION-A 不同，也只能待核实。
        before = {row["conformance_state"]: row for row in queries.shipment_checks(self.conn, "SHIP-52")}
        self.assertEqual(set(before), {PENDING})
        self.assertEqual(before[PENDING]["reason_code"], "evidence_not_verified")

        self._ingest_day(DAY_DIRS[1])
        self._ingest_day(DAY_DIRS[2])  # day3：LOT-32 补交凭证并 verified
        after = {row["conformance_state"]: row for row in queries.shipment_checks(self.conn, "SHIP-52")}
        self.assertEqual(set(after), {CONTRADICTED})
        self.assertEqual(after[CONTRADICTED]["reason_code"], "origin_mismatch")

        timeline = queries.check_timeline(self.conn, "SHIP-52")
        states = [(row["batch_id"], row["conformance_state"]) for row in timeline]
        # 逐批次轨迹完整保留：batch1/2 待核实 -> batch3 起判定冲突。
        self.assertEqual(states, [(1, PENDING), (2, PENDING), (3, CONTRADICTED)])

    def test_lot_scd_timeline_records_state_change(self) -> None:
        self._ingest_all()
        timeline = queries.lot_timeline(self.conn, "LOT-32")
        self.assertEqual(
            [(row["batch_id"], row["record_state"], row["evidence_valid"]) for row in timeline],
            [(1, "pending", 0), (3, "verified", 1)],
        )

    def test_content_correction_does_not_retroactively_change_pinned_orders(self) -> None:
        self._ingest_all()
        # ORD-90/91/93 钉住 revision 1；day4 出现 revision 2 后，核验所用承诺仍是 CL-20。
        for order_id in ("ORD-90", "ORD-91", "ORD-93"):
            for shipment in queries.order_shipments(self.conn, order_id):
                for check in queries.shipment_checks(self.conn, shipment["shipment_id"]):
                    self.assertEqual(check["post_revision"], 1)
                    self.assertEqual(check["claim_id"], "CL-20")
        # 新订单 ORD-94 钉住 revision 2，关联的是新承诺 CL-21。
        checks = queries.shipment_checks(self.conn, "SHIP-56")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["claim_id"], "CL-21")
        self.assertEqual(checks[0]["post_revision"], 2)

    def test_posts_query_lists_versions_and_claims(self) -> None:
        self._ingest_all()
        rows = queries.post_versions(self.conn, "POST-4")
        revisions = {(row["revision"], row["claim_id"], row["claim_value"]) for row in rows}
        self.assertIn((1, "CL-20", "REGION-A"), revisions)
        self.assertIn((2, "CL-21", "REGION-A"), revisions)

    def test_split_shipment_order_aggregates_to_worst_state(self) -> None:
        self._ingest_all()
        # ORD-93 拆为 SHIP-54（LOT-31 符合）与 SHIP-55（LOT-32 冲突）。
        shipments = queries.order_shipments(self.conn, "ORD-93")
        self.assertEqual(len(shipments), 2)
        states = set()
        for shipment in shipments:
            for check in queries.shipment_checks(self.conn, shipment["shipment_id"]):
                states.add(check["conformance_state"])
        self.assertEqual(states, {CONFORMING, CONTRADICTED})
        trace = queries.order_trace(self.conn, "ORD-93")
        self.assertEqual(trace["overall_state"], CONTRADICTED)
        self.assertEqual(trace["shipment_count"], 2)

    def test_invalid_certificate_keeps_harvest_claim_pending_then_flips(self) -> None:
        self._ingest_day(DAY_DIRS[0])
        self._ingest_day(DAY_DIRS[1])
        # day2：LOT-33 corrected，采收日 2026-07-01 在窗口外，但证据无效 -> 待核实。
        checks_day2 = queries.shipment_checks(self.conn, "SHIP-53")
        self.assertEqual(len(checks_day2), 1)
        self.assertEqual(checks_day2[0]["conformance_state"], PENDING)
        self.assertEqual(checks_day2[0]["reason_code"], "evidence_not_verified")

        self._ingest_day(DAY_DIRS[2])
        self._ingest_day(DAY_DIRS[3])  # day4：LOT-33 凭证 verified
        checks_day4 = queries.shipment_checks(self.conn, "SHIP-53")
        self.assertEqual(checks_day4[0]["conformance_state"], CONTRADICTED)
        self.assertEqual(checks_day4[0]["reason_code"], "harvest_outside_window")
        # 替代关系保留在发货事实上。
        shipment = queries.order_shipments(self.conn, "ORD-92")[0]
        self.assertEqual(shipment["replaces_shipment"], "SHIP-50")
        self.assertEqual(shipment["aftercare_state"], "substitute")

    def test_evidence_applicability_covers_lots_and_using_orders(self) -> None:
        self._ingest_all()
        rows = {row["certificate_ref"]: row for row in queries.evidence_applicability(self.conn)}
        self.assertEqual(rows["CERT-11"]["evidence_valid"], 1)
        self.assertEqual(rows["CERT-11"]["lot_id"], "LOT-31")
        self.assertIn("ORD-90", rows["CERT-11"]["used_by_orders"].split(","))
        # 最终全部凭证都已 verified；无效状态的历史仍可在 lot_timeline 追溯。
        self.assertTrue(all(row["evidence_valid"] == 1 for row in rows.values()))

    def test_order_status_history_records_every_batch(self) -> None:
        self._ingest_all()
        timeline = queries.order_status_timeline(self.conn, "ORD-91")
        states = [(row["batch_id"], row["overall_state"]) for row in timeline]
        self.assertEqual(states, [(1, PENDING), (2, PENDING), (3, CONTRADICTED), (4, CONTRADICTED)])

    def test_missing_source_file_is_skipped_not_fatal(self) -> None:
        work = Path(self.tmp.name) / "partial"
        shutil.copytree(EXAMPLES / DAY_DIRS[0], work)
        (work / "shipments.csv").unlink()
        stats = ingest_delivery(self.conn, work, DELIVERED_AT[DAY_DIRS[0]])
        self.assertIn("shipments.csv", stats.missing_files)
        rebuild(self.conn)
        # 订单已入库但无发货 -> 整体待核实。
        trace = queries.order_trace(self.conn, "ORD-90")
        self.assertEqual(trace["overall_state"], PENDING)
        self.assertEqual(trace["unshipped_count"], 1)


class IngestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "w.sqlite3")
        self.addCleanup(self.conn.close)

    def _write(self, filename: str, header: str, rows: list[str]) -> Path:
        directory = Path(self.tmp.name) / "delivery"
        directory.mkdir(exist_ok=True)
        (directory / filename).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return directory

    def test_missing_required_column_raises(self) -> None:
        directory = self._write(
            "lot_records.csv",
            "record_id,lot_id,product_id,record_state",
            ["lot_records-01,LOT-1,SKU-1,verified"],
        )
        with self.assertRaises(IngestError):
            ingest_delivery(self.conn, directory)

    def test_duplicate_record_id_raises(self) -> None:
        directory = self._write(
            "orders.csv",
            "record_id,order_id,post_id,post_revision,product_id,placed_at,order_state",
            [
                "orders-01,ORD-1,POST-1,1,SKU-1,2026-07-01T00:00:00Z,confirmed",
                "orders-01,ORD-2,POST-1,1,SKU-1,2026-07-02T00:00:00Z,confirmed",
            ],
        )
        with self.assertRaises(IngestError):
            ingest_delivery(self.conn, directory)

    def test_unknown_directory_raises(self) -> None:
        with self.assertRaises(IngestError):
            ingest_delivery(self.conn, Path(self.tmp.name) / "nope")


class MissingLotLinkTests(unittest.TestCase):
    """构造发货缺失 lot_id 的场景，验证不会按 SKU 推定产地。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "w.sqlite3")
        self.addCleanup(self.conn.close)
        self.directory = Path(self.tmp.name) / "delivery"
        self.directory.mkdir()

    def _write(self, filename: str, header: str, rows: list[str]) -> None:
        (self.directory / filename).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")

    def test_shipment_without_lot_id_stays_pending(self) -> None:
        self._write(
            "content_claims.csv",
            "record_id,claim_id,post_id,revision,product_id,claim_kind,claim_value,published_at",
            ["content_claims-01,CL-1,POST-1,1,SKU-1,origin,REGION-A,2026-07-01T00:00:00Z"],
        )
        self._write(
            "lot_records.csv",
            "record_id,lot_id,supplier_id,product_id,origin_region,harvest_date,certificate_ref,record_state",
            ["lot_records-01,LOT-1,FARM-1,SKU-1,REGION-A,2026-06-01,CERT-1,verified"],
        )
        self._write(
            "orders.csv",
            "record_id,order_id,post_id,post_revision,product_id,placed_at,order_state",
            ["orders-01,ORD-1,POST-1,1,SKU-1,2026-07-01T01:00:00Z,confirmed"],
        )
        self._write(
            "shipments.csv",
            "record_id,shipment_id,order_id,product_id,lot_id,shipped_at,delivered_at,replaces_shipment,aftercare_state",
            ["shipments-01,SHIP-1,ORD-1,SKU-1,,2026-07-02T00:00:00Z,,,complete"],
        )
        ingest_delivery(self.conn, self.directory, "2026-07-02T00:00:00Z")
        rebuild(self.conn)
        checks = queries.shipment_checks(self.conn, "SHIP-1")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["conformance_state"], PENDING)
        self.assertEqual(checks[0]["reason_code"], "missing_lot_link")
        # 同 SKU 的已验证批次不得被自动关联到这笔无批次发货。
        self.assertIsNone(checks[0]["lot_id"])


if __name__ == "__main__":
    unittest.main()
