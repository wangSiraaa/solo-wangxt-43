"""独立核对用例：账面数量与费用都不信任服务层结果，用原始单据/流水独立重算后比对。"""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core import services
from core.models import (
    Contract, ContractPriceVersion, DeliveryMilestone, Item,
    ProcessingBatch, QuantityLedgerEntry as L, Receipt, Settlement, Supplier, WorkReport,
)


def make_contract(**kw):
    supplier = Supplier.objects.create(code="SUP-T", name="测试外协厂")
    raw = Item.objects.create(code="RAW-T", name="原料", kind=Item.RAW, unit="米",
                              unit_cost=kw.pop("raw_cost", D("6.00")))
    product = Item.objects.create(code="PRD-T", name="成品", kind=Item.PRODUCT,
                                  unit="件", unit_cost=D("0"))
    contract = Contract.objects.create(
        code="C-T", supplier=supplier, raw_material=raw, product=product,
        conversion_ratio=kw.pop("ratio", D("3.0")),
        agreed_loss_rate=kw.pop("loss", D("0.02")),
        signed_at=date(2026, 1, 1),
    )
    ContractPriceVersion.objects.create(contract=contract, unit_price=D("1.20"),
                                        effective_from=date(2026, 1, 1))
    ContractPriceVersion.objects.create(contract=contract, unit_price=D("1.15"),
                                        effective_from=date(2026, 7, 1))
    for due, qty in kw.pop("milestones", []):
        DeliveryMilestone.objects.create(contract=contract, due_date=due, planned_qty=qty,
                                         penalty_rate_per_day=D("0.05"), penalty_cap=D("5000"))
    return contract


def make_batch(contract, qty=D("10000")):
    batch = ProcessingBatch.objects.create(code="B-T", contract=contract, issued_qty=qty,
                                           issued_at=date(2026, 6, 1))
    services.post_issue(batch)
    return batch


def receive(batch, no, day, q, r=D("0"), s=D("0"), kind=Receipt.NORMAL):
    receipt, created = services.post_receipt(
        batch=batch, supplier_receipt_no=no, kind=kind, received_at=day,
        qualified_qty=D(q), rework_qty=D(r), scrap_qty=D(s),
    )
    return receipt, created


class BookQuantityReconciliationTest(TestCase):
    """账面数量核对：单据汇总 == 数量流水重算 == 领料×比例×损耗恒等式。"""

    def setUp(self):
        self.contract = make_contract()
        self.batch = make_batch(self.contract)
        receive(self.batch, "R1", date(2026, 6, 28), "15000", s="100")
        receive(self.batch, "R2", date(2026, 7, 15), "9000", r="800", s="600")
        receive(self.batch, "R3", date(2026, 9, 5), "4000", s="100")
        receive(self.batch, "R4", date(2026, 9, 8), "700", s="100", kind=Receipt.REWORK_RETURN)
        services.post_work_report(batch=self.batch, supplier_report_no="W1",
                                  qty=D("29000"), reported_at=date(2026, 7, 20))

    def test_document_side_equals_ledger_side(self):
        doc = services.batch_quantities(self.batch)
        led = services.ledger_quantities(self.batch)
        for key in ("issued_qty", "reported_qty", "qualified_qty",
                    "scrap_total", "rework_outstanding"):
            self.assertEqual(doc[key], led[key], f"{key} 单据与流水不一致")

    def test_quantity_identity_holds(self):
        """应产 = 合格 + 报废 + 在返 + 在制；应产必须等于 领料×转化比例。"""
        q = services.batch_quantities(self.batch)
        self.assertEqual(q["expected_output"], D("10000") * D("3.0"))
        accounted = (q["qualified_qty"] + q["scrap_total"]
                     + q["rework_outstanding"] + q["in_process_qty"])
        self.assertEqual(accounted, q["expected_output"])

    def test_known_demo_numbers(self):
        q = services.batch_quantities(self.batch)
        self.assertEqual(q["qualified_qty"], D("28700"))
        self.assertEqual(q["scrap_total"], D("900"))
        self.assertEqual(q["rework_outstanding"], D("0"))   # 800 = 回厂700 + 返工报废100
        self.assertEqual(q["in_process_qty"], D("400"))
        self.assertEqual(q["allowed_loss_qty"], D("600"))
        self.assertEqual(q["over_loss_qty"], D("300"))
        self.assertEqual(q["settleable_qty"], D("28700"))


class DuplicateReceiptTest(TestCase):
    """重复回传：同一回传单号只入账一次，流水不重复。"""

    def test_duplicate_receipt_is_idempotent(self):
        contract = make_contract()
        batch = make_batch(contract)
        r1, created1 = receive(batch, "RC-DUP", date(2026, 6, 28), "1000", s="10")
        r2, created2 = receive(batch, "RC-DUP", date(2026, 6, 28), "1000", s="10")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(r1.id, r2.id)
        self.assertEqual(Receipt.objects.filter(supplier_receipt_no="RC-DUP").count(), 1)
        entries = L.objects.filter(batch=batch, entry_type=L.RECEIVE_QUALIFIED)
        self.assertEqual(sum(e.qty for e in entries), D("1000"))


class LatePenaltyTest(TestCase):
    """超期扣款：部分交付只对迟交部分扣一次，后续节点不重复扣。"""

    def test_partial_delivery_penalized_once(self):
        contract = make_contract(milestones=[(date(2026, 6, 30), D("15000")),
                                             (date(2026, 8, 31), D("26000"))])
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "15000")          # 节点1 刚好交齐
        receive(batch, "R2", date(2026, 7, 15), "9000")           # 累计 24000
        receive(batch, "R3", date(2026, 9, 5), "4000")            # 累计 28000，迟 5 天

        total, details = services.compute_late_penalty(contract, as_of=date(2026, 9, 10))
        self.assertEqual(len(details), 1)                          # 只有节点2 产生扣款
        d = details[0]
        self.assertEqual(D(d["shortfall_qty"]), D("2000"))         # 只算未交的 2000
        self.assertEqual(d["days_late"], 5)
        self.assertEqual(D(d["amount"]), D("500.00"))              # 2000×5×0.05
        self.assertEqual(total, D("500.00"))

    def test_no_double_penalty_across_milestones(self):
        """同一笔迟交数量在第二个节点不会被重复扣罚。"""
        contract = make_contract(milestones=[(date(2026, 6, 30), D("5000")),
                                             (date(2026, 7, 31), D("5000"))])
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 8, 10), "5000")            # 两个节点都逾期，一次交齐

        total, details = services.compute_late_penalty(contract, as_of=date(2026, 8, 10))
        self.assertEqual(len(details), 1)                          # 只在节点1 扣
        self.assertEqual(D(details[0]["newly_penalized_qty"]), D("5000"))
        self.assertEqual(D(details[0]["amount"]), D("5000.00"))    # 10250 触发封顶 5000


class OverLossTest(TestCase):
    """约定损耗内不扣款，超出部分折原料成本扣款。"""

    def test_within_agreed_loss_no_charge(self):
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "29000", s="600")  # 恰好在 2% 损耗内
        preview = services.build_settlement_preview(batch, as_of=date(2026, 7, 1))
        self.assertEqual(D(preview["over_loss_amount"]), D("0"))

    def test_over_loss_charged_at_raw_cost(self):
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "28200", s="900")  # 超损耗 300 件
        preview = services.build_settlement_preview(batch, as_of=date(2026, 7, 1))
        # 300 件 / 3.0 = 100 米 × 6.00 = 600.00
        self.assertEqual(D(preview["over_loss_amount"]), D("600.00"))


class SettleableRuleTest(TestCase):
    """报工不结算、返工不结算、超交不结算。"""

    def test_report_only_never_settleable(self):
        contract = make_contract()
        batch = make_batch(contract)
        services.post_work_report(batch=batch, supplier_report_no="W1",
                                  qty=D("30000"), reported_at=date(2026, 6, 20))
        preview = services.build_settlement_preview(batch, as_of=date(2026, 7, 1))
        self.assertEqual(D(preview["qualified_amount"]), D("0"))
        self.assertEqual(D(preview["quantities"]["settleable_qty"]), D("0"))

    def test_rework_not_settled_until_returned(self):
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "1000", r="500")
        q = services.batch_quantities(batch)
        self.assertEqual(q["settleable_qty"], D("1000"))
        self.assertEqual(q["rework_outstanding"], D("500"))
        receive(batch, "R2", date(2026, 7, 5), "480", s="20", kind=Receipt.REWORK_RETURN)
        q = services.batch_quantities(batch)
        self.assertEqual(q["settleable_qty"], D("1480"))
        self.assertEqual(q["rework_outstanding"], D("0"))

    def test_rework_return_cannot_exceed_outstanding(self):
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "1000", r="100")
        with self.assertRaises(services.DomainError):
            receive(batch, "R2", date(2026, 7, 5), "200", kind=Receipt.REWORK_RETURN)

    def test_over_delivery_not_settled(self):
        contract = make_contract()
        batch = make_batch(contract, qty=D("1000"))                # 应产 3000
        receive(batch, "R1", date(2026, 6, 28), "3200")            # 超交 200
        q = services.batch_quantities(batch)
        self.assertEqual(q["settleable_qty"], D("3000"))
        self.assertEqual(q["over_delivered_qty"], D("200"))
        preview = services.build_settlement_preview(batch, as_of=date(2026, 7, 1))
        self.assertEqual(D(preview["qualified_amount"]), D("3600.00"))  # 只结 3000×1.20


class FeeReconciliationTest(TestCase):
    """费用核对：用原始收货单与价版独立重算，与结算快照逐分比对。"""

    def test_snapshot_matches_independent_recompute(self):
        contract = make_contract(milestones=[(date(2026, 6, 30), D("15000")),
                                             (date(2026, 8, 31), D("26000"))])
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "15000", s="100")
        receive(batch, "R2", date(2026, 7, 15), "9000", r="800", s="600")
        receive(batch, "R3", date(2026, 9, 5), "4000", s="100")
        receive(batch, "R4", date(2026, 9, 8), "700", s="100", kind=Receipt.REWORK_RETURN)

        settlement, _ = services.confirm_settlement(batch, "supplier", as_of=date(2026, 9, 10))
        settlement, _ = services.confirm_settlement(batch, "us", as_of=date(2026, 9, 10))
        self.assertEqual(settlement.status, Settlement.CONFIRMED)
        snap = settlement.snapshot

        # —— 独立重算（不调用 services 的金额逻辑）——
        expected_qualified = D("0")
        for r in Receipt.objects.filter(batch=batch).order_by("received_at", "id"):
            price = (ContractPriceVersion.objects
                     .filter(contract=contract, effective_from__lte=r.received_at)
                     .order_by("-effective_from").first().unit_price)
            expected_qualified += (r.qualified_qty * price).quantize(D("0.01"))
        self.assertEqual(expected_qualified, D("33755.00"))
        self.assertEqual(D(snap["qualified_amount"]), expected_qualified)

        expected_over_loss = ((D("900") - D("600")) / D("3.0") * D("6.00")).quantize(D("0.01"))
        self.assertEqual(D(snap["over_loss_amount"]), expected_over_loss)   # 600.00

        expected_late = (D("2000") * 5 * D("0.05")).quantize(D("0.01"))
        self.assertEqual(D(snap["late_penalty_amount"]), expected_late)     # 500.00

        self.assertEqual(D(snap["total_amount"]),
                         expected_qualified - expected_over_loss - expected_late)  # 32655.00

        # 快照行合计 == 快照总额（借贷平衡）
        line_sum = sum(D(l["amount"]) for l in snap["lines"])
        self.assertEqual(line_sum, D(snap["total_amount"]))

    def test_snapshot_frozen_after_confirm(self):
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "1000")
        services.confirm_settlement(batch, "supplier", as_of=date(2026, 7, 1))
        settlement, _ = services.confirm_settlement(batch, "us", as_of=date(2026, 7, 1))
        frozen_total = settlement.snapshot["total_amount"]
        with self.assertRaises(services.DomainError):
            services.confirm_settlement(batch, "us", as_of=date(2026, 7, 2))
        settlement.refresh_from_db()
        self.assertEqual(settlement.snapshot["total_amount"], frozen_total)
        self.assertTrue(ProcessingBatch.objects.get(id=batch.id).locked)

    def test_changed_calculation_resets_confirmation(self):
        """确认期间来了新收货，计算指纹变化，双方需重新确认。"""
        contract = make_contract()
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "1000")
        s, _ = services.confirm_settlement(batch, "supplier", as_of=date(2026, 7, 1))
        self.assertTrue(s.confirmed_by_supplier)
        receive(batch, "R2", date(2026, 6, 30), "500")
        s, _ = services.confirm_settlement(batch, "us", as_of=date(2026, 7, 2))
        self.assertFalse(s.confirmed_by_supplier)   # 供应商确认被重置
        self.assertEqual(s.status, Settlement.DRAFT)


class ApiSmokeTest(TestCase):
    def test_flow_and_reconciliation_endpoints(self):
        contract = make_contract(milestones=[(date(2026, 6, 30), D("15000"))])
        batch = make_batch(contract)
        receive(batch, "R1", date(2026, 6, 28), "15000", s="100")

        resp = self.client.get(f"/api/batches/{batch.code}/flow/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["quantities"]["qualified_qty"], "15000")

        resp = self.client.get(f"/api/batches/{batch.code}/reconciliation/")
        self.assertTrue(resp.json()["balanced"])

        # 重复回传走 API 也幂等
        payload = {"batch_code": batch.code, "supplier_receipt_no": "R1",
                   "received_at": "2026-06-28", "qualified_qty": "15000", "scrap_qty": "100"}
        resp = self.client.post("/api/receipts/", payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["created"])
