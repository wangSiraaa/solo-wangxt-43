from decimal import Decimal

from rest_framework import serializers

from .models import Receipt


class WorkReportIn(serializers.Serializer):
    batch_code = serializers.CharField()
    supplier_report_no = serializers.CharField(max_length=64)
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    reported_at = serializers.DateField()


class ReceiptIn(serializers.Serializer):
    batch_code = serializers.CharField()
    supplier_receipt_no = serializers.CharField(max_length=64)
    kind = serializers.ChoiceField(choices=[c[0] for c in Receipt.KIND_CHOICES],
                                   default=Receipt.NORMAL)
    received_at = serializers.DateField()
    qualified_qty = serializers.DecimalField(max_digits=14, decimal_places=3,
                                             min_value=Decimal("0"), default=Decimal("0"))
    rework_qty = serializers.DecimalField(max_digits=14, decimal_places=3,
                                          min_value=Decimal("0"), default=Decimal("0"))
    scrap_qty = serializers.DecimalField(max_digits=14, decimal_places=3,
                                         min_value=Decimal("0"), default=Decimal("0"))


class ConfirmIn(serializers.Serializer):
    party = serializers.ChoiceField(choices=["supplier", "us"])


class TransferIn(serializers.Serializer):
    batch_code = serializers.CharField()
    code = serializers.CharField(max_length=32)
    to_supplier_code = serializers.CharField()
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    transferred_at = serializers.DateField()
    rework_unit_price = serializers.DecimalField(max_digits=16, decimal_places=4,
                                                 min_value=Decimal("0"))


class TransferReceiptIn(serializers.Serializer):
    transfer_code = serializers.CharField()
    receipt_no = serializers.CharField(max_length=64)
    received_at = serializers.DateField()
    qualified_qty = serializers.DecimalField(max_digits=14, decimal_places=3,
                                             min_value=Decimal("0"), default=Decimal("0"))
    scrap_qty = serializers.DecimalField(max_digits=14, decimal_places=3,
                                         min_value=Decimal("0"), default=Decimal("0"))


class DeterminationIn(serializers.Serializer):
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    reference_date = serializers.DateField()
    note = serializers.CharField(max_length=256)
