"""Master files - download, edit in Excel, upload, preview, confirm (Phase 1, 21-Nov-2026)."""
import io

import openpyxl
import pytest
from django.urls import reverse

from inward.accounts.models import RolePermission
from inward.core import masterfiles as mf
from inward.core.models import MasterUpload
from inward.rma import services as svc
from inward.rma.models import Approver, CustomerSite, SalesOfficerChange, SalesOfficerLeft, Territory
from tests.helpers import verified_client
from tests.test_rma import env  # noqa: F401  (fixture: Customer Master + TSI loaded)

pytestmark = pytest.mark.django_db


def _edit(data: bytes, change) -> bytes:
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.worksheets[0]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    rows = change(rows)
    ws.delete_rows(2, ws.max_row)
    for r in rows:
        ws.append([r.get(h) for h in head])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_tsi_round_trip_with_resignation(client, env):  # noqa: F811
    cfa, clerk = env["cfa"], env["clerk"]
    spec = mf.REGISTRY["RMA_TSI"]
    data = mf.workbook(spec)
    ws = openpyxl.load_workbook(io.BytesIO(data)).worksheets[0]
    assert [c.value for c in ws[1]][:6] == ["Territory", "Sales Officer", "BL", "E-mail", "Status", "Change reason"]

    def change(rows):
        for r in rows:
            if str(r["Territory"]) == "19":
                r.update({"Sales Officer": "Ravi Kumar", "E-mail": "ravi@example.com", "Change reason": "Resigned"})
            if str(r["Territory"]) == "29":
                r.update({"Status": "Left"})                                   # no reason -> refused
        return rows + [{"Territory": "41", "Sales Officer": "New Man", "BL": "RETAIL", "E-mail": "new@example.com"}]
    edited = _edit(data, change)
    with pytest.raises(Exception):
        mf.upload(clerk, "RMA_TSI", edited, "tsi.xlsx")                      # upload is the CFA Manager's
    up = mf.upload(cfa, "RMA_TSI", edited, "tsi.xlsx")
    p = up.plan
    assert [x["key"] for x in p["new"]] == ["41"] and [x["key"] for x in p["changed"]] == ["19"]
    assert p["changed"][0]["reason"] == "Resigned" and len(p["errors"]) == 1 and "Resigned or Transferred" in p["errors"][0]["message"]
    assert Territory.objects.get(pk="19").officer == "Prasad"                   # nothing applied yet
    mf.confirm(cfa, up.pk)
    assert svc.SalesOfficers().territory("19") == ("Ravi Kumar", "ravi@example.com", "19")
    assert SalesOfficerLeft.objects.filter(email="prasath@example.com").exists()
    assert Territory.objects.get(pk="41").mail == "new@example.com"
    assert SalesOfficerChange.objects.filter(territory_id="19", reason="Resigned").exists()
    assert MasterUpload.objects.get(pk=up.pk).status == "APPLIED"
    with pytest.raises(mf.MasterFileError, match="already applied"):
        mf.confirm(cfa, up.pk)

    # the CFA Manager gives the upload to the clerk
    RolePermission.objects.create(role_id="DEPOT_CLERK", permission_id="MASTER_UPLOAD", mode="GRANT")
    again = mf.upload(clerk, "RMA_TSI", mf.workbook(spec), "same.xlsx")
    assert again.plan["unchanged"] == 3 and not again.plan["ops"]              # the download reads back unchanged

    c = verified_client(client, clerk)
    page = c.get(reverse("core:masterfiles")).content.decode()
    assert "TSI Master (Sales Officers)" in page and "Customer Master (dealer sites)" in page and "Upload and preview" in page
    resp = c.get(reverse("core:masterfile_download", args=["RMA_TSI"]))
    assert resp.status_code == 200 and "TSI Master (Sales Officers) Depot" in resp["Content-Disposition"]
    page = c.get(reverse("core:masterfile_preview", args=[up.pk])).content.decode()
    assert "Applied" in page and "ravi@example.com" in page


def test_customer_master_and_approvers(env):  # noqa: F811
    cfa = env["cfa"]
    svc.approver_for("Jagan V")
    svc.approver_for("B Maniganda")

    def cust(rows):
        rows[0]["Territory"] = "S.S1.035.29"                                    # dealer moves territory
        rows[1]["Status"] = "Remove"
        return rows
    up = mf.upload(cfa, "RMA_CUSTOMER", _edit(mf.workbook(mf.REGISTRY["RMA_CUSTOMER"]), cust), "c.xlsx")
    assert len(up.plan["changed"]) == 1 and len(up.plan["removed"]) == 1
    mf.confirm(cfa, up.pk)
    assert CustomerSite.objects.count() == 1 and CustomerSite.objects.get().territory_no == "29"

    def appr(rows):
        for r in rows:
            if r["Name (as in the portal)"] == "Jagan V":
                r.update({"E-mail": "jagan@example.com"})                    # first e-mail: no reason needed
            if r["Name (as in the portal)"] == "B Maniganda":
                r.update({"Status": "Left", "Mails go to (replacement)": "Jagan V"})
        return rows + [{"Name (as in the portal)": "S Ravi", "E-mail": "ravi@example.com", "Status": "Active"}]
    up = mf.upload(cfa, "RMA_APPROVERS", _edit(mf.workbook(mf.REGISTRY["RMA_APPROVERS"]), appr), "a.xlsx")
    assert [x["key"] for x in up.plan["new"]] == ["S Ravi"] and len(up.plan["left"]) == 1 and not up.plan["errors"]
    mf.confirm(cfa, up.pk)
    b = Approver.objects.get(key="B MANIGANDA")
    assert not b.active and b.redirect_to.name == "Jagan V" and Approver.objects.get(key="S RAVI").email == "ravi@example.com"

    def wrong(rows):
        for r in rows:
            if r["Name (as in the portal)"] == "Jagan V":
                r["E-mail"] = "other@example.com"                             # changed without a reason
        return rows
    up = mf.upload(cfa, "RMA_APPROVERS", _edit(mf.workbook(mf.REGISTRY["RMA_APPROVERS"]), wrong), "w.xlsx")
    assert "give the Change reason" in up.plan["errors"][0]["message"] and not up.plan["ops"]
    mf.cancel(cfa, up.pk)
    assert MasterUpload.objects.get(pk=up.pk).status == "CANCELLED"
