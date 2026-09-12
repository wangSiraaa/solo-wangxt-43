"""演示数据：两条合同线，覆盖转厂返工全场景。

批次 B-2026-001（未确认，可交互演示）：
- 原厂苏州精工无返工能力，RC-002 的 800 件不合格分两次转给宏展精密
- TR-001 二次返工失败：500 件报废 20，按原合同版本上限赔偿
- 返工费由原厂承担：新厂应收 = 原厂抵扣，同一笔责任账驱动两侧

批次 B-2026-002（已结算锁定）：
- 结算快照已冻结，之后客户端发现 200 件批次性虚焊
- 追加责任认定形成独立追偿 CL 记录，不触碰快照

B-2026-001 手工核对账：
  应产 10000米 × 3 = 30000；合格 28780，报废 800+20(二次)，在制 400
  加工费 15000×1.20 + (9000+480+4000+300)×1.15 = 18000 + 15847 = 33847.00
  超损耗 (800-600)/3 × 6.00 = 400.00
  超期   节点8/31短交1520，9/5交齐迟5天 × 0.05 = 380.00
  返工费抵扣 (480+300)×0.30 = 234.00（= 应付新厂 234.00）
  赔偿抵扣 20/3×6.00 = 40.00（上限 300 内）
  应付原厂 33847 - 400 - 380 - 234 - 40 = 32793.00
"""
from datetime import date
from decimal import Decimal as D

from django.core.management.base import BaseCommand

from core.models import (
    Contract, ContractPriceVersion, DeliveryMilestone, Item,
    LiabilityEntry, ProcessingBatch, QuantityLedgerEntry, Receipt,
    RecoveryClaim, Settlement, Supplier, TransferOrder, TransferReceipt, WorkReport,
)
from core import services


class Command(BaseCommand):
    help = "重置并载入委外结算演示数据（含转厂返工）"

    def handle(self, *args, **opts):
        for m in (Settlement, LiabilityEntry, RecoveryClaim, TransferReceipt, TransferOrder,
                  QuantityLedgerEntry, Receipt, WorkReport, ProcessingBatch,
                  DeliveryMilestone, ContractPriceVersion, Contract, Item, Supplier):
            m.objects.all().delete()

        sup_a = Supplier.objects.create(code="SUP-01", name="苏州精工五金有限公司")
        sup_b = Supplier.objects.create(code="SUP-02", name="宏展精密电子有限公司")
        raw = Item.objects.create(code="CU-WIRE", name="磷铜线材", kind=Item.RAW,
                                  unit="米", unit_cost=D("6.00"))
        product = Item.objects.create(code="CONN-01", name="接线端子", kind=Item.PRODUCT,
                                      unit="件", unit_cost=D("0"))

        # ---- 合同 1：主演示批次 ----
        c1 = Contract.objects.create(
            code="C-2026-001", supplier=sup_a, raw_material=raw, product=product,
            conversion_ratio=D("3.0000"), agreed_loss_rate=D("0.0200"),
            signed_at=date(2026, 1, 5),
        )
        ContractPriceVersion.objects.create(contract=c1, unit_price=D("1.20"),
                                            compensation_cap=D("300.00"),
                                            effective_from=date(2026, 1, 1), note="年初价版")
        ContractPriceVersion.objects.create(contract=c1, unit_price=D("1.15"),
                                            compensation_cap=D("300.00"),
                                            effective_from=date(2026, 7, 1), note="下半年调价")
        DeliveryMilestone.objects.create(contract=c1, due_date=date(2026, 6, 30),
                                         planned_qty=D("15000"), penalty_rate_per_day=D("0.05"),
                                         penalty_cap=D("5000"))
        DeliveryMilestone.objects.create(contract=c1, due_date=date(2026, 8, 31),
                                         planned_qty=D("26000"), penalty_rate_per_day=D("0.05"),
                                         penalty_cap=D("5000"))

        b1 = ProcessingBatch.objects.create(code="B-2026-001", contract=c1,
                                            issued_qty=D("10000"), issued_at=date(2026, 6, 1))
        services.post_issue(b1)
        services.post_work_report(batch=b1, supplier_report_no="WR-001",
                                  qty=D("15000"), reported_at=date(2026, 6, 25))
        services.post_work_report(batch=b1, supplier_report_no="WR-002",
                                  qty=D("14000"), reported_at=date(2026, 7, 20))
        services.post_receipt(batch=b1, supplier_receipt_no="RC-001",
                              kind=Receipt.NORMAL, received_at=date(2026, 6, 28),
                              qualified_qty=D("15000"), scrap_qty=D("100"))
        for _ in range(2):  # 重复回传幂等演示
            services.post_receipt(batch=b1, supplier_receipt_no="RC-002",
                                  kind=Receipt.NORMAL, received_at=date(2026, 7, 15),
                                  qualified_qty=D("9000"), rework_qty=D("800"), scrap_qty=D("600"))

        # 分批转厂：原厂无返工能力，800 件不合格分两次转宏展精密
        t1, _ = services.post_transfer(batch=b1, code="T-001", to_supplier=sup_b, qty=D("500"),
                                       transferred_at=date(2026, 7, 25),
                                       rework_unit_price=D("0.30"))
        t2, _ = services.post_transfer(batch=b1, code="T-002", to_supplier=sup_b, qty=D("300"),
                                       transferred_at=date(2026, 8, 10),
                                       rework_unit_price=D("0.30"))
        # 二次返工失败：T-001 报废 20 件
        services.post_transfer_receipt(transfer=t1, receipt_no="TR-001",
                                       received_at=date(2026, 8, 20),
                                       qualified_qty=D("480"), scrap_qty=D("20"))
        services.post_receipt(batch=b1, supplier_receipt_no="RC-003",
                              kind=Receipt.NORMAL, received_at=date(2026, 9, 5),
                              qualified_qty=D("4000"), scrap_qty=D("100"))
        services.post_transfer_receipt(transfer=t2, receipt_no="TR-002",
                                       received_at=date(2026, 9, 6),
                                       qualified_qty=D("300"), scrap_qty=D("0"))

        # ---- 合同 2：已结算批次 + 结算后追加责任认定 ----
        c2 = Contract.objects.create(
            code="C-2026-002", supplier=sup_a, raw_material=raw, product=product,
            conversion_ratio=D("3.0000"), agreed_loss_rate=D("0.0200"),
            signed_at=date(2026, 7, 1),
        )
        ContractPriceVersion.objects.create(contract=c2, unit_price=D("1.15"),
                                            compensation_cap=D("300.00"),
                                            effective_from=date(2026, 7, 1), note="单一口价")
        DeliveryMilestone.objects.create(contract=c2, due_date=date(2026, 8, 31),
                                         planned_qty=D("8000"), penalty_rate_per_day=D("0.05"),
                                         penalty_cap=D("3000"))
        b2 = ProcessingBatch.objects.create(code="B-2026-002", contract=c2,
                                            issued_qty=D("3000"), issued_at=date(2026, 8, 1))
        services.post_issue(b2)
        services.post_receipt(batch=b2, supplier_receipt_no="RC-201",
                              kind=Receipt.NORMAL, received_at=date(2026, 8, 28),
                              qualified_qty=D("8800"), scrap_qty=D("150"))
        services.confirm_settlement(b2, "supplier", as_of=date(2026, 9, 1))
        services.confirm_settlement(b2, "us", as_of=date(2026, 9, 1))
        # 结算后：客户端发现 200 件批次性虚焊 -> 独立追偿，快照不动
        services.post_liability_determination(
            batch=b2, qty=D("200"), reference_date=date(2026, 9, 10),
            note="客户端发现批次性虚焊，原厂质量责任",
        )

        q1 = services.batch_quantities(b1)
        claim = RecoveryClaim.objects.get(batch=b2)
        self.stdout.write(self.style.SUCCESS(
            f"演示数据就绪：\n"
            f"  {b1.code} 合格 {q1['qualified_qty']} / 应产 {q1['expected_output']} "
            f"/ 在新厂 {q1['at_supplier_b']}（待双方确认）\n"
            f"  {b2.code} 已结算锁定，追加追偿 {claim.code} = {claim.amount}"
        ))
