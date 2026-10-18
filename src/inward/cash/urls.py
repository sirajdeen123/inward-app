from django.urls import path

from inward.cash import views

app_name = "cash"
urlpatterns = [
    path("", views.register, name="register"),
    path("<int:pk>/receive/", views.receive, name="receive"),
    path("expense/", views.expense, name="expense"),
    path("handover/", views.handover, name="handover"),
    path("<int:pk>/confirm/", views.confirm, name="confirm"),
    path("opening/", views.opening, name="opening"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
    path("<int:pk>/bill/", views.bill, name="bill"),
    path("statement/", views.statement, name="statement"),
]
