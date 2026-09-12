"""核算服务：所有数量与金额计算集中在这里，全部使用 Decimal。

规则约定：
- 应产数量 = 领料数 × 转化比例；约定损耗 = 应产 × 约定损耗率。
- 报工只作差异对照，永不进入可结算数量。
- 收货拆 合格/返工/报废；返工回厂的合格数计入结算，返工中报废计入报废。
- 可结算数 = min(累计合格, 应产数量)；超出应产的部分记为超交差异，不结算。
- 超损耗扣款 = max(0, 总报废 - 约定损耗) / 转化比例 × 原料单价（折回原料计费）。
- 超期扣款按交付节点逐段计算：每个节点只对"新增逾期未交数量"扣一次，
  部分交付时未迟交的部分不扣，已扣过的数量在后续节点不重复扣。
"""
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    ContractPriceVersion,
    ProcessingBatch,
    QuantityLedgerEntry as L,
    Receipt,
    Settlement,
    SettlementLine,
    WorkReport,
)

ZERO = Decimal("0")
Q3 = Decimal("0.001")
CENT = Decimal("0.01")


def q3(v):
    return Decimal(v).quantize(Q3, rounding=ROUND_HALF_UP)


def money(v):
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


class DomainError(Exception):
    """业务规则校验失败，视图层转成 400。"""


def _sum(qs, field):
    return qs.aggregate(s=Sum(field))["s"] or ZERO


# ---------------------------------------------------------------- 过账

def post_issue(batch):
    """领料过账：批次建立时把领料数写入数量流水（幂等）。"""
    if L.objects.filter(batch=batch, entry_type=L.ISSUE).exists():
        return
    L.objects.create(
        batch=batch, entry_type=L.ISSUE, qty=batch.issued_qty,
        ref_type="ProcessingBatch", ref_id=batch.id, memo=f"委外领料 {batch.code}",
    )


def post_work_report(*, batch, supplier_report_no, qty, reported_at):
    """报工入账（幂等：同一回传号重复提交返回已有记录）。"""
    existing = WorkReport.objects.filter(supplier_report_no=supplier_report_no).first()
    if existing:
        return existing, False
    with transaction.atomic():
        report = WorkReport.objects.create(
            batch=batch, supplier_report_no=supplier_report_no, qty=qty, reported_at=reported_at
        )
        L.objects.create(
            batch=batch, entry_type=L.REPORT, qty=qty,
            ref_type="WorkReport", ref_id=report.id, memo=f"报工回传 {supplier_report_no}（备查）",
        )
    return report, True


def resolve_price_version(contract, on_date):
    return (
        ContractPriceVersion.objects.filter(contract=contract, effective_from__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def post_receipt(*, batch, supplier_receipt_no, kind, received_at,
                 qualified_qty, rework_qty=ZERO, scrap_qty=ZERO):
    """收货检验入账。幂等 + 规则校验，全部在一个事务里。"""
    existing = Receipt.objects.filter(supplier_receipt_no=supplier_receipt_no).first()
    if existing:  # 重复回传：直接返回原单，不重复入账
        return existing, False
    if batch.locked:
        raise DomainError(f"批次 {batch.code} 已结算锁定，拒绝收货入账")
    if kind == Receipt.REWORK_RETURN:
        outstanding = batch_quantities(batch)["rework_outstanding"]
        if qualified_qty + scrap_qty > outstanding:
            raise DomainError(
                f"返工回厂数量 {qualified_qty + scrap_qty} 超出在返数量 {outstanding}"
            )
    elif rework_qty + scrap_qty + qualified_qty <= ZERO:
        raise DomainError("收货数量必须大于 0")

    price_version = resolve_price_version(batch.contract, received_at)
    if price_version is None:
        raise DomainError(f"{received_at} 之前合同没有生效价版，无法入账")

    with transaction.atomic():
        receipt = Receipt.objects.create(
            batch=batch, supplier_receipt_no=supplier_receipt_no, kind=kind,
            received_at=received_at, qualified_qty=qualified_qty,
            rework_qty=rework_qty, scrap_qty=scrap_qty, price_version=price_version,
        )
        entries = []
        if kind == Receipt.REWORK_RETURN:
            if qualified_qty:
                entries.append((L.REWORK_RETURN_QUALIFIED, qualified_qty))
            if scrap_qty:
                entries.append((L.REWORK_RETURN_SCRAP, scrap_qty))
        else:
            if qualified_qty:
                entries.append((L.RECEIVE_QUALIFIED, qualified_qty))
            if rework_qty:
                entries.append((L.RECEIVE_REWORK, rework_qty))
            if scrap_qty:
                entries.append((L.RECEIVE_SCRAP, scrap_qty))
        for entry_type, qty in entries:
            L.objects.create(
                batch=batch, entry_type=entry_type, qty=qty,
                ref_type="Receipt", ref_id=receipt.id,
                memo=f"收货回传 {supplier_receipt_no}",
            )
    return receipt, True


# ---------------------------------------------------------------- 数量去向

def batch_quantities(batch):
    """一个批次的数量去向。所有数都能回到 领料 × 转化比例 × 约定损耗。"""
    contract = batch.contract
    issued = batch.issued_qty
    expected = q3(issued * contract.conversion_ratio)

    receipts = Receipt.objects.filter(batch=batch)
    normal = receipts.filter(kind=Receipt.NORMAL)
    returned = receipts.filter(kind=Receipt.REWORK_RETURN)

    qualified = _sum(normal, "qualified_qty") + _sum(returned, "qualified_qty")
    rework_in = _sum(normal, "rework_qty")
    scrap_normal = _sum(normal, "scrap_qty")
    scrap_rework = _sum(returned, "scrap_qty")
    scrap_total = scrap_normal + scrap_rework
    rework_outstanding = rework_in - _sum(returned, "qualified_qty") - scrap_rework

    reported = _sum(WorkReport.objects.filter(batch=batch), "qty")

    allowed_loss = q3(expected * contract.agreed_loss_rate)
    over_loss = max(ZERO, scrap_total - allowed_loss)
    settleable = min(qualified, expected)          # 规则允许的结算数量
    over_delivered = max(ZERO, qualified - expected)  # 超交：不结算，列差异
    in_process = expected - qualified - scrap_total - rework_outstanding

    return {
        "issued_qty": issued,
        "conversion_ratio": contract.conversion_ratio,
        "expected_output": expected,
        "reported_qty": reported,                    # 供应商声称（备查）
        "qualified_qty": qualified,
        "rework_outstanding": rework_outstanding,
        "scrap_total": scrap_total,
        "in_process_qty": in_process,                # 在制/未回 = 应产 - 已交代数
        "agreed_loss_rate": contract.agreed_loss_rate,
        "allowed_loss_qty": allowed_loss,
        "over_loss_qty": over_loss,
        "settleable_qty": settleable,
        "over_delivered_qty": over_delivered,
    }


def ledger_quantities(batch):
    """只从数量流水重算的账面——用于独立核对，不许引用 batch_quantities。"""
    sums = {
        row["entry_type"]: row["s"]
        for row in L.objects.filter(batch=batch).values("entry_type").annotate(s=Sum("qty"))
    }
    g = lambda t: sums.get(t, ZERO)
    qualified = g(L.RECEIVE_QUALIFIED) + g(L.REWORK_RETURN_QUALIFIED)
    scrap = g(L.RECEIVE_SCRAP) + g(L.REWORK_RETURN_SCRAP)
    rework_outstanding = g(L.RECEIVE_REWORK) - g(L.REWORK_RETURN_QUALIFIED) - g(L.REWORK_RETURN_SCRAP)
    return {
        "issued_qty": g(L.ISSUE),
        "reported_qty": g(L.REPORT),
        "qualified_qty": qualified,
        "scrap_total": scrap,
        "rework_outstanding": rework_outstanding,
    }


# ---------------------------------------------------------------- 超期扣款

def compute_late_penalty(contract, as_of=None):
    """按交付节点逐段计算。返回 (总额, 明细)。

    - 节点短交数 = max(0, 累计计划 - 到期日累计合格)
    - 本节点新扣数量 = max(0, 短交数 - 之前节点已扣数量)  ← 部分交付不整单重复扣罚
    - 迟交天数 = 累计合格达到本节点计划之日 - 到期日；结算时仍未交齐按 as_of 计
    """
    as_of = as_of or date.today()
    timeline = []
    cum = ZERO
    qs = (
        Receipt.objects.filter(batch__contract=contract)
        .values("received_at")
        .annotate(q=Sum("qualified_qty"))
        .order_by("received_at")
    )
    for row in qs:
        cum += row["q"]
        timeline.append((row["received_at"], cum))

    def cum_by(d):
        total = ZERO
        for day, q in timeline:
            if day <= d:
                total = q
        return total

    def cleared_on(target):
        for day, q in timeline:
            if q >= target:
                return day
        return None

    details, total, penalized_cum = [], ZERO, ZERO
    for ms in contract.milestones.all():
        shortfall = max(ZERO, ms.planned_qty - cum_by(ms.due_date))
        new_qty = max(ZERO, shortfall - penalized_cum)
        penalized_cum = max(penalized_cum, shortfall)
        if new_qty > ZERO:
            clear_date = cleared_on(ms.planned_qty) or as_of
            days = max(0, (clear_date - ms.due_date).days)
            amount = min(money(new_qty * days * ms.penalty_rate_per_day), money(ms.penalty_cap))
            if amount > ZERO:
                details.append({
                    "milestone_id": ms.id,
                    "due_date": ms.due_date.isoformat(),
                    "planned_qty": str(ms.planned_qty),
                    "shortfall_qty": str(shortfall),
                    "newly_penalized_qty": str(new_qty),
                    "days_late": days,
                    "amount": str(amount),
                })
                total += amount
    return money(total), details


# ---------------------------------------------------------------- 结算

def build_settlement_preview(batch, as_of=None):
    """结算试算：合格加工费 - 超损耗扣款 - 超期扣款。供预览与确认共用。"""
    as_of = as_of or date.today()
    contract = batch.contract
    q = batch_quantities(batch)

    lines = []
    remaining = q["settleable_qty"]
    for r in Receipt.objects.filter(batch=batch, qualified_qty__gt=ZERO).order_by("received_at", "id"):
        take = min(r.qualified_qty, remaining)
        if take <= ZERO:
            break
        remaining -= take
        amount = money(take * r.price_version.unit_price)
        lines.append({
            "line_type": SettlementLine.QUALIFIED,
            "receipt_id": r.id,
            "receipt_no": r.supplier_receipt_no,
            "qty": str(take),
            "unit_price": str(r.price_version.unit_price),
            "price_effective_from": r.price_version.effective_from.isoformat(),
            "amount": str(amount),
            "note": f"合格 {take} × 价版 {r.price_version.effective_from}",
        })
    qualified_amount = sum((Decimal(l["amount"]) for l in lines), ZERO)

    over_loss_amount = ZERO
    if q["over_loss_qty"] > ZERO:
        raw_qty = q3(q["over_loss_qty"] / contract.conversion_ratio)
        over_loss_amount = money(raw_qty * contract.raw_material.unit_cost)
        lines.append({
            "line_type": SettlementLine.OVER_LOSS,
            "qty": str(q["over_loss_qty"]),
            "unit_price": str(contract.raw_material.unit_cost),
            "amount": str(-over_loss_amount),
            "note": f"报废 {q['scrap_total']} 超约定损耗 {q['allowed_loss_qty']}，"
                    f"折原料 {raw_qty} {contract.raw_material.unit} 扣款",
        })

    late_total, late_details = compute_late_penalty(contract, as_of)
    for d in late_details:
        lines.append({
            "line_type": SettlementLine.LATE_PENALTY,
            "milestone_id": d["milestone_id"],
            "qty": d["newly_penalized_qty"],
            "unit_price": "0",
            "amount": str(-Decimal(d["amount"])),
            "note": f"节点 {d['due_date']} 短交 {d['shortfall_qty']}，"
                    f"新扣 {d['newly_penalized_qty']} × {d['days_late']} 天",
        })

    total = money(qualified_amount - over_loss_amount - late_total)
    preview = {
        "batch": batch.code,
        "contract": contract.code,
        "as_of": as_of.isoformat(),
        "quantities": {k: str(v) for k, v in q.items()},
        "diff": {  # 双方差异：供应商报工声称 vs 我方检验合格
            "supplier_claimed_qty": str(q["reported_qty"]),
            "our_qualified_qty": str(q["qualified_qty"]),
            "diff_qty": str(q["reported_qty"] - q["qualified_qty"]),
            "our_scrap_qty": str(q["scrap_total"]),
            "our_rework_outstanding_qty": str(q["rework_outstanding"]),
        },
        "lines": lines,
        "qualified_amount": str(money(qualified_amount)),
        "over_loss_amount": str(money(over_loss_amount)),
        "late_penalty_amount": str(money(late_total)),
        "total_amount": str(total),
    }
    preview["calc_hash"] = calc_hash(preview)
    return preview


def calc_hash(preview):
    payload = json.dumps(
        {k: v for k, v in preview.items() if k != "calc_hash"},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def confirm_settlement(batch, party, as_of=None):
    """双方确认。任一方确认时若计算结果已变化，则重置双方确认（差异需重新认可）。
    双方都确认后冻结快照并锁定批次。"""
    preview = build_settlement_preview(batch, as_of)
    with transaction.atomic():
        settlement = Settlement.objects.filter(batch=batch).select_for_update().first()
        if settlement and settlement.status == Settlement.CONFIRMED:
            raise DomainError("结算已确认冻结，不能重复确认")
        if settlement is None or settlement.calc_hash != preview["calc_hash"]:
            if settlement:
                settlement.lines.all().delete()
            else:
                settlement = Settlement.objects.create(
                    batch=batch, code=f"ST-{batch.code}", calc_hash=""
                )
            settlement.calc_hash = preview["calc_hash"]
            settlement.confirmed_by_supplier = False
            settlement.confirmed_by_us = False
            settlement.save()
            for line in preview["lines"]:
                SettlementLine.objects.create(
                    settlement=settlement,
                    line_type=line["line_type"],
                    receipt_id=line.get("receipt_id"),
                    milestone_id=line.get("milestone_id"),
                    qty=Decimal(line["qty"]),
                    unit_price=Decimal(line["unit_price"]),
                    amount=Decimal(line["amount"]),
                    note=line["note"],
                )
        if party == "supplier":
            settlement.confirmed_by_supplier = True
        elif party == "us":
            settlement.confirmed_by_us = True
        else:
            raise DomainError("party 必须是 supplier 或 us")

        if settlement.both_confirmed:
            settlement.status = Settlement.CONFIRMED
            settlement.confirmed_at = timezone.now()
            settlement.snapshot = preview  # 冻结计算快照
            batch.locked = True
            batch.save(update_fields=["locked"])
            L.objects.create(
                batch=batch, entry_type=L.SETTLE_LOCK, qty=ZERO,
                ref_type="Settlement", ref_id=settlement.id,
                memo=f"结算 {settlement.code} 双方确认，快照 {preview['calc_hash'][:12]}",
            )
        settlement.save()
    return settlement, preview
