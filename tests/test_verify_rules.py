from __future__ import annotations

import unittest
from datetime import date

import _path  # noqa: F401  # 导入路径引导

from origin_fulfillment.schema import CONFORMING, CONTRADICTED, PENDING
from origin_fulfillment.timeutil import parse_harvest_window, parse_instant
from origin_fulfillment.verify import Claim, ShipmentFacts, aggregate_order, evaluate_claim


class TimeUtilTests(unittest.TestCase):
    def test_z_suffix_is_utc(self) -> None:
        parsed = parse_instant("2026-07-05T10:00:00Z")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertEqual(parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-07-05T10:00:00Z")

    def test_offset_converted_to_utc(self) -> None:
        parsed = parse_instant("2026-07-05T18:00:00+08:00")
        self.assertEqual(parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-07-05T10:00:00Z")

    def test_harvest_window_inclusive_bounds(self) -> None:
        start, end = parse_harvest_window("2026-06-20/2026-06-30")
        self.assertEqual((start, end), (date(2026, 6, 20), date(2026, 6, 30)))

    def test_invalid_window_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_harvest_window("2026-06-30/2026-06-20")
        with self.assertRaises(ValueError):
            parse_harvest_window("2026-06-30")


def _origin_claim(value: str = "REGION-A") -> Claim:
    return Claim(claim_id="CL-1", claim_kind="origin", claim_value=value)


def _window_claim() -> Claim:
    return Claim(claim_id="CL-2", claim_kind="harvest_window", claim_value="2026-06-20/2026-06-30")


class OriginVerifyTests(unittest.TestCase):
    def test_verified_matching_origin_is_conforming(self) -> None:
        facts = ShipmentFacts("S1", "SKU-8", "LOT-31", "SKU-8", "REGION-A", "2026-06-25", "verified")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, CONFORMING)
        self.assertEqual(code, "origin_match")

    def test_verified_conflicting_origin_is_contradicted(self) -> None:
        facts = ShipmentFacts("S1", "SKU-8", "LOT-32", "SKU-8", "REGION-B", "2026-06-27", "verified")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, CONTRADICTED)
        self.assertEqual(code, "origin_mismatch")

    def test_pending_certificate_never_contradicted_even_when_regions_differ(self) -> None:
        # 关键口径：凭证无效时即使产地名称不同，也只能保留待核实，不能判不符合。
        facts = ShipmentFacts("S1", "SKU-8", "LOT-32", "SKU-8", "REGION-B", "2026-06-27", "pending")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "evidence_not_verified")

    def test_corrected_certificate_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-07-01", "corrected")
        state, code, _ = evaluate_claim(_window_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "evidence_not_verified")

    def test_missing_lot_link_is_pending_not_inferred_from_sku(self) -> None:
        # 关键口径：不得按 SKU 推定产地；缺失批次关系必须待核实。
        facts = ShipmentFacts("S1", "SKU-8", None)
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "missing_lot_link")

    def test_unknown_lot_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-8", "LOT-99")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "unknown_lot")

    def test_missing_origin_field_even_verified_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-8", "LOT-31", "SKU-8", None, "2026-06-25", "verified")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "missing_origin_field")

    def test_lot_product_mismatch_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-8", "LOT-77", "SKU-9", "REGION-A", None, "verified")
        state, code, _ = evaluate_claim(_origin_claim(), facts)
        self.assertEqual(state, PENDING)
        self.assertEqual(code, "lot_product_mismatch")


class HarvestWindowVerifyTests(unittest.TestCase):
    def test_harvest_inside_window_is_conforming(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-06-25", "verified")
        state, code, _ = evaluate_claim(_window_claim(), facts)
        self.assertEqual((state, code), (CONFORMING, "harvest_in_window"))

    def test_harvest_on_boundary_is_conforming(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-06-30", "verified")
        state, _, _ = evaluate_claim(_window_claim(), facts)
        self.assertEqual(state, CONFORMING)

    def test_harvest_outside_window_is_contradicted_only_with_valid_evidence(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-07-01", "verified")
        state, code, _ = evaluate_claim(_window_claim(), facts)
        self.assertEqual((state, code), (CONTRADICTED, "harvest_outside_window"))

    def test_unparseable_window_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-06-25", "verified")
        bad_claim = Claim("CL-2", "harvest_window", "not-a-window")
        state, code, _ = evaluate_claim(bad_claim, facts)
        self.assertEqual((state, code), (PENDING, "unparseable_claim_window"))

    def test_unsupported_kind_is_pending(self) -> None:
        facts = ShipmentFacts("S1", "SKU-3", "LOT-33", "SKU-3", "REGION-C", "2026-06-25", "verified")
        claim = Claim("CL-9", "organic_cert", "YES")
        state, code, _ = evaluate_claim(claim, facts)
        self.assertEqual((state, code), (PENDING, "unsupported_claim_kind"))


class AggregationTests(unittest.TestCase):
    def test_contradiction_dominates(self) -> None:
        self.assertEqual(
            aggregate_order([CONFORMING, CONTRADICTED, PENDING], 0), CONTRADICTED
        )

    def test_pending_dominates_conforming(self) -> None:
        self.assertEqual(aggregate_order([CONFORMING, PENDING], 0), PENDING)

    def test_unshipped_order_is_pending_even_without_checks(self) -> None:
        self.assertEqual(aggregate_order([], 1), PENDING)

    def test_all_conforming(self) -> None:
        self.assertEqual(aggregate_order([CONFORMING, CONFORMING], 0), CONFORMING)


if __name__ == "__main__":
    unittest.main()
