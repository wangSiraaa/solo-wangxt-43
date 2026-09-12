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
    """合同价版：同一合同不同生效日期的加工单价。"""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="price_versions")
    unit_price = models.DecimalField(**MONEY)
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
    QUALIFIED = "QUALIFIED"        # 合格加工费
    OVER_LOSS = "OVER_LOSS"        # 超约定损耗扣款
    LATE_PENALTY = "LATE_PENALTY"  # 超期扣款

    settlement = models.ForeignKey(Settlement, on_delete=models.CASCADE, related_name="lines")
    line_type = models.CharField(max_length=16)
    receipt = models.ForeignKey(Receipt, on_delete=models.SET_NULL, null=True, blank=True)
    milestone = models.ForeignKey(DeliveryMilestone, on_delete=models.SET_NULL, null=True, blank=True)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    amount = models.DecimalField(**MONEY)  # 扣款为负数
    note = models.CharField(max_length=256, blank=True)
