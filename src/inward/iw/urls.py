from django.urls import path

from inward.iw import views, views_link, views_outbound

app_name = "iw"
urlpatterns = [
    path("", views.iw_list, name="list"),
    path("new/", views.iw_new, name="new"),
    path("<int:pk>/", views.iw_detail, name="detail"),
    path("<int:pk>/edit/", views.iw_edit, name="edit"),
    path("<int:pk>/void/", views.iw_void, name="void"),
    path("<int:pk>/discard/", views.iw_discard, name="discard"),
    path("<int:pk>/link/ship/", views_link.link_ship, name="link_ship"),
    path("links/<int:link_pk>/lscrr/", views_link.lscrr, name="lscrr"),
    path("links/<int:link_pk>/remove/", views_link.unlink_ship, name="unlink_ship"),
    path("<int:pk>/colour-bank/add/", views_link.add_colour_bank, name="add_colour_bank"),
    path("colour-bank/<int:item_pk>/remove/", views_link.remove_colour_bank, name="remove_colour_bank"),
    path("<int:pk>/non-stock/add/", views_link.add_non_stock, name="add_non_stock"),
    path("non-stock/<int:ref_pk>/remove/", views_link.remove_non_stock, name="remove_non_stock"),
    path("<int:pk>/outbound/", views_outbound.outbound_page, name="outbound"),
    path("<int:pk>/outbound/ships/add/", views_outbound.outbound_add_ship, name="outbound_add_ship"),
    path("outbound/ships/<int:ship_pk>/remove/", views_outbound.outbound_remove_ship, name="outbound_remove_ship"),
    path("<int:pk>/outbound/upload/", views_outbound.outbound_upload, name="outbound_upload"),
    path("<int:pk>/outbound/assign/", views_outbound.outbound_assign, name="outbound_assign"),
    path("<int:pk>/vendor/", views_outbound.vendor_page, name="vendor"),
    path("vendor/<int:cons_pk>/remove/", views_outbound.vendor_remove, name="vendor_remove"),
]
