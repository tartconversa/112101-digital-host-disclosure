"""视频承诺与实际发货批次的核验规则（纯函数，便于单测）。

关键原则（与业务口径一致）：

1. 名称相同不等于来源相同：核验基于发货记录中的实际 ``lot_id``，
   绝不按商品 SKU 推定产地；
2. 凭证有效性集中在 :data:`EVIDENCE_VERIFIED_STATE`：只有 ``verified``
   状态的批次记录可支撑承诺，``pending`` / ``corrected`` 等一律视为
   暂无有效凭证，结论只能是 ``pending_verification``；
3. 缺失批次关系、批次未知、承诺窗口解析失败等任何证据缺口，
   都保留 ``pending_verification``，不得填成符合或不符合；
4. 只有证据齐全且与承诺直接冲突时才判定 ``contradicted``；
5. 后来的内容更正不在这里处理——订单在整合层已钉住下单时的视频版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .schema import CONFORMING, CONTRADICTED, EVIDENCE_VERIFIED_STATE, PENDING
from .timeutil import parse_harvest_window


@dataclass(frozen=True)
class Claim:
    claim_id: str
    claim_kind: str
    claim_value: str


@dataclass(frozen=True)
class ShipmentFacts:
    shipment_id: str
    product_id: str
    lot_id: str | None            # None：发货缺失批次关系
    lot_product_id: str | None = None
    origin_region: str | None = None
    harvest_date: str | None = None
    lot_state: str | None = None  # None：批次在批次表中未知


def evaluate_claim(claim: Claim, facts: ShipmentFacts) -> tuple[str, str, str]:
    """对单笔发货的单条承诺返回 ``(状态, 原因码, 中文说明)``。"""
    # 商品维度先校验：SKU 相同本就不是产地证据，商品不一致更不能比对。
    if facts.lot_product_id is not None and facts.lot_product_id != facts.product_id:
        return (
            PENDING,
            "lot_product_mismatch",
            f"发货批次商品 {facts.lot_product_id} 与订单商品 {facts.product_id} 不一致，待核实",
        )

    if facts.lot_id is None:
        return (
            PENDING,
            "missing_lot_link",
            "发货记录缺失批次关系，无法溯源实际产地，保留待核实",
        )
    if facts.lot_state is None:
        return (
            PENDING,
            "unknown_lot",
            f"批次 {facts.lot_id} 在批次凭证表中无记录，保留待核实",
        )
    if facts.lot_state != EVIDENCE_VERIFIED_STATE:
        return (
            PENDING,
            "evidence_not_verified",
            f"批次 {facts.lot_id} 凭证状态为 {facts.lot_state}，非有效凭证，保留待核实",
        )

    if claim.claim_kind == "origin":
        if not facts.origin_region:
            return (
                PENDING,
                "missing_origin_field",
                f"批次 {facts.lot_id} 缺少产地字段，无法比对产地承诺，保留待核实",
            )
        if facts.origin_region == claim.claim_value:
            return (
                CONFORMING,
                "origin_match",
                f"有效凭证产地 {facts.origin_region} 与承诺 {claim.claim_value} 一致",
            )
        return (
            CONTRADICTED,
            "origin_mismatch",
            f"有效凭证产地 {facts.origin_region} 与承诺 {claim.claim_value} 冲突",
        )

    if claim.claim_kind == "harvest_window":
        try:
            start, end = parse_harvest_window(claim.claim_value)
        except ValueError:
            return (
                PENDING,
                "unparseable_claim_window",
                f"承诺采收窗口无法解析: {claim.claim_value!r}，保留待核实",
            )
        if not facts.harvest_date:
            return (
                PENDING,
                "missing_harvest_date",
                f"批次 {facts.lot_id} 缺少采收日期，保留待核实",
            )
        try:
            harvested = date.fromisoformat(facts.harvest_date)
        except ValueError:
            return (
                PENDING,
                "unparseable_harvest_date",
                f"批次采收日期无法解析: {facts.harvest_date!r}，保留待核实",
            )
        if start <= harvested <= end:
            return (
                CONFORMING,
                "harvest_in_window",
                f"采收日期 {facts.harvest_date} 落在承诺窗口 {start} 至 {end} 内",
            )
        return (
            CONTRADICTED,
            "harvest_outside_window",
            f"采收日期 {facts.harvest_date} 落在承诺窗口 {start} 至 {end} 之外",
        )

    return (
        PENDING,
        "unsupported_claim_kind",
        f"暂不支持的承诺类型 {claim.claim_kind}，保留待核实",
    )


def no_claim_check(reason: str = "no_pinned_claim") -> tuple[str, str, str]:
    """订单钉住版本没有可核验承诺时使用的占位结论。"""
    details = {
        "no_pinned_claim": "用户下单时的视频版本没有可核验的公开承诺，保留待核实",
    }
    return PENDING, reason, details.get(reason, "无对应承诺，保留待核实")


def aggregate_order(check_states: list[str], unshipped: int) -> str:
    """汇总订单整体状态：冲突优先，其次待核实，全部符合才算符合。"""
    if CONTRADICTED in check_states:
        return CONTRADICTED
    if unshipped > 0 or PENDING in check_states or not check_states:
        return PENDING
    if check_states and all(state == CONFORMING for state in check_states):
        return CONFORMING
    return PENDING


def evaluate_shipment(claim_rows: list[dict[str, Any]], facts: ShipmentFacts) -> list[dict[str, Any]]:
    """生成单笔发货对订单全部钉住承诺的核验行（无承诺时给一条占位行）。"""
    if not claim_rows:
        state, code, detail = no_claim_check()
        return [
            {
                "claim_id": None,
                "claim_kind": None,
                "claim_value": None,
                "state": state,
                "reason_code": code,
                "reason_detail": detail,
            }
        ]
    results: list[dict[str, Any]] = []
    for row in claim_rows:
        claim = Claim(
            claim_id=row["claim_id"],
            claim_kind=row["claim_kind"],
            claim_value=row["claim_value"],
        )
        state, code, detail = evaluate_claim(claim, facts)
        results.append(
            {
                "claim_id": claim.claim_id,
                "claim_kind": claim.claim_kind,
                "claim_value": claim.claim_value,
                "state": state,
                "reason_code": code,
                "reason_detail": detail,
            }
        )
    return results
