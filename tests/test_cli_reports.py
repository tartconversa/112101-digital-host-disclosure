from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import _path  # noqa: F401

from origin_fulfillment.cli import main
from origin_fulfillment.db import connect
from origin_fulfillment.ingest import ingest_delivery
from origin_fulfillment.integrate import rebuild
from origin_fulfillment.reports import render_json, render_table

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "cli.sqlite3")

    def _run(self, *argv: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.db, *argv])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_full_cli_flow(self) -> None:
        output = self._run("ingest", "--dir", str(SOURCES), "--at", "2026-07-10T00:00:00Z")
        self.assertIn("已创建摄入批次 #1", output)

        evidence = self._run("evidence")
        self.assertIn("CERT-11", evidence)
        self.assertIn("LOT-31", evidence)

        posts = self._run("posts", "--post", "POST-4")
        self.assertIn("CL-20", posts)

        trace = self._run("trace", "ORD-91")
        self.assertIn("SHIP-52", trace)
        self.assertIn("pending_verification", trace)  # LOT-32 凭证 pending

        history = self._run("history", "--shipment", "SHIP-52")
        self.assertIn("evidence_not_verified", history)

        orders = self._run("orders")
        self.assertIn("ORD-90", orders)

    def test_json_output_is_parseable(self) -> None:
        self._run("ingest", "--dir", str(SOURCES), "--at", "2026-07-10T00:00:00Z")
        output = self._run("--json", "evidence")
        payload = json.loads(output)
        self.assertIsInstance(payload, list)
        self.assertTrue(any(row["certificate_ref"] == "CERT-11" for row in payload))

    def test_unknown_order_exit_code(self) -> None:
        self._run("ingest", "--dir", str(SOURCES), "--at", "2026-07-10T00:00:00Z")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.db, "trace", "ORD-NOPE"])
        self.assertEqual(code, 2)


class ReportsTests(unittest.TestCase):
    def test_render_table_handles_cjk_width(self) -> None:
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
        conn.execute("INSERT INTO t VALUES ('产地', 'REGION-A')")
        rows = conn.execute("SELECT * FROM t").fetchall()
        output = render_table(rows)
        body = [line for line in output.splitlines() if "产地" in line][0]
        # 中文按两个显示宽度填充后，表格竖线仍能对齐（同一行两个单元格各有结尾竖线）。
        self.assertTrue(body.startswith("|"))
        self.assertTrue(body.endswith("|"))
        self.assertEqual(body.count("|"), 3)

    def test_empty_table_hint(self) -> None:
        self.assertEqual(render_table([]), "（无记录）")

    def test_render_json_roundtrip(self) -> None:
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t VALUES ('凭证')")
        payload = json.loads(render_json(conn.execute("SELECT * FROM t").fetchall()))
        self.assertEqual(payload, [{"a": "凭证"}])


class IngestFromProvidedSourcesTests(unittest.TestCase):
    """基线 sources/ 目录（题目随附快照）的核验口径。"""

    def test_baseline_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "base.sqlite3")
            conn = connect(db_path)
            self.addCleanup(conn.close)
            ingest_delivery(conn, SOURCES, "2026-07-10T00:00:00Z")
            rebuild(conn)

            from origin_fulfillment import queries
            self.assertEqual(
                {r["conformance_state"] for r in queries.shipment_checks(conn, "SHIP-51")},
                {"conforming"},
            )
            self.assertEqual(
                {r["conformance_state"] for r in queries.shipment_checks(conn, "SHIP-52")},
                {"pending_verification"},
            )
            self.assertEqual(
                {r["conformance_state"] for r in queries.shipment_checks(conn, "SHIP-53")},
                {"pending_verification"},
            )


if __name__ == "__main__":
    unittest.main()
