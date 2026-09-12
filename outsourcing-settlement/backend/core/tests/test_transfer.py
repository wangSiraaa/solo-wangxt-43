"""转厂返工链路核对：实物数量守恒、三方责任分离、返工费不重复计、快照不可覆盖。"""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core import services
from core.models import (
    Contract, ContractPriceVersion, DeliveryMilestone, Item,
    LiabilityEntry, ProcessingBatch, QuantityLedgerEntry as L, Receipt,
    RecoveryClaim, Settlement, Supplier, TransferOrder,
)


def make_world(cap=D("300.00"), milestones=None):
    sup_a = Supplier.objects.create(code="SUP-A", name="原厂A")
    sup_b = Supplier.objects.create(code="SUP-B", name="新厂B")
    raw = Item.objects.create(code="RAW-X", name="原料", kind=Item.RAW, unit="米",
                              unit_cost=D("6.00"))
    product = Item.objects.create(code="PRD-X", name="成品", kind=Item.PRODUCT,
                                  unit="件", unit_cost=D("0"))
    contract = Contract.objects.create(
        code="C-X", supplier=sup_a, raw_material=raw, product=product,
        conversion_ratio=D("3.0"), agreed_loss_rate=D("0.02"), signed_at=date(2026, 1, 1),
    )
    ContractPriceVersion.objects.create(contract=contract, unit_price=D("1.20"),
                                        compensation_cap=cap,
                                        effective_from=date(2026, 1, 1))
    for due, qty in (milestones or []):
        DeliveryMilestone.objects.create(contract=contract, due_date=due, planned_qty=qty,
                                         penalty_rate_per_day=D("0.05"), penalty_cap=D("5000"))
    batch = ProcessingBatch.objects.create(code="B-X", contract=contract,
                                           issued_qty=D("10000"), issued_at=date(2026, 6, 1))
    services.post_issue(batch)
    return sup_a, sup_b, contract, batch


def receive(batch, no, day, q, r=D("0"), s=D("0")):
    return services.post_receipt(batch=batch, supplier_receipt_no=no, kind=Receipt.NORMAL,
                                 received_at=day, qualified_qty=D(q), rework_qty=D(r),
                                 scrap_qty=D(s))[0]


def transfer(batch, sup_b, code, qty, day, price=D("0.30")):
    return services.post_transfer(batch=batch, code=code, to_supplier=sup_b, qty=D(qty),
                                  transferred_at=day, rework_unit_price=price)[0]


class TransferConservationTest(TestCase):
    """全链实物数量守恒：应产 = 合格 + 报废 + 二次报废 + 在返(A) + 在新厂(B) + 在制。"""

    def setUp(self):
        self.sup_a, self.sup_b, self.contract, self.batch = make_world()
        receive(self.batch, "R1", date(2026, 6, 28), "15000", s="100")
        receive(self.batch, "R2", date(2026, 7, 15), "9000", r="800", s="600")
        t1 = transfer(self.batch, self.sup_b, "T1", "500", date(2026, 7, 25))
        transfer(self.batch, self.sup_b, "T2", "300", date(2026, 8, 10))
        services.post_transfer_receipt(transfer=t1, receipt_no="TR1",
                                       received_at=date(2026, 8, 20),
                                       qualified_qty=D("480"), scrap_qty=D("20"))

    def test_quantity_identity_with_transfers(self):
        q = services.batch_quantities(self.batch)
        accounted = (q["qualified_qty"] + q["scrap_total"] + q["transfer_scrap_qty"]
                     + q["rework_outstanding"] + q["at_supplier_b"] + q["in_process_qty"])
        self.assertEqual(accounted, q["expected_output"])
        self.assertEqual(q["qualified_qty"], D("24480"))      # 15000+9000+480
        self.assertEqual(q["at_supplier_b"], D("300"))        # T2 的 300 件仍在新厂
        self.assertEqual(q["rework_outstanding"], D("0"))     # 全部转出
        self.assertEqual(q["transfer_scrap_qty"], D("20"))

    def test_ledger_side_matches_document_side(self):
        doc = services.batch_quantities(self.batch)
        led = services.ledger_quantities(self.batch)
        for key in ("qualified_qty", "scrap_total", "rework_outstanding",
                    "at_supplier_b", "transfer_scrap_qty"):
            self.assertEqual(doc[key], led[key], f"{key} 单据与流水不一致")

    def test_transfer_is_not_reproduction(self):
        """转厂不增加应产、不改变可结算上限。"""
        q = services.batch_quantities(self.batch)
        self.assertEqual(q["expected_output"], D("30000"))
        self.assertEqual(q["settleable_qty"], D("24480"))

    def test_transfer_cannot_exceed_rework_at_a(self):
        with self.assertRaises(services.DomainError):
            transfer(self.batch, self.sup_b, "T-OVER", "1", date(2026, 8, 21))

    def test_transfer_receipt_cannot_exceed_transferred(self):
        t = TransferOrder.objects.get(code="T2")
        with self.assertRaises(services.DomainError):
            services.post_transfer_receipt(transfer=t, receipt_no="TR-OVER",
                                           received_at=date(2026, 8, 25),
                                           qualified_qty=D("301"))


class TransferBlockingTest(TestCase):
    """正在确认结算 / 已锁定的批次不能再次转出。"""

    def setUp(self):
        self.sup_a, self.sup_b, self.contract, self.batch = make_world()
        receive(self.batch, "R1", date(2026, 6, 28), "1000", r="500")

    def test_blocked_during_confirmation(self):
        services.confirm_settlement(self.batch, "supplier", as_of=date(2026, 7, 1))
        with self.assertRaises(services.DomainError):
            transfer(self.batch, self.sup_b, "T-B", "100", date(2026, 7, 2))

    def test_blocked_when_locked(self):
        services.confirm_settlement(self.batch, "supplier", as_of=date(2026, 7, 1))
        services.confirm_settlement(self.batch, "us", as_of=date(2026, 7, 1))
        with self.assertRaises(services.DomainError):
            transfer(self.batch, self.sup_b, "T-L", "100", date(2026, 7, 2))


class ReworkFeeNoDoubleCountTest(TestCase):
    """一笔返工费：计入新厂应付 且 只抵扣原厂一次，不重复。"""

    def setUp(self):
        self.sup_a, self.sup_b, self.contract, self.batch = make_world()
        receive(self.batch, "R1", date(2026, 6, 28), "15000", s="100")
        receive(self.batch, "R2", date(2026, 7, 15), "9000", r="800", s="600")
        t1 = transfer(self.batch, self.sup_b, "T1", "500", date(2026, 7, 25))
        t2 = transfer(self.batch, self.sup_b, "T2", "300", date(2026, 8, 10))
        services.post_transfer_receipt(transfer=t1, receipt_no="TR1",
                                       received_at=date(2026, 8, 20),
                                       qualified_qty=D("480"), scrap_qty=D("20"))
        services.post_transfer_receipt(transfer=t2, receipt_no="TR2",
                                       received_at=date(2026, 9, 6),
                                       qualified_qty=D("300"), scrap_qty=D("0"))

    def test_b_payable_equals_a_offset_exactly_once(self):
        s, _ = services.confirm_settlement(self.batch, "supplier", as_of=date(2026, 9, 10))
        s, _ = services.confirm_settlement(self.batch, "us", as_of=date(2026, 9, 10))
        snap = s.snapshot

        # 新厂应付（独立重算）：合格回厂 × 返工单价
        b_payable = D("480") * D("0.30") + D("300") * D("0.30")   # 234.00
        self.assertEqual(D(snap["b_payable_amount"]), b_payable)

        # 原厂抵扣侧：责任账中 REWORK_FEE 的抵扣合计
        fees = LiabilityEntry.objects.filter(batch=self.batch,
                                             entry_type=LiabilityEntry.REWORK_FEE)
        a_offset = sum(e.offset_amount for e in fees)
        self.assertEqual(a_offset, b_payable)                      # 两侧同源相等
        self.assertEqual(sum(e.amount for e in fees), b_payable)   # 只记了一笔

        # 快照中返工费抵扣只出现一次（两行各一笔，合计 = b_payable）
        offset_lines = [l for l in snap["lines"] if l["line_type"] == "REWORK_OFFSET"]
        self.assertEqual(sum(-D(l["amount"]) for l in offset_lines), b_payable)

        # 原厂加工费不受抵扣影响：独立重算 == 快照（本合同单一价版 1.20）
        expected_qualified = (D("15000") + D("9000") + D("480") + D("300")) * D("1.20")
        self.assertEqual(D(snap["qualified_amount"]), expected_qualified.quantize(D("0.01")))

        # 没有形成追偿（余额充足），也没有重复扣款行
        self.assertEqual(RecoveryClaim.objects.filter(batch=self.batch).count(), 0)
        self.assertEqual(D(snap["total_amount"]),
                         D(snap["qualified_amount"]) - D(snap["over_loss_amount"])
                         - D(snap["late_penalty_amount"]) - b_payable - D("40.00"))

    def test_secondary_scrap_compensation_capped_by_contract_version(self):
        """二次报废赔偿 40.00 在版本上限 300 内；超额部分豁免并留痕。"""
        comp = LiabilityEntry.objects.get(batch=self.batch,
                                          entry_type=LiabilityEntry.COMPENSATION)
        self.assertEqual(comp.amount, D("40.00"))       # 20/3×6.00
        self.assertEqual(comp.price_version.effective_from, date(2026, 1, 1))


class ExcessClaimTest(TestCase):
    """责任账超过可抵扣余额时，超出部分形成独立追偿记录。"""

    def test_excess_forms_recovery_claim(self):
        sup_a, sup_b, contract, batch = make_world()
        receive(batch, "R1", date(2026, 6, 28), "100", r="900")
        t = transfer(batch, sup_b, "T1", "900", date(2026, 7, 1), price=D("2.00"))
        services.post_transfer_receipt(transfer=t, receipt_no="TR1",
                                       received_at=date(2026, 7, 10),
                                       qualified_qty=D("900"))    # 返工费 1800
        services.confirm_settlement(batch, "supplier", as_of=date(2026, 7, 15))
        s, _ = services.confirm_settlement(batch, "us", as_of=date(2026, 7, 15))

        # 可抵扣余额 = (100+900 合格) × 1.20 = 1200；返工费 1800 超出 600
        entry = LiabilityEntry.objects.get(batch=batch)
        self.assertEqual(entry.offset_amount, D("1200.00"))
        claim = RecoveryClaim.objects.get(batch=batch)
        self.assertEqual(claim.amount, D("600.00"))               # 超出部分独立追偿
        self.assertEqual(entry.claim, claim)
        self.assertEqual(D(s.snapshot["total_amount"]), D("0.00"))


class PostSettlementDeterminationTest(TestCase):
    """结算后追加责任认定：形成独立追偿，已冻结快照与原厂加工费不被覆盖。"""

    def test_snapshot_untouched_and_claim_created(self):
        sup_a, sup_b, contract, batch = make_world(cap=D("300.00"))
        receive(batch, "R1", date(2026, 6, 28), "29000", s="600")
        services.confirm_settlement(batch, "supplier", as_of=date(2026, 7, 1))
        s, _ = services.confirm_settlement(batch, "us", as_of=date(2026, 7, 1))
        frozen_total = s.snapshot["total_amount"]
        frozen_hash = s.calc_hash

        entry, claim, waived = services.post_liability_determination(
            batch=batch, qty=D("200"), reference_date=date(2026, 7, 5),
            note="客户端发现批次性虚焊",
        )
        # 200/3×6 = 400，上限 300 -> 追偿 300，豁免 100
        self.assertEqual(claim.amount, D("300.00"))
        self.assertEqual(waived, D("100.00"))
        self.assertEqual(claim.supplier, sup_a)

        s.refresh_from_db()
        self.assertEqual(s.snapshot["total_amount"], frozen_total)   # 快照不动
        self.assertEqual(s.calc_hash, frozen_hash)

    def test_determination_rejected_before_settlement(self):
        sup_a, sup_b, contract, batch = make_world()
        receive(batch, "R1", date(2026, 6, 28), "1000")
        with self.assertRaises(services.DomainError):
            services.post_liability_determination(
                batch=batch, qty=D("10"), reference_date=date(2026, 7, 5), note="过早",
            )


class TrajectoryTest(TestCase):
    """对账员视角：同一批货上两家供应商各自承担什么。"""

    def test_trajectory_shows_both_suppliers(self):
        sup_a, sup_b, contract, batch = make_world()
        receive(batch, "R1", date(2026, 6, 28), "1000", r="500")
        t = transfer(batch, sup_b, "T1", "500", date(2026, 7, 1))
        services.post_transfer_receipt(transfer=t, receipt_no="TR1",
                                       received_at=date(2026, 7, 10),
                                       qualified_qty=D("480"), scrap_qty=D("20"))
        rows = services.goods_trajectory(batch)
        by_event = {}
        for r in rows:
            by_event.setdefault(r["event"], []).append(r)

        out = by_event["TRANSFER_OUT"][0]
        self.assertIn("原厂A", out["custodian"])
        self.assertIn("新厂B", out["custodian"])
        self.assertEqual(out["liability_party"], "原厂A")          # 责任仍在原厂

        back = by_event["TRANSFER_RETURN_QUALIFIED"][0]
        self.assertEqual(back["custodian"], "我方")
        self.assertIn("新厂B", back["settlement_party"])           # 应付新厂
        self.assertIn("原厂A", back["settlement_party"])           # 原厂负担

        fee = [r for r in by_event["REWORK_FEE"]][0]
        self.assertEqual(fee["liability_party"], "原厂A")
        self.assertIn("新厂B", fee["settlement_party"])

        scrap = by_event["TRANSFER_RETURN_SCRAP"][0]
        self.assertEqual(scrap["liability_party"], "原厂A")


class TransferApiSmokeTest(TestCase):
    def test_transfer_endpoints_and_locked_batch_guard(self):
        sup_a, sup_b, contract, batch = make_world()
        receive(batch, "R1", date(2026, 6, 28), "1000", r="500")

        resp = self.client.post("/api/transfers/", {
            "batch_code": "B-X", "code": "T1", "to_supplier_code": "SUP-B",
            "qty": "500", "transferred_at": "2026-07-01", "rework_unit_price": "0.30",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201)

        resp = self.client.post("/api/transfer-receipts/", {
            "transfer_code": "T1", "receipt_no": "TR1", "received_at": "2026-07-10",
            "qualified_qty": "480", "scrap_qty": "20",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(D(resp.json()["rework_fee"]), D("144.00"))

        # 双方确认锁定后，转厂 API 拒绝
        self.client.post(f"/api/batches/{batch.code}/settlement/confirm/",
                         {"party": "supplier"}, content_type="application/json")
        self.client.post(f"/api/batches/{batch.code}/settlement/confirm/",
                         {"party": "us"}, content_type="application/json")
        resp = self.client.post("/api/transfers/", {
            "batch_code": "B-X", "code": "T2", "to_supplier_code": "SUP-B",
            "qty": "1", "transferred_at": "2026-07-12", "rework_unit_price": "0.30",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(f"/api/batches/{batch.code}/trajectory/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(e["event"] == "TRANSFER_OUT" for e in resp.json()["events"]))
