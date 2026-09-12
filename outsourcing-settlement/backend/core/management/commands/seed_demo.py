"""演示数据：一条合同 + 一个加工批次，覆盖三类异常场景。

- 部分不合格：RC-002 拆出 合格9000 / 返工800 / 报废600
- 重复回传：RC-002 回传两次，第二次幂等返回原单
- 超约定损耗：总报废 900 > 约定损耗 600（应产30000 × 2%）

预期账面（供人工核对）：
  应产 10000米 × 3 = 30000 件；合格 28700，报废 900，在返 100，在制 300
  加工费 15000×1.20 + (9000+4000+700)×1.15 = 18000 + 15755 = 33755.00
  超损耗扣款 (900-600)/3 × 6.00 = 600.00
  超期扣款  节点8/31短交2000，9/05交齐迟5天 × 0.05 = 500.00
  应付合计 33755.00 - 600.00 - 500.00 = 32655.00
"""
from datetime import date
from decimal import Decimal as D

from django.core.management.base import BaseCommand

from core.models import (
    Contract, ContractPriceVersion, DeliveryMilestone, Item,
    ProcessingBatch, QuantityLedgerEntry, Receipt, Settlement, Supplier, WorkReport,
)
from core import services


class Command(BaseCommand):
    help = "重置并载入委外结算演示数据"

    def handle(self, *args, **opts):
        for m in (Settlement, QuantityLedgerEntry, Receipt, WorkReport,
                  ProcessingBatch, DeliveryMilestone, ContractPriceVersion,
                  Contract, Item, Supplier):
            m.objects.all().delete()

        supplier = Supplier.objects.create(code="SUP-01", name="苏州精工五金有限公司")
        raw = Item.objects.create(code="CU-WIRE", name="磷铜线材", kind=Item.RAW,
                                  unit="米", unit_cost=D("6.00"))
        product = Item.objects.create(code="CONN-01", name="接线端子", kind=Item.PRODUCT,
                                      unit="件", unit_cost=D("0"))
        contract = Contract.objects.create(
            code="C-2026-001", supplier=supplier, raw_material=raw, product=product,
            conversion_ratio=D("3.0000"), agreed_loss_rate=D("0.0200"),
            signed_at=date(2026, 1, 5),
        )
        ContractPriceVersion.objects.create(contract=contract, unit_price=D("1.20"),
                                            effective_from=date(2026, 1, 1), note="年初价版")
        ContractPriceVersion.objects.create(contract=contract, unit_price=D("1.15"),
                                            effective_from=date(2026, 7, 1), note="下半年调价")
        DeliveryMilestone.objects.create(contract=contract, due_date=date(2026, 6, 30),
                                         planned_qty=D("15000"), penalty_rate_per_day=D("0.05"),
                                         penalty_cap=D("5000"))
        DeliveryMilestone.objects.create(contract=contract, due_date=date(2026, 8, 31),
                                         planned_qty=D("26000"), penalty_rate_per_day=D("0.05"),
                                         penalty_cap=D("5000"))

        batch = ProcessingBatch.objects.create(code="B-2026-001", contract=contract,
                                               issued_qty=D("10000"), issued_at=date(2026, 6, 1))
        services.post_issue(batch)

        # 报工（供应商声称 29000，仅备查，不参与结算）
        services.post_work_report(batch=batch, supplier_report_no="WR-001",
                                  qty=D("15000"), reported_at=date(2026, 6, 25))
        services.post_work_report(batch=batch, supplier_report_no="WR-002",
                                  qty=D("14000"), reported_at=date(2026, 7, 20))

        # 分批收货
        services.post_receipt(batch=batch, supplier_receipt_no="RC-001",
                              kind=Receipt.NORMAL, received_at=date(2026, 6, 28),
                              qualified_qty=D("15000"), scrap_qty=D("100"))
        # 部分不合格 + 重复回传（第二次应幂等，不重复入账）
        for _ in range(2):
            services.post_receipt(batch=batch, supplier_receipt_no="RC-002",
                                  kind=Receipt.NORMAL, received_at=date(2026, 7, 15),
                                  qualified_qty=D("9000"), rework_qty=D("800"), scrap_qty=D("600"))
        services.post_receipt(batch=batch, supplier_receipt_no="RC-003",
                              kind=Receipt.NORMAL, received_at=date(2026, 9, 5),
                              qualified_qty=D("4000"), scrap_qty=D("100"))
        services.post_receipt(batch=batch, supplier_receipt_no="RC-004",
                              kind=Receipt.REWORK_RETURN, received_at=date(2026, 9, 8),
                              qualified_qty=D("700"), scrap_qty=D("100"))

        q = services.batch_quantities(batch)
        self.stdout.write(self.style.SUCCESS(
            f"演示数据就绪：批次 {batch.code} 合格 {q['qualified_qty']} / "
            f"应产 {q['expected_output']} / 超损耗 {q['over_loss_qty']}"
        ))
