"""Screen 05 part C - scans, closure gate, IRR Reconciliation Report, reopen (SoT 8.7, table 11)."""
import pytest
from django.urls import reverse

from inward.accounts.approvals import decide_and_apply
from inward.iw import closure, erp
from inward.iw.models import Iw, MailMessage
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_erp_recon import at_erp, full_receipt, irr_rows  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import EASYCLEAN, WEATHERCOAT
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.7 signed scan"


@pytest.fixture
def reconciled(at_erp):
    w, clerk = at_erp, at_erp["clerk"]
    from inward.iw import mra
    Iw.objects.filter(pk=w["iw"].pk).update(status=Iw.Status.PHYSICALLY_VERIFIED)
    rev, _ = mra.issue(clerk, w["iw"].pk, reconfirm=True)
    Iw.objects.filter(pk=w["iw"].pk).update(status=Iw.Status.MRA_ISSUED)
    for purpose in ("MRA", "SED_NOTIFY"):
        MailMessage.objects.create(iw=w["iw"], mra=rev, purpose=purpose, stamp=f"x {purpose}", subject="s",
                                   html_body="", status=MailMessage.Status.SENT, created_by=clerk)
    erp.set_lscrr(clerk, w["link"].pk, False)
    erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "NORMAL", "42637")
    full_receipt()
    memos = {m.kind: m for m in erp.memos(w["iw"])}
    erp.add_sed_trx(clerk, w["iw"].pk, memos["SHORT"].key, "3560000101")
    erp.add_sed_trx(clerk, w["iw"].pk, memos["DAMAGE"].key, "3562905913")
    irr_rows([("Miscellaneous Issue", "3560000101", EASYCLEAN, 3, "10.8", None, "Issue", ""),
              ("Subinventory Transfer", "3562905913", WEATHERCOAT, 1, "20", None, "Issue", "035DMG")])
    return w


def _gates(iw):
    return {g.label: g for g in closure.gates(Iw.objects.get(pk=iw.pk))}


def test_gates_and_close(reconciled):
    w, clerk = reconciled, reconciled["clerk"]
    g = _gates(w["iw"])
    assert g["Inbound Ships (normal + excess receipts) fully reconciled"].ok
    assert g["SED memos reconciled or LSCRR-covered"].ok and g["Mandatory mails SENT"].ok
    assert not g["Signed MRA scan"].ok and not g["Transporter LR scan"].ok
    with pytest.raises(IwError, match="Signed MRA scan"):
        closure.close(clerk, w["iw"].pk)
    with pytest.raises(IwError, match="PDF or a photo"):
        closure.add_scan(clerk, w["iw"].pk, "signed_mra", b"x", "mra.txt", "text/plain")
    closure.add_scan(clerk, w["iw"].pk, "signed_mra", PDF, "mra signed.pdf", "application/pdf")
    closure.add_scan(clerk, w["iw"].pk, "lr_scan", b"\xff\xd8 jpeg", "lr.jpg", "image/jpeg")
    snap = closure.close(clerk, w["iw"].pk)
    iw = Iw.objects.get(pk=w["iw"].pk)
    assert iw.status == Iw.Status.CLOSED and snap.number == 1 and all(x["ok"] for x in snap.gates)
    assert {r["status"] for r in snap.recon} == {"FULL"}
    with pytest.raises(IwError, match="ERP entries start"):
        erp.add_receipt(clerk, iw.pk, f"SHIP:{w['link'].pk}", "NORMAL", "42999")
    with pytest.raises(IwError, match="closed"):
        closure.add_scan(clerk, iw.pk, "lr_scan", PDF, "lr2.pdf", "application/pdf")


def test_unsent_mail_and_unreconciled_block(reconciled):
    w, clerk = reconciled, reconciled["clerk"]
    MailMessage.objects.filter(iw=w["iw"], purpose="SED_NOTIFY").update(status=MailMessage.Status.QUEUED)
    erp.remove_sed_trx(clerk, w["iw"].sed_trx.get(kind="SHORT").pk)
    g = _gates(w["iw"])
    assert not g["Mandatory mails SENT"].ok and "Internal SED notification" in g["Mandatory mails SENT"].detail
    assert not g["SED memos reconciled or LSCRR-covered"].ok


def test_reopen_needs_cfa_approval(reconciled, make_user):
    w, clerk = reconciled, reconciled["clerk"]
    closure.add_scan(clerk, w["iw"].pk, "signed_mra", PDF, "mra.pdf", "application/pdf")
    closure.add_scan(clerk, w["iw"].pk, "lr_scan", PDF, "lr.pdf", "application/pdf")
    closure.close(clerk, w["iw"].pk)
    iw = Iw.objects.get(pk=w["iw"].pk)
    req = closure.request_reopen(clerk, iw, "TRX number typed wrongly")
    with pytest.raises(IwError, match="already waiting"):
        closure.request_reopen(clerk, iw, "again")
    decide_and_apply(make_user("cfa_ro", "CFA_MANAGER"), req.pk, True)
    iw.refresh_from_db()
    assert iw.status == Iw.Status.REOPENED and iw.closures.first().reopen_reason == "TRX number typed wrongly"
    snap = closure.close(clerk, iw.pk)            # second closure = new snapshot, the first is kept
    assert snap.number == 2 and iw.closures.count() == 2


def test_screens(client, reconciled):
    w = reconciled
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:erp", args=[w["iw"].pk])).content.decode()
    assert "Final documents &amp; closure" in page and "Signed MRA (driver + depot)" in page
    from django.core.files.uploadedfile import SimpleUploadedFile
    resp = c.post(reverse("iw:scan_add", args=[w["iw"].pk]),
                  {"role": "signed_mra", "file": SimpleUploadedFile("mra.pdf", PDF, content_type="application/pdf")},
                  follow=True)
    assert "mra.pdf uploaded" in resp.content.decode()
    resp = c.post(reverse("iw:close", args=[w["iw"].pk]), follow=True)
    assert "Not ready to close" in resp.content.decode() and "Transporter LR scan" in resp.content.decode()
