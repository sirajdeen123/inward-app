from django.urls import path

from inward.accounts import views

app_name = "accounts"
urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("two-factor/", views.two_factor_view, name="two_factor"),
    path("logout/", views.logout_view, name="logout"),
    path("approvals/", views.approvals_view, name="approvals"),
    path("roles/", views.roles_view, name="roles"),
    path("approvals/<int:pk>/decide/", views.decide_view, name="decide"),
]
