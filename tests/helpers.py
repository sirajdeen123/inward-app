"""Test helpers: Oracle-style SIT reports and a signed-in, two-factor-verified browser client."""
from __future__ import annotations

from pathlib import Path

HEADER = ["From Org", "To Org", "Delivery Id", "Shipment Period", "Shipment Date", "Item Type", "Item Code",
          "Item Desc", "Item BL", "Uom", "Volume", "Quantity Issued", "Receipt Num", "Receipt Date", "Quantity Received"]


def sit_html(path: Path, rows: list[tuple], window=("03-AUG-25", "25-JUN-26"), system_date="26-JUN-2026",
             to_org="035:Coimbatore") -> Path:
    """rows: (from_org, ship_id, shipment_date, sku, desc, bl, uom, volume, qty)."""
    def tr(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
    body = [tr(HEADER)]
    for from_org, ship, sdate, sku, desc, bl, uom, vol, qty in rows:
        body.append(tr([from_org, to_org, ship, "JUN-2026", sdate, "FG", sku, desc, bl, uom, vol, qty, "", "", ""]))
    path.write_text(
        "<html><head><title>RTF Template</title></head><body>"
        "<p>BPIL Depot Stock In Transit Report</p>"
        f"<p>From Date :: {window[0]} To Date :: {window[1]}</p><p>System Date :: {system_date}</p>"
        f"<table>{''.join(body)}</table></body></html>", encoding="utf-8")
    return path


def verified_client(client, user):
    """Sign the user in and mark the session as having passed two-factor."""
    from django_otp import DEVICE_ID_SESSION_KEY
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client
