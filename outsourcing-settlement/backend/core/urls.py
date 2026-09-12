from django.urls import path

from . import views

urlpatterns = [
    path("batches/", views.BatchList.as_view()),
    path("batches/<str:code>/flow/", views.BatchFlow.as_view()),
    path("batches/<str:code>/reconciliation/", views.BatchReconciliation.as_view()),
    path("batches/<str:code>/settlement/preview/", views.SettlementPreview.as_view()),
    path("batches/<str:code>/settlement/confirm/", views.SettlementConfirm.as_view()),
    path("batches/<str:code>/settlement/", views.SettlementDetail.as_view()),
    path("contracts/<str:code>/", views.ContractDetail.as_view()),
    path("work-reports/", views.WorkReportPost.as_view()),
    path("receipts/", views.ReceiptPost.as_view()),
]
