from django.urls import path

from inward.rma import views

app_name = "rma"
urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("grn/<int:pk>/", views.grn, name="grn"),
    path("grn/<int:pk>/link/", views.grn_link, name="grn_link"),
    path("grn/<int:pk>/pdf/", views.grn_pdf, name="grn_pdf"),
    path("request/<str:request_id>/", views.detail, name="detail"),
    path("request/<str:request_id>/irr/", views.irr_add, name="irr_add"),
    path("request/<str:request_id>/irr/<int:link_pk>/remove/", views.irr_remove, name="irr_remove"),
    path("request/<str:request_id>/bill-to/", views.bill_to, name="bill_to"),
    path("request/<str:request_id>/close/", views.close, name="close"),
    path("request/<str:request_id>/track/", views.track, name="track"),
    path("request/<str:request_id>/handover/", views.handover, name="handover"),
    path("approvers/", views.approvers, name="approvers"),
    path("register.xlsx", views.register_xlsx, name="register"),
    path("daily-report/", views.daily_preview, name="daily_preview"),
]
