from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import (
    LiabilityEntry,
    ProcessingBatch,
    RecoveryClaim,
    Settlement,
    Supplier,
    TransferOrder,
    TransferReceipt,
)
from .serializers import (
    ConfirmIn,
    DeterminationIn,
    ReceiptIn,
    TransferIn,
    TransferReceiptIn,
    WorkReportIn,
)


def batch_summary(batch):
    q = services.batch_quantities(batch)
    return {
        "code": batch.code,
        "contract": batch.contract.code,
        "supplier": batch.contract.supplier.name,
        "product": batch.contract.product.name,
        "unit": batch.contract.product.unit,
        "issued_at": batch.issued_at.isoformat(),
        "locked": batch.locked,
        "settlement_status": getattr(getattr(batch, "settlement", None), "status", None),
        "quantities": {k: str(v) for k, v in q.items()},
    }


class BatchList(APIView):
    def get(self, request):
        batches = ProcessingBatch.objects.select_related(
            "contract__supplier", "contract__product"
        ).order_by("code")
        return Response([batch_summary(b) for b in batches])


class BatchFlow(APIView):
    """批次数量去向：领料 -> 报工(备查) -> 收货(合格/返工/报废) -> 可结算 + 数量流水。"""

    def get(self, request, code):
        batch = get_object_or_404(ProcessingBatch, code=code)
        data = batch_summary(batch)
        data["ledger"] = [
            {
                "id": e.id, "entry_type": e.entry_type, "qty": str(e.qty),
                "ref_type": e.ref_type, "memo": e.memo,
                "created_at": e.created_at.isoformat(),
            }
            for e in batch.ledger_entries.all()
        ]
        data["receipts"] = [
            {
                "no": r.supplier_receipt_no, "kind": r.kind,
                "received_at": r.received_at.isoformat(),
                "qualified": str(r.qualified_qty), "rework": str(r.rework_qty),
                "scrap": str(r.scrap_qty),
                "unit_price": str(r.price_version.unit_price),
                "price_effective_from": r.price_version.effective_from.isoformat(),
            }
            for r in batch.receipts.order_by("received_at", "id")
        ]
        data["work_reports"] = [
            {"no": w.supplier_report_no, "qty": str(w.qty), "reported_at": w.reported_at.isoformat()}
            for w in batch.work_reports.order_by("reported_at", "id")
        ]
        data["transfers"] = [
            {
                "code": t.code, "to_supplier": t.to_supplier.name, "qty": str(t.qty),
                "rework_unit_price": str(t.rework_unit_price),
                "transferred_at": t.transferred_at.isoformat(), "status": t.status,
                "price_version_from": t.price_version.effective_from.isoformat() if t.price_version else None,
                "receipts": [
                    {"no": tr.receipt_no, "received_at": tr.received_at.isoformat(),
                     "qualified": str(tr.qualified_qty), "scrap": str(tr.scrap_qty),
                     "rework_fee": str(tr.rework_fee)}
                    for tr in t.receipts.all()
                ],
            }
            for t in batch.transfers.select_related("to_supplier", "price_version")
        ]
        data["liabilities"] = [
            {
                "entry_type": le.entry_type, "status": le.status,
                "supplier": le.supplier.name,
                "counter_supplier": le.counter_supplier.name if le.counter_supplier else None,
                "amount": str(le.amount), "offset_amount": str(le.offset_amount),
                "claim": le.claim.code if le.claim else None, "note": le.note,
            }
            for le in LiabilityEntry.objects.filter(batch=batch)
            .select_related("supplier", "counter_supplier", "claim")
        ]
        data["claims"] = [
            {"code": c.code, "supplier": c.supplier.name, "amount": str(c.amount),
             "reason": c.reason, "status": c.status}
            for c in RecoveryClaim.objects.filter(batch=batch)
        ]
        data["b_payable_amount"] = str(
            sum((tr.rework_fee for tr in TransferReceipt.objects.filter(transfer__batch=batch)),
                services.ZERO)
        )
        return Response(data)


class BatchReconciliation(APIView):
    """账面核对：单据汇总 vs 数量流水重算，两边必须一致。"""

    def get(self, request, code):
        batch = get_object_or_404(ProcessingBatch, code=code)
        doc = services.batch_quantities(batch)
        ledger = services.ledger_quantities(batch)
        checks = []
        for key in ("issued_qty", "reported_qty", "qualified_qty", "scrap_total",
                    "rework_outstanding", "at_supplier_b", "transfer_scrap_qty"):
            checks.append({
                "measure": key,
                "document_side": str(doc[key]),
                "ledger_side": str(ledger[key]),
                "balanced": doc[key] == ledger[key],
            })
        return Response({"batch": batch.code, "balanced": all(c["balanced"] for c in checks),
                         "checks": checks})


class WorkReportPost(APIView):
    def post(self, request):
        s = WorkReportIn(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        batch = get_object_or_404(ProcessingBatch, code=d["batch_code"])
        report, created = services.post_work_report(
            batch=batch, supplier_report_no=d["supplier_report_no"],
            qty=d["qty"], reported_at=d["reported_at"],
        )
        return Response({"id": report.id, "created": created},
                        status=201 if created else 200)


class ReceiptPost(APIView):
    """收货回传。重复回传（同一 supplier_receipt_no）返回 200 + 原单，不重复入账。"""

    def post(self, request):
        s = ReceiptIn(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        batch = get_object_or_404(ProcessingBatch, code=d["batch_code"])
        receipt, created = services.post_receipt(
            batch=batch, supplier_receipt_no=d["supplier_receipt_no"], kind=d["kind"],
            received_at=d["received_at"], qualified_qty=d["qualified_qty"],
            rework_qty=d["rework_qty"], scrap_qty=d["scrap_qty"],
        )
        return Response({"id": receipt.id, "created": created,
                         "price_version_from": receipt.price_version.effective_from.isoformat()},
                        status=201 if created else 200)


class SettlementPreview(APIView):
    """结算试算 + 双方差异（确认前展示）。"""

    def get(self, request, code):
        batch = get_object_or_404(ProcessingBatch, code=code)
        settlement = getattr(batch, "settlement", None)
        if settlement and settlement.status == Settlement.CONFIRMED:
            return Response({"status": Settlement.CONFIRMED, "snapshot": settlement.snapshot})
        preview = services.build_settlement_preview(batch)
        return Response({
            "status": getattr(settlement, "status", "NONE"),
            "confirmed_by_supplier": getattr(settlement, "confirmed_by_supplier", False),
            "confirmed_by_us": getattr(settlement, "confirmed_by_us", False),
            "preview": preview,
        })


class SettlementConfirm(APIView):
    def post(self, request, code):
        s = ConfirmIn(data=request.data)
        s.is_valid(raise_exception=True)
        batch = get_object_or_404(ProcessingBatch, code=code)
        settlement, preview = services.confirm_settlement(batch, s.validated_data["party"])
        return Response({
            "status": settlement.status,
            "confirmed_by_supplier": settlement.confirmed_by_supplier,
            "confirmed_by_us": settlement.confirmed_by_us,
            "calc_hash": settlement.calc_hash,
            "snapshot": settlement.snapshot,
        })


class SettlementDetail(APIView):
    def get(self, request, code):
        batch = get_object_or_404(ProcessingBatch, code=code)
        settlement = get_object_or_404(Settlement, batch=batch)
        return Response({
            "code": settlement.code,
            "status": settlement.status,
            "calc_hash": settlement.calc_hash,
            "confirmed_at": settlement.confirmed_at and settlement.confirmed_at.isoformat(),
            "snapshot": settlement.snapshot,
            "lines": [
                {"line_type": l.line_type, "qty": str(l.qty),
                 "unit_price": str(l.unit_price), "amount": str(l.amount), "note": l.note}
                for l in settlement.lines.all()
            ],
        })


class TransferPost(APIView):
    """转厂：实物 A -> B，责任仍在 A。正在确认结算的批次拒绝转出。"""

    def post(self, request):
        s = TransferIn(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        batch = get_object_or_404(ProcessingBatch, code=d["batch_code"])
        to_supplier = get_object_or_404(Supplier, code=d["to_supplier_code"])
        order, created = services.post_transfer(
            batch=batch, code=d["code"], to_supplier=to_supplier, qty=d["qty"],
            transferred_at=d["transferred_at"], rework_unit_price=d["rework_unit_price"],
        )
        return Response({"code": order.code, "created": created, "status": order.status},
                        status=201 if created else 200)


class TransferReceiptPost(APIView):
    def post(self, request):
        s = TransferReceiptIn(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        transfer = get_object_or_404(TransferOrder, code=d["transfer_code"])
        tr, created = services.post_transfer_receipt(
            transfer=transfer, receipt_no=d["receipt_no"], received_at=d["received_at"],
            qualified_qty=d["qualified_qty"], scrap_qty=d["scrap_qty"],
        )
        return Response({"id": tr.id, "created": created, "rework_fee": str(tr.rework_fee)},
                        status=201 if created else 200)


class LiabilityDeterminationPost(APIView):
    """结算后追加责任认定：只形成独立追偿，不改已冻结快照。"""

    def post(self, request, code):
        s = DeterminationIn(data=request.data)
        s.is_valid(raise_exception=True)
        batch = get_object_or_404(ProcessingBatch, code=code)
        entry, claim, waived = services.post_liability_determination(
            batch=batch, qty=s.validated_data["qty"],
            reference_date=s.validated_data["reference_date"],
            note=s.validated_data["note"],
        )
        return Response({
            "claim_code": claim.code, "claim_amount": str(claim.amount),
            "cap_waived": str(waived), "note": entry.note,
        }, status=201)


class BatchTrajectory(APIView):
    """同一批货的轨迹：实物所在方 / 质量责任方 / 应收应付方。"""

    def get(self, request, code):
        batch = get_object_or_404(ProcessingBatch, code=code)
        return Response({"batch": batch.code, "events": services.goods_trajectory(batch)})


class ContractDetail(APIView):
    def get(self, request, code):
        from .models import Contract
        c = get_object_or_404(Contract, code=code)
        return Response({
            "code": c.code,
            "supplier": c.supplier.name,
            "raw_material": {"code": c.raw_material.code, "unit": c.raw_material.unit,
                             "unit_cost": str(c.raw_material.unit_cost)},
            "product": {"code": c.product.code, "unit": c.product.unit},
            "conversion_ratio": str(c.conversion_ratio),
            "agreed_loss_rate": str(c.agreed_loss_rate),
            "price_versions": [
                {"unit_price": str(p.unit_price), "effective_from": p.effective_from.isoformat(),
                 "note": p.note}
                for p in c.price_versions.all()
            ],
            "milestones": [
                {"due_date": m.due_date.isoformat(), "planned_qty": str(m.planned_qty),
                 "penalty_rate_per_day": str(m.penalty_rate_per_day),
                 "penalty_cap": str(m.penalty_cap)}
                for m in c.milestones.all()
            ],
        })
