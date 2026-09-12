from decimal import Decimal

from django.db import models

QTY = dict(max_digits=14, decimal_places=3)      # 数量
MONEY = dict(max_digits=16, decimal_places=4)    # 金额（展示层保留 2 位）
RATE = dict(max_digits=8, decimal_places=4)      # 比例/费率


class Supplier(models.Model):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=128)

    def __str__(self):
        return self.name


class Item(models.Model):
    RAW = "RAW"
    PRODUCT = "PRODUCT"
    KIND_CHOICES = [(RAW, "原材料"), (PRODUCT, "加工成品")]

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=128)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    unit = models.CharField(max_length=16)
    unit_cost = models.DecimalField(**MONEY)  # 原材料成本，用于超损耗扣款

    def __str__(self):
        return f"{self.code} {self.name}"


class Contract(models.Model):
    """委外合同：转化比例 + 约定损耗 + 价版 + 交付节点。"""

    code = models.CharField(max_length=32, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="contracts")
    raw_material = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    product = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    conversion_ratio = models.DecimalField(**RATE)   # 1 单位原料 -> N 单位成品
    agreed_loss_rate = models.DecimalField(**RATE)   # 约定损耗率，如 0.0200 = 2%
    signed_at = models.DateField()

    def __str__(self):
        return self.code


class ContractPriceVersion(models.Model):
    """合同价版：同一合同不同生效日期的加工单价与赔偿上限。"""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="price_versions")
    unit_price = models.DecimalField(**MONEY)
    compensation_cap = models.DecimalField(**MONEY, null=True, blank=True)  # None = 无上限
    effective_from = models.DateField()
    note = models.CharField(max_length=128, blank=True)

    class Meta:
        unique_together = ("contract", "effective_from")
        ordering = ["-effective_from"]


class DeliveryMilestone(models.Model):
    """交付节点：到期的累计应交付合格数，逾期按件按天扣款，封顶。"""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="milestones")
    due_date = models.DateField()
    planned_qty = models.DecimalField(**QTY)          # 累计计划合格数
    penalty_rate_per_day = models.DecimalField(**MONEY)  # 每件每天扣款
    penalty_cap = models.DecimalField(**MONEY)        # 本节点扣款上限

    class Meta:
        ordering = ["due_date"]
        unique_together = ("contract", "due_date")


class ProcessingBatch(models.Model):
    """加工批次 = 一次委外领料。所有数量流水挂在批次上。"""

    code = models.CharField(max_length=32, unique=True)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="batches")
    issued_qty = models.DecimalField(**QTY)   # 领料数（原料单位）
    issued_at = models.DateField()
    locked = models.BooleanField(default=False)  # 结算确认后锁定

    def __str__(self):
        return self.code


class WorkReport(models.Model):
    """报工：外协厂声称的产出，仅作差异对照，不参与可结算数量。"""

    batch = models.ForeignKey(ProcessingBatch, on_delete=models.CASCADE, related_name="work_reports")
    supplier_report_no = models.CharField(max_length=64, unique=True)  # 幂等键
    qty = models.DecimalField(**QTY)
    reported_at = models.DateField()


class Receipt(models.Model):
    """收货检验：分批收货，每批拆为 合格/返工/报废。
    supplier_receipt_no 唯一 —— 重复回传只入账一次。"""

    NORMAL = "NORMAL"
    REWORK_RETURN = "REWORK_RETURN"
    KIND_CHOICES = [(NORMAL, "正常收货"), (REWORK_RETURN, "返工回厂")]

    batch = models.ForeignKey(ProcessingBatch, on_delete=models.CASCADE, related_name="receipts")
    supplier_receipt_no = models.CharField(max_length=64, unique=True)  # 幂等键
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=NORMAL)
    received_at = models.DateField()
    qualified_qty = models.DecimalField(**QTY, default=Decimal("0"))
    rework_qty = models.DecimalField(**QTY, default=Decimal("0"))
    scrap_qty = models.DecimalField(**QTY, default=Decimal("0"))
    price_version = models.ForeignKey(
        ContractPriceVersion, on_delete=models.PROTECT, null=True, related_name="+"
    )


class QuantityLedgerEntry(models.Model):
    """数量流水：每一笔数量变动都入账，账面核对以此为准。"""

    ISSUE = "ISSUE"
    REPORT = "REPORT"                      # 备查，不计入可结算
    RECEIVE_QUALIFIED = "RECEIVE_QUALIFIED"
    RECEIVE_REWORK = "RECEIVE_REWORK"
    RECEIVE_SCRAP = "RECEIVE_SCRAP"
    REWORK_RETURN_QUALIFIED = "REWORK_RETURN_QUALIFIED"
    REWORK_RETURN_SCRAP = "REWORK_RETURN_SCRAP"
    TRANSFER_OUT = "TRANSFER_OUT"                    # 转厂：实物 A -> B，责任仍在 A
    TRANSFER_RETURN_QUALIFIED = "TRANSFER_RETURN_QUALIFIED"
    TRANSFER_RETURN_SCRAP = "TRANSFER_RETURN_SCRAP"  # 二次报废
    SETTLE_LOCK = "SETTLE_LOCK"

    batch = models.ForeignKey(ProcessingBatch, on_delete=models.CASCADE, related_name="ledger_entries")
    entry_type = models.CharField(max_length=32)
    qty = models.DecimalField(**QTY)
    ref_type = models.CharField(max_length=32)   # Receipt / WorkReport / Settlement ...
    ref_id = models.PositiveBigIntegerField()
    memo = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class TransferOrder(models.Model):
    """转厂单：把原厂无能力返工的不合格件转给另一家加工。
    只移动实物保管权（A -> B），质量责任仍在原厂，不算重新生产。"""

    OPEN = "OPEN"
    CLOSED = "CLOSED"

    code = models.CharField(max_length=32, unique=True)  # 幂等键
    batch = models.ForeignKey(ProcessingBatch, on_delete=models.PROTECT, related_name="transfers")
    from_supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")
    to_supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(**QTY)
    rework_unit_price = models.DecimalField(**MONEY)     # 应付新厂的返工单价
    transferred_at = models.DateField()
    price_version = models.ForeignKey(                  # 原合同版本：赔偿上限/抵扣规则来源
        ContractPriceVersion, on_delete=models.PROTECT, null=True, related_name="+"
    )
    status = models.CharField(max_length=8, default=OPEN)


class TransferReceipt(models.Model):
    """新厂返工回厂：合格计入批次合格数，报废为二次报废（原厂赔偿）。"""

    transfer = models.ForeignKey(TransferOrder, on_delete=models.PROTECT, related_name="receipts")
    receipt_no = models.CharField(max_length=64, unique=True)  # 幂等键
    received_at = models.DateField()
    qualified_qty = models.DecimalField(**QTY, default=Decimal("0"))
    scrap_qty = models.DecimalField(**QTY, default=Decimal("0"))
    price_version = models.ForeignKey(                  # 原厂加工费价版（按回厂日）
        ContractPriceVersion, on_delete=models.PROTECT, null=True, related_name="+"
    )
    rework_fee = models.DecimalField(**MONEY, default=Decimal("0"))  # 应付新厂 = 合格 × 返工单价


class LiabilityEntry(models.Model):
    """责任账：原厂应负担的 返工费/二次报废赔偿。
    同一笔账驱动两侧：新厂应收（rework_fee）与原厂抵扣/追偿，天然不会重复计。"""

    REWORK_FEE = "REWORK_FEE"
    COMPENSATION = "COMPENSATION"
    PENDING = "PENDING"      # 待抵扣（结算时冲减原厂应付）
    OFFSET = "OFFSET"        # 已在结算中抵扣
    CLAIMED = "CLAIMED"      # 超出可抵扣余额/结算后追加 -> 独立追偿

    batch = models.ForeignKey(ProcessingBatch, on_delete=models.PROTECT, related_name="liabilities")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")   # 责任方（原厂）
    counter_supplier = models.ForeignKey(                    # 应收方（新厂），赔偿时为空
        Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    entry_type = models.CharField(max_length=16)
    status = models.CharField(max_length=8, default=PENDING)
    amount = models.DecimalField(**MONEY)                # 原厂应负担总额
    offset_amount = models.DecimalField(**MONEY, default=Decimal("0"))  # 已抵扣部分
    price_version = models.ForeignKey(                   # 赔偿上限所依据的原合同版本
        ContractPriceVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    transfer_receipt = models.ForeignKey(
        TransferReceipt, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    claim = models.ForeignKey(
        "RecoveryClaim", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RecoveryClaim(models.Model):
    """独立追偿记录：可抵扣余额不足或结算后追加责任认定时形成。"""

    OPEN = "OPEN"

    code = models.CharField(max_length=40, unique=True)
    batch = models.ForeignKey(
        ProcessingBatch, on_delete=models.PROTECT, null=True, blank=True, related_name="claims"
    )
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="+")  # 被追偿方
    amount = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=256)
    status = models.CharField(max_length=8, default=OPEN)
    created_at = models.DateTimeField(auto_now_add=True)


class Settlement(models.Model):
    """结算单：双方确认差异后冻结计算快照。"""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"

    batch = models.OneToOneField(ProcessingBatch, on_delete=models.PROTECT, related_name="settlement")
    code = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=12, default=DRAFT)
    calc_hash = models.CharField(max_length=64)          # 当前草稿计算指纹
    confirmed_by_supplier = models.BooleanField(default=False)
    confirmed_by_us = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(null=True, blank=True)   # 确认后冻结，不再重算
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def both_confirmed(self):
        return self.confirmed_by_supplier and self.confirmed_by_us


class SettlementLine(models.Model):
    QUALIFIED = "QUALIFIED"          # 合格加工费
    OVER_LOSS = "OVER_LOSS"          # 超约定损耗扣款
    LATE_PENALTY = "LATE_PENALTY"    # 超期扣款
    REWORK_OFFSET = "REWORK_OFFSET"  # 返工费抵扣（原厂负担新厂返工费）
    COMPENSATION = "COMPENSATION"    # 二次报废赔偿抵扣
    CLAIM_NOTICE = "CLAIM_NOTICE"    # 超可抵扣余额转追偿（提示行，金额 0）

    settlement = models.ForeignKey(Settlement, on_delete=models.CASCADE, related_name="lines")
    line_type = models.CharField(max_length=16)
    receipt = models.ForeignKey(Receipt, on_delete=models.SET_NULL, null=True, blank=True)
    milestone = models.ForeignKey(DeliveryMilestone, on_delete=models.SET_NULL, null=True, blank=True)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    amount = models.DecimalField(**MONEY)  # 扣款为负数
    note = models.CharField(max_length=256, blank=True)
