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
    LiabilityEntry,
    ProcessingBatch,
    QuantityLedgerEntry as L,
    Receipt,
    RecoveryClaim,
    Settlement,
    SettlementLine,
    TransferOrder,
    TransferReceipt,
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
    """一个批次的数量去向。所有数都能回到 领料 × 转化比例 × 约定损耗。

    转厂只移动实物保管权：转出数从"在返（原厂）"移到"在新厂"，
    合格回厂才计入合格数；二次报废单独列示并由原厂赔偿，
    不计入超损耗扣款基数（避免与赔偿重复扣）。
    """
    contract = batch.contract
    issued = batch.issued_qty
    expected = q3(issued * contract.conversion_ratio)

    receipts = Receipt.objects.filter(batch=batch)
    normal = receipts.filter(kind=Receipt.NORMAL)
    returned = receipts.filter(kind=Receipt.REWORK_RETURN)

    self_return_q = _sum(returned, "qualified_qty")
    scrap_normal = _sum(normal, "scrap_qty")
    scrap_rework = _sum(returned, "scrap_qty")
    rework_in = _sum(normal, "rework_qty")

    transfers = TransferOrder.objects.filter(batch=batch)
    transferred = _sum(transfers, "qty")
    t_receipts = TransferReceipt.objects.filter(transfer__batch=batch)
    t_qualified = _sum(t_receipts, "qualified_qty")
    t_scrap = _sum(t_receipts, "scrap_qty")
    at_supplier_b = transferred - t_qualified - t_scrap

    qualified = _sum(normal, "qualified_qty") + self_return_q + t_qualified
    scrap_chargeable = scrap_normal + scrap_rework          # 超损耗扣款基数
    rework_at_supplier = rework_in - self_return_q - scrap_rework - transferred

    reported = _sum(WorkReport.objects.filter(batch=batch), "qty")

    allowed_loss = q3(expected * contract.agreed_loss_rate)
    over_loss = max(ZERO, scrap_chargeable - allowed_loss)
    settleable = min(qualified, expected)          # 规则允许的结算数量
    over_delivered = max(ZERO, qualified - expected)  # 超交：不结算，列差异
    in_process = (expected - qualified - scrap_chargeable - t_scrap
                  - rework_at_supplier - at_supplier_b)

    return {
        "issued_qty": issued,
        "conversion_ratio": contract.conversion_ratio,
        "expected_output": expected,
        "reported_qty": reported,                    # 供应商声称（备查）
        "qualified_qty": qualified,
        "rework_outstanding": rework_at_supplier,    # 在返（实物在原厂）
        "transferred_out": transferred,
        "at_supplier_b": at_supplier_b,              # 实物在新厂
        "transfer_scrap_qty": t_scrap,               # 二次报废（原厂赔偿，不入损耗基数）
        "scrap_total": scrap_chargeable,
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
    qualified = g(L.RECEIVE_QUALIFIED) + g(L.REWORK_RETURN_QUALIFIED) + g(L.TRANSFER_RETURN_QUALIFIED)
    scrap = g(L.RECEIVE_SCRAP) + g(L.REWORK_RETURN_SCRAP)
    transferred = g(L.TRANSFER_OUT)
    at_supplier_b = transferred - g(L.TRANSFER_RETURN_QUALIFIED) - g(L.TRANSFER_RETURN_SCRAP)
    rework_outstanding = (g(L.RECEIVE_REWORK) - g(L.REWORK_RETURN_QUALIFIED)
                          - g(L.REWORK_RETURN_SCRAP) - transferred)
    return {
        "issued_qty": g(L.ISSUE),
        "reported_qty": g(L.REPORT),
        "qualified_qty": qualified,
        "scrap_total": scrap,
        "transfer_scrap_qty": g(L.TRANSFER_RETURN_SCRAP),
        "rework_outstanding": rework_outstanding,
        "at_supplier_b": at_supplier_b,
    }


# ---------------------------------------------------------------- 转厂与责任账

def post_transfer(*, batch, code, to_supplier, qty, transferred_at, rework_unit_price):
    """转厂：实物 A -> B。幂等；正在确认结算或已锁定的批次禁止转出。"""
    existing = TransferOrder.objects.filter(code=code).first()
    if existing:
        return existing, False
    if batch.locked:
        raise DomainError(f"批次 {batch.code} 已结算锁定，不能转出")
    if Settlement.objects.filter(batch=batch, status=Settlement.DRAFT).exists():
        raise DomainError(f"批次 {batch.code} 正在确认结算，不能再次转出")
    available = batch_quantities(batch)["rework_outstanding"]
    if qty > available:
        raise DomainError(f"转出数量 {qty} 超出原厂在返数量 {available}")
    version = resolve_price_version(batch.contract, transferred_at)
    with transaction.atomic():
        order = TransferOrder.objects.create(
            batch=batch, code=code, from_supplier=batch.contract.supplier,
            to_supplier=to_supplier, qty=qty, rework_unit_price=rework_unit_price,
            transferred_at=transferred_at, price_version=version,
        )
        L.objects.create(
            batch=batch, entry_type=L.TRANSFER_OUT, qty=qty,
            ref_type="TransferOrder", ref_id=order.id,
            memo=f"转厂 {code}：{order.from_supplier.name} → {to_supplier.name}（责任仍在原厂）",
        )
    return order, True


def _book_compensation(*, batch, price_version, amount):
    """按原合同版本的赔偿上限记账，返回 (应赔额, 超帽豁免额)。"""
    amount = money(amount)
    if amount <= ZERO:
        return ZERO, ZERO
    cap = price_version.compensation_cap if price_version else None
    if cap is None:
        return amount, ZERO
    used = _sum(
        LiabilityEntry.objects.filter(
            batch__contract=batch.contract,
            entry_type=LiabilityEntry.COMPENSATION,
            price_version=price_version,
        ),
        "amount",
    )
    booked = min(amount, max(ZERO, money(cap - used)))
    return money(booked), money(amount - booked)


def _create_claim(*, batch, supplier, amount, reason):
    seq = RecoveryClaim.objects.count() + 1
    return RecoveryClaim.objects.create(
        code=f"CL-{seq:04d}", batch=batch, supplier=supplier, amount=amount, reason=reason
    )


def post_transfer_receipt(*, transfer, receipt_no, received_at, qualified_qty, scrap_qty=ZERO):
    """新厂返工回厂（幂等）。合格计入批次合格数；二次报废由原合同版本上限内赔偿。
    返工费记原厂责任账（应付新厂、抵扣原厂），同一笔账驱动两侧，不会重复计。"""
    existing = TransferReceipt.objects.filter(receipt_no=receipt_no).first()
    if existing:
        return existing, False
    batch = transfer.batch
    contract = batch.contract
    returned = _sum(transfer.receipts.all(), "qualified_qty") + _sum(transfer.receipts.all(), "scrap_qty")
    remaining = transfer.qty - returned
    if qualified_qty + scrap_qty > remaining:
        raise DomainError(f"回厂数量 {qualified_qty + scrap_qty} 超出转厂未回数量 {remaining}")
    price_version = resolve_price_version(contract, received_at)
    if price_version is None:
        raise DomainError(f"{received_at} 之前合同没有生效价版，无法入账")
    rework_fee = money(qualified_qty * transfer.rework_unit_price)
    comp_raw = money(q3(scrap_qty / contract.conversion_ratio) * contract.raw_material.unit_cost)
    comp, waived = _book_compensation(
        batch=batch, price_version=transfer.price_version, amount=comp_raw
    )

    with transaction.atomic():
        tr = TransferReceipt.objects.create(
            transfer=transfer, receipt_no=receipt_no, received_at=received_at,
            qualified_qty=qualified_qty, scrap_qty=scrap_qty,
            price_version=price_version, rework_fee=rework_fee,
        )
        if qualified_qty:
            L.objects.create(batch=batch, entry_type=L.TRANSFER_RETURN_QUALIFIED,
                             qty=qualified_qty, ref_type="TransferReceipt", ref_id=tr.id,
                             memo=f"新厂回厂合格 {receipt_no}")
        if scrap_qty:
            L.objects.create(batch=batch, entry_type=L.TRANSFER_RETURN_SCRAP,
                             qty=scrap_qty, ref_type="TransferReceipt", ref_id=tr.id,
                             memo=f"二次报废 {receipt_no}（原厂责任）")
        new_entries = []
        if rework_fee > ZERO:
            new_entries.append(LiabilityEntry.objects.create(
                batch=batch, supplier=transfer.from_supplier,
                counter_supplier=transfer.to_supplier,
                entry_type=LiabilityEntry.REWORK_FEE, amount=rework_fee,
                price_version=transfer.price_version, transfer_receipt=tr,
                note=f"返工费 {qualified_qty}×{transfer.rework_unit_price} "
                     f"应付 {transfer.to_supplier.name}，由 {transfer.from_supplier.name} 承担",
            ))
        if comp > ZERO:
            note = f"二次报废 {scrap_qty} 件折原料赔偿（原合同版本上限内）"
            if waived > ZERO:
                note += f"，超帽豁免 {waived}"
            new_entries.append(LiabilityEntry.objects.create(
                batch=batch, supplier=transfer.from_supplier,
                entry_type=LiabilityEntry.COMPENSATION, amount=comp,
                price_version=transfer.price_version, transfer_receipt=tr, note=note,
            ))
        if batch.locked:  # 结算后回厂：不能改快照，直接形成追偿
            for e in new_entries:
                e.status = LiabilityEntry.CLAIMED
                e.claim = _create_claim(
                    batch=batch, supplier=e.supplier, amount=e.amount,
                    reason=f"结算后责任（{receipt_no}）：{e.note}",
                )
                e.save()
        if returned + qualified_qty + scrap_qty >= transfer.qty:
            transfer.status = TransferOrder.CLOSED
            transfer.save(update_fields=["status"])
    return tr, True


def post_liability_determination(*, batch, qty, reference_date, note):
    """结算后追加责任认定：批次已锁定，赔偿按原合同版本上限计算，
    直接形成独立追偿记录，不触碰已冻结的结算快照。"""
    if not batch.locked:
        raise DomainError("追加责任认定仅用于已结算锁定的批次")
    contract = batch.contract
    version = resolve_price_version(contract, reference_date)
    raw_amount = money(q3(qty / contract.conversion_ratio) * contract.raw_material.unit_cost)
    booked, waived = _book_compensation(batch=batch, price_version=version, amount=raw_amount)
    with transaction.atomic():
        entry = LiabilityEntry.objects.create(
            batch=batch, supplier=contract.supplier,
            entry_type=LiabilityEntry.COMPENSATION, amount=booked, price_version=version,
            note=f"追加责任认定 {qty} 件：{note}"
                 + (f"（超帽豁免 {waived}）" if waived > ZERO else ""),
        )
        claim = _create_claim(
            batch=batch, supplier=entry.supplier, amount=booked,
            reason=f"结算后追加责任认定（{batch.code}）：{note}",
        )
        entry.status = LiabilityEntry.CLAIMED
        entry.claim = claim
        entry.save()
    return entry, claim, waived


def allocate_liabilities(pending, balance):
    """待抵扣责任账按入账顺序分摊到可抵扣余额；超出部分转追偿。纯函数，便于核对。"""
    alloc, remaining = [], money(balance)
    for entry in pending:
        applied = min(entry.amount, max(ZERO, remaining))
        remaining -= applied
        alloc.append((entry, money(applied), money(entry.amount - applied)))
    return alloc


def goods_trajectory(batch):
    """同一批货的轨迹：每条记录实物所在方 / 质量责任方 / 应收应付方。"""
    a = batch.contract.supplier.name
    transfers = {t.id: t for t in TransferOrder.objects.filter(batch=batch)}
    t_receipts = {tr.id: tr for tr in TransferReceipt.objects.filter(transfer__batch=batch)}
    rows = []
    for e in batch.ledger_entries.all():
        custodian = liability = payable = "—"
        t = e.entry_type
        if t == L.ISSUE:
            custodian = a
        elif t == L.RECEIVE_QUALIFIED:
            custodian, liability, payable = "我方", a, f"应付 {a} 加工费"
        elif t == L.RECEIVE_REWORK:
            custodian, liability = a, a
        elif t == L.RECEIVE_SCRAP:
            custodian, liability = "已报废", a
        elif t == L.REWORK_RETURN_QUALIFIED:
            custodian, liability, payable = "我方", a, f"应付 {a} 加工费"
        elif t == L.REWORK_RETURN_SCRAP:
            custodian, liability = "已报废", a
        elif t == L.TRANSFER_OUT:
            order = transfers.get(e.ref_id)
            b = order.to_supplier.name if order else "新厂"
            custodian, liability, payable = f"{a} → {b}", a, f"返工费由 {a} 承担"
        elif t == L.TRANSFER_RETURN_QUALIFIED:
            tr = t_receipts.get(e.ref_id)
            b = tr.transfer.to_supplier.name if tr else "新厂"
            custodian, liability, payable = "我方", a, f"应付 {b} 返工费（{a} 负担）"
        elif t == L.TRANSFER_RETURN_SCRAP:
            custodian, liability, payable = "二次报废", a, f"{a} 赔偿（上限按原合同版本）"
        elif t == L.SETTLE_LOCK:
            payable = "结算冻结"
        rows.append({
            "at": e.created_at.isoformat(), "kind": "QTY", "event": t,
            "qty": str(e.qty), "amount": None,
            "custodian": custodian, "liability_party": liability,
            "settlement_party": payable, "memo": e.memo,
        })
    for le in LiabilityEntry.objects.filter(batch=batch).select_related(
            "supplier", "counter_supplier", "claim"):
        rows.append({
            "at": le.created_at.isoformat(), "kind": "MONEY", "event": le.entry_type,
            "qty": None, "amount": str(le.amount),
            "custodian": "—",
            "liability_party": le.supplier.name,
            "settlement_party": (
                f"应付 {le.counter_supplier.name} / 抵扣 {le.supplier.name}"
                if le.counter_supplier else f"{le.supplier.name} 赔偿"
            ) + (f"（已转追偿 {le.claim.code}）" if le.claim else ""),
            "memo": le.note,
        })
    rows.sort(key=lambda r: r["at"])
    return rows


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
    qs_t = (
        TransferReceipt.objects.filter(transfer__batch__contract=contract)
        .values("received_at")
        .annotate(q=Sum("qualified_qty"))
        .order_by("received_at")
    )
    by_day = {}
    for row in list(qs) + list(qs_t):
        by_day[row["received_at"]] = by_day.get(row["received_at"], ZERO) + row["q"]
    for day in sorted(by_day):
        cum += by_day[day]
        timeline.append((day, cum))

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
    """结算试算：合格加工费 - 超损耗扣款 - 超期扣款 - 责任抵扣。供预览与确认共用。

    应付新厂的返工费不在此处支出（那是另一条应付线），而是以"原厂责任抵扣"
    冲减原厂应付——同一笔 LiabilityEntry 驱动两侧，不会双重入账。
    """
    as_of = as_of or date.today()
    contract = batch.contract
    q = batch_quantities(batch)

    # 合格事件：正常/自返收货 + 新厂返工回厂，统一按日期取价、受可结算上限约束
    events = []
    for r in Receipt.objects.filter(batch=batch, qualified_qty__gt=ZERO):
        events.append((r.received_at, r.id, "R", r))
    for tr in TransferReceipt.objects.filter(transfer__batch=batch, qualified_qty__gt=ZERO):
        events.append((tr.received_at, tr.id, "T", tr))
    events.sort(key=lambda x: (x[0], x[1]))

    lines = []
    remaining = q["settleable_qty"]
    for _, _, kind, obj in events:
        take = min(obj.qualified_qty, remaining)
        if take <= ZERO:
            break
        remaining -= take
        amount = money(take * obj.price_version.unit_price)
        if kind == "R":
            ref, note = obj.supplier_receipt_no, f"收货 {obj.supplier_receipt_no}"
            receipt_id = obj.id
        else:
            ref, note = obj.receipt_no, f"新厂回厂 {obj.receipt_no}（转厂 {obj.transfer.code}）"
            receipt_id = None
        lines.append({
            "line_type": SettlementLine.QUALIFIED,
            "receipt_id": receipt_id,
            "receipt_no": ref,
            "qty": str(take),
            "unit_price": str(obj.price_version.unit_price),
            "price_effective_from": obj.price_version.effective_from.isoformat(),
            "amount": str(amount),
            "note": f"{note}：{take} × 价版 {obj.price_version.effective_from}",
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

    # 责任抵扣：可抵扣余额 = 结算前应付原厂；超出部分转独立追偿
    balance = money(qualified_amount - over_loss_amount - late_total)
    pending = list(
        LiabilityEntry.objects.filter(batch=batch, status=LiabilityEntry.PENDING).order_by("id")
    )
    alloc = allocate_liabilities(pending, balance)
    offset_total = ZERO
    for entry, applied, to_claim in alloc:
        if applied > ZERO:
            line_type = (SettlementLine.REWORK_OFFSET
                         if entry.entry_type == LiabilityEntry.REWORK_FEE
                         else SettlementLine.COMPENSATION)
            lines.append({
                "line_type": line_type,
                "qty": "0",
                "unit_price": "0",
                "amount": str(-applied),
                "note": entry.note,
            })
            offset_total += applied
        if to_claim > ZERO:
            lines.append({
                "line_type": SettlementLine.CLAIM_NOTICE,
                "qty": "0",
                "unit_price": "0",
                "amount": "0",
                "note": f"{entry.note} —— 超出可抵扣余额 {to_claim}，确认后形成独立追偿",
            })

    total = money(balance - offset_total)
    b_payable = _sum(TransferReceipt.objects.filter(transfer__batch=batch), "rework_fee")
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
            "at_supplier_b_qty": str(q["at_supplier_b"]),
        },
        "lines": lines,
        "qualified_amount": str(money(qualified_amount)),
        "over_loss_amount": str(money(over_loss_amount)),
        "late_penalty_amount": str(money(late_total)),
        "liability_offset_amount": str(money(offset_total)),
        "total_amount": str(total),
        "b_payable_amount": str(money(b_payable)),   # 应付新厂（独立应付线）
        "claims": [
            {"code": c.code, "supplier": c.supplier.name, "amount": str(c.amount),
             "reason": c.reason, "status": c.status}
            for c in RecoveryClaim.objects.filter(batch=batch)
        ],
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
            # 责任账落账：余额内抵扣，超出部分形成独立追偿
            balance = (Decimal(preview["qualified_amount"])
                       - Decimal(preview["over_loss_amount"])
                       - Decimal(preview["late_penalty_amount"]))
            pending = list(
                LiabilityEntry.objects.filter(batch=batch, status=LiabilityEntry.PENDING)
                .order_by("id")
            )
            for entry, applied, to_claim in allocate_liabilities(pending, balance):
                entry.offset_amount = applied
                if to_claim > ZERO:
                    entry.claim = _create_claim(
                        batch=batch, supplier=entry.supplier, amount=to_claim,
                        reason=f"结算抵扣余额不足（{settlement.code}）：{entry.note}",
                    )
                entry.status = (LiabilityEntry.OFFSET if applied > ZERO
                                else LiabilityEntry.CLAIMED)
                entry.save()
            L.objects.create(
                batch=batch, entry_type=L.SETTLE_LOCK, qty=ZERO,
                ref_type="Settlement", ref_id=settlement.id,
                memo=f"结算 {settlement.code} 双方确认，快照 {preview['calc_hash'][:12]}",
            )
        settlement.save()
    return settlement, preview
