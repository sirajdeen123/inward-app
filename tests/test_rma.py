"""Material returns module (21-Oct-2026): GRN -> portal request -> approver mails -> CN -> IRR -> closure."""
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest
from django.urls import reverse
from django.utils import timezone

from inward.core.models import FeatureFlag, ImportBatch, NotificationRecipient
from inward.importers.common import ImportFailed
from inward.iw.models import MailMessage
from inward.rma import importers as imp
from inward.rma import mails
from inward.rma import services as svc
from inward.rma.models import Approver, Grn, GrnLine, Request, StageEvent
from tests.conftest import sample
from tests.helpers import verified_client

pytestmark = pytest.mark.django_db

GRN_TEXT = """GOODS RETURN NOTE
BERGER PAINTS INDIA LTD Return Depot : 035:Coimbatore Original S.F.NO.9/1A2, THUDIYALUR POST
Coimbatore Pin-641034
Transporter : RAINBOW LOGISTICS ( PRIMARY ) Receipt  No : RTN/2627/287Tamil Nadu Tin-33680720088 Receipt Date : 17/09/2026
Cst-41040 Ph- 9244545442 Vehicle No. : OTHERS - CARRY Receipt Time : 10:30:33
TRX Type : RMA Payment Term : PAID - RATE PER KG
TRX No TRX Date Dealer Summary
12403 31-AUG-26
1187422 - NATARAJA MARKETING
CORPORATION A/C VAISHNAVI
ENTERPRISES (1792722)
Inv Nop Ret Nop Drum/Bkt Crtn Bags Loose Wt
F006980D98020000 - SEAL-O-PRIME-HS-20 L 4 4 4 0 0 0 117
F00D390991020000 - WC LONGLIFE 10 P0 New BS-.-20 L 4 4 4 0 0 0 99
TOTAL 8 0 0 0 216
Sign of Godown Keeper Sign of Transporter"""


def test_grn_page_is_read():
    g = imp.parse_grn_page(GRN_TEXT)
    assert (g.grn_no, g.grn_date, g.depot, g.dealer_code, g.site_code) == ("RTN/2627/287", date(2026, 9, 17), "035",
                                                                          "1187422", "1792722")
    assert g.dealer_name == "NATARAJA MARKETING CORPORATION A/C VAISHNAVI ENTERPRISES"
    assert [(ln.invoice_no, ln.sku, ln.ret_nop, ln.weight) for ln in g.lines] == [
        ("12403", "F006980D98020000", 4, 117), ("12403", "F00D390991020000", 4, 99)]
    assert g.lines[1].description == "WC LONGLIFE 10 P0 New BS-.-20 L" and g.lines[0].invoice_date == date(2026, 8, 31)
    assert imp.parse_grn_page("some other document") is None


@pytest.mark.samples
def test_real_grn_pdfs():
    one = imp.parse_grn_pdf(sample("RMA/280 GRN.pdf").read_bytes())
    bulk = imp.parse_grn_pdf(sample("RMA/BPILREPORT_DSP_RETURN_883099566_1.PDF").read_bytes())
    assert [g.grn_no for g in one] == ["RTN/2627/280"]
    assert [g.grn_no for g in bulk] == [f"RTN/2627/{n}" for n in (292, 287, 288, 289, 290, 291)]
    assert all(g.lines and g.site_code for g in one + bulk)

# ---------------------------------------------------------------- fixtures: report files


def _book(path, sheets: dict):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


SUM_HEAD = ["Request Id", "Region", "Depot", "Dealer", "Invoice No", "Invoice Date", "Total Value", "Return Type",
            "Material Receipt Date", "Status", "Pending At", "Requested By", "Requested Date", "ADMIN Action By",
            "ADMIN Action Date", "RSM Action By", "RSM Action Date", "RCM Action By", "RCM Action Date", "RMA No",
            "RMA Date", "CN No", "CN Date", "CN Value", "Oracle Status", "Oracle Error"]
DET_HEAD = ["Request Id", "Region", "Depot", "Dealer", "Invoice No", "Invoice Date", "SKU Code", "SKU Desc", "Quantity",
            "Value", "Volume", "Return Type", "Status", "RMA Depot", "Sub Inventory", "Loactor", "RMA No", "RMA Date"]


def portal(tmp_path, name, reqs, depot="Coimbatore-035"):
    """reqs: (request_id, dealer, invoice, status, pending_at, rma_no, [(sku, qty, value)])"""
    summ = [["Report as on - 19/09/2026 | From: 01/06/2026 To: 19/09/2026", None, None, "RMA Summary Report"], SUM_HEAD]
    det = [["Report as on - 19/09/2026"], DET_HEAD]
    for rid, dealer, inv, status, pending, rma, lines in reqs:
        total = sum(v for _, _, v in lines)
        summ.append([rid, "S2-TN", depot, dealer, inv, datetime(2026, 8, 31), total, "DAMAGE CUSTOMER RETURN", None,
                     status, pending, "Clerk", datetime(2026, 9, 18), None, None, None, None, None, None, rma,
                     datetime(2026, 9, 25) if rma else None, None, None, None, None, None])
        for sku, qty, val in lines:
            det.append([rid, "S2-TN", depot, dealer, inv, datetime(2026, 8, 31), sku, "PAINT", str(qty), val, 80,
                        "DAMAGE CUSTOMER RETURN", status, None, "035RCV", None, rma, None])
    return _book(tmp_path / name, {"RMA Summary": summ, "RMA Details": det})


def cn_report(tmp_path, rma, lines, org="035"):
    rows = [["XXGST BPIL RMA CN INV REPORT"], [], ["From Date 01-JUN-26 to 18-SEP-26"], [f"Organization Code :: {org}"], [],
            ["Cust Code", "Cust Name", "Bill To", None, "Return Order", None, "Inv\xa0 Order", None, "Cn\xa0 Nunber", None,
             "Cn\xa0 Date", None, "Inv\xa0 Number", None, "Inv Date", None, None, "Return\xa0 Reason", None, "Sku", None,
             "Pack", None, "Uom", None, "CN Nop", None, "INV\xa0 Nop", None, "Cn Rate"]]
    for sku, nop in lines:
        r = [1187422, "NATARAJA", 1792722, None, rma, None, 1, None, "035-1800", None, datetime(2026, 9, 26), None,
             "035-12403", None, datetime(2026, 8, 31), None, None, "DAMAGE", None, sku, None, 20, None, "L", None, nop,
             None, nop, None, 1000]
        rows += [r, list(r)]          # Oracle repeats the line - counted once
    return _book(tmp_path / "cn.xlsx", {"CN": rows})


@pytest.fixture
def env(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    for label, mail in (("Admin Officer", "ao@example.com"), ("Godown", "godown@example.com"),
                        ("CFA Manager", "cfa@example.com"), ("RCM", "rcm@example.com")):
        NotificationRecipient.objects.filter(role_label=label).update(email=mail)
    clerk, cfa = make_user("clerk_rma", "DEPOT_CLERK"), make_user("cfa_rma", "CFA_MANAGER")
    _book(tmp_path / "cust.xlsx", {"S": [["BPIL Customer Master Summary Report"], ["Location: 035:Coimbatore"], [],
                                         ["TERRITORY", "BL TYPE", "ACCOUNT NUMBER", "CUSTOMER NAME", "SITE TYPE",
                                          "SITE CODE", "CUSTOMER TYPE"],
                                         ["S.S1.035.19 ", "Retail", 1187422, "NATARAJA", "BILL_TO", 1792722, "Dealer"],
                                         ["S.S1.035.29 ", "Retail", 1187422, "NATARAJA", "BILL_TO", 2129131, "Site Ops"]]})
    _book(tmp_path / "tsi.xlsx", {"S": [["Territory", "Sales rep", "BL", "Email"], [19, "Prasad", "RETAIL", "prasath@example.com>"],
                                        [29, "Other", "RETAIL", None]]})
    svc.import_customer_master(clerk, tmp_path / "cust.xlsx", "cust.xlsx")
    svc.import_tsi(clerk, tmp_path / "tsi.xlsx", "tsi.xlsx")
    return {"clerk": clerk, "cfa": cfa, "tmp": tmp_path}


def _grn(env, monkeypatch, text=GRN_TEXT):
    monkeypatch.setattr(imp, "parse_grn_pdf", lambda data: [imp.parse_grn_page(text)])
    return svc.upload_grn(env["clerk"], b"%PDF-1.4 test", "grn.pdf")


LINES = [("F006980D98020000", 4, 40000), ("F00D390991020000", 4, 36000)]


def test_full_flow(env, monkeypatch):
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    res = _grn(env, monkeypatch)
    g = res["created"][0]
    assert g.grn_no == "RTN/2627/287" and g.lines.count() == 2 and not res["linked"]
    assert _grn(env, monkeypatch)["skipped"] == ["RTN/2627/287"]                   # the same GRN again
    assert svc.SalesOfficers().for_site("1792722") == ("Prasad", "prasath@example.com", "19")
    assert [u["invoice_no"] for u in svc.unlinked_grns()] == ["12403"]

    # the clerk raised the portal request; the upload links it to the GRN (dealer + invoice)
    r1 = svc.import_portal(clerk, portal(tmp, "p1.xlsx", [("28990", "1187422-NATARAJA", "12403", "ADMIN Pending",
                                                         "Muraleedharan K M", "", LINES)]), "p1.xlsx")
    assert (r1["new"], r1["linked"], r1["unmapped"]) == (1, 1, 1)
    r = Request.objects.get(pk="28990")
    assert GrnLine.objects.filter(request=r).count() == 2 and svc.stage(r) == "APPROVAL"
    assert StageEvent.objects.get().notified            # first upload ever: the 09:30 digest tells them
    with pytest.raises(svc.RmaError, match="already uploaded"):
        svc.import_portal(clerk, tmp / "p1.xlsx", "p1.xlsx")

    # map the approver; the 09:30 digest goes to him, Cc Admin Officer + godown + Sales Officer
    a = Approver.objects.get(key="MURALEEDHARAN K M")
    svc.save_approver(cfa, a.pk, "murali@example.com", True)
    morning = timezone.make_aware(datetime(2026, 9, 26, 9, 0))
    assert mails.due(morning) == {}
    out = mails.due(timezone.make_aware(datetime(2026, 9, 26, 9, 40)))
    assert out["digests"] == 1 and out["daily"] == 1
    m = MailMessage.objects.get(purpose="RMA_APPR")
    assert m.to == ["murali@example.com"] and m.cc == ["ao@example.com", "godown@example.com", "prasath@example.com"]
    assert "28990" in m.html_body and "pending your approval" in m.subject
    d = MailMessage.objects.get(purpose="RMA_DAILY")
    assert d.to == ["ao@example.com"] and d.cc == ["cfa@example.com", "rcm@example.com"] and d.attachments.count() == 1
    assert mails.due(timezone.make_aware(datetime(2026, 9, 26, 11, 0))) == {}      # once a day only

    # later the same day the request moves to the DM: a 'new pending' mail to him only
    svc.save_approver(cfa, svc.approver_for("S Muthusamy").pk, "dm@example.com", True)
    svc.import_portal(clerk, portal(tmp, "p2.xlsx", [("28990", "1187422-NATARAJA", "12403", "DM Pending",
                                                    "S Muthusamy", "", LINES)]), "p2.xlsx")
    assert mails.due(timezone.make_aware(datetime(2026, 9, 26, 15, 0))) == {"new": 1}
    new = MailMessage.objects.filter(purpose="RMA_APPR").latest("id")
    assert new.to == ["dm@example.com"] and "New material return" in new.subject

    # RCM copy switched off -> next day's report goes without the RCM
    FeatureFlag.objects.filter(key="rma_daily_cc_rcm").update(value={"value": False})
    mails.due(timezone.make_aware(datetime(2026, 9, 27, 9, 45)))
    assert MailMessage.objects.filter(purpose="RMA_DAILY").latest("id").cc == ["cfa@example.com"]

    # approved -> RMA No, Credit Note, IRR receipt -> reconciled -> closed after the godown check
    svc.import_portal(clerk, portal(tmp, "p3.xlsx", [("28990", "1187422-NATARAJA", "12403", "Processed", "", "15999",
                                                    LINES)]), "p3.xlsx")
    r.refresh_from_db()
    assert svc.stage(r) == "CN"
    res = svc.import_cn(clerk, cn_report(tmp, 15999, [("F006980D98020000", 4), ("F00D390991020000", 4)]), "cn.xlsx")
    assert (res["lines"], res["duplicates"]) == (2, 2) and svc.stage(r) == "IRR"
    from inward.irr.models import IrrTransaction
    b = ImportBatch.objects.create(kind="IRR", source="MANUAL", filename="irr.xlsx", sha256="x" * 64)
    for sku in ("F006980D98020000", "F00D390991020000"):
        IrrTransaction.objects.create(batch=b, type="Rma Receipt", type_key="RMA RECEIPT", trx_number="43100",
                                      trx_date=date(2026, 9, 27), sku=sku, no_of_pack=Decimal(4),
                                      transaction_type="Receipt", source_subinv="035RMA", source_locator="WH1.RM.C1.1")
    assert svc.irr_candidates(r)[0]["trx"] == "43100"
    with pytest.raises(svc.RmaError, match="not an 'Rma Receipt'"):
        svc.add_irr(clerk, "28990", "999")
    assert svc.auto_link_irr() == 1            # one TRX, same SKUs and pieces as the Credit Note -> linked by itself
    link = r.irr_links.get()
    assert (link.trx_number, link.auto, link.entered_by) == ("43100", True, None) and svc.stage(r) == "CLOSE"
    rec = svc.reconcile(r)
    assert rec["status"] == "MATCH" and rec["credited_value"] == "8000.00"
    assert "pick Good / Damaged" in " ".join(svc.close_blockers(r, rec))
    svc.update_grn(clerk, g.pk, {"gate_entry_no": "ge-77", "transport_mode": "DEALER", "freight": "350"},
                   {ln.pk: "GOOD" for ln in g.lines.all()})
    with pytest.raises(Exception):
        svc.close(clerk, "28990", "Murugan", "")                 # the clerk cannot close
    svc.close(cfa, "28990", "Murugan", "")
    r.refresh_from_db()
    assert r.closed_at and r.close_kind == "RECONCILED" and svc.stage(r) == "CLOSED"

    # closure record (29-Oct-2026): request date, who raised it, every approval level, RMA No, CN, GRN, IRR, check, closure
    t = svc.trail(r)
    steps = {x["step"]: x for x in t["steps"]}
    assert steps["Request raised on the RMA Portal"]["who"] == "Clerk"
    assert steps["Request raised on the RMA Portal"]["when"] == date(2026, 9, 18)
    assert steps["Admin approval"]["who"] == "Muraleedharan K M" and steps["Admin approval"]["seen"]
    assert steps["DM approval"]["who"] == "S Muthusamy" and steps["DM approval"]["state"] == "DONE"
    assert steps["RMA No created in Oracle"]["who"] == "15999" and steps["RMA No created in Oracle"]["day"] == 7
    assert steps["Credit Note to the dealer"]["who"] == "035-1800" and "8,000.00" in steps["Credit Note to the dealer"]["note"]
    assert steps["Received in Oracle (IRR Rma Receipt)"]["who"] == "43100"
    assert steps["Physical check by the godown"]["who"] == "Murugan"
    assert steps["Closed in the Stock Inward app"]["state"] == "DONE" and "Reconciled" in steps["Closed in the Stock Inward app"]["note"]
    assert all(x["state"] == "DONE" for x in t["steps"]) and t["to_rma"] == 7 and t["to_cn"] == 8
    from inward.rma import report
    page = report.html(r)
    assert "CLOSURE RECORD" in page and "Turnaround" in page and "Portal request date" in page and "Prasad" in page


def test_same_portal_report_again_says_no_change(client, env):
    """29-Oct-2026: a second export of the same report showed '0 new, 0 moved' - now it says it is the same."""
    reqs = [("61107", "1187422-NATARAJA", "500", "DM Pending", "Jagan V", "", LINES)]
    c = verified_client(client, env["clerk"])
    with open(portal(env["tmp"], "a.xlsx", reqs), "rb") as f:
        page = c.post(reverse("rma:upload"), {"kind": "PORTAL", "file": f}, follow=True).content.decode()
    assert "1 new" in page and "Now: Approval pending 1" in page
    ImportBatch.objects.filter(kind="RMA_PORTAL").update(sha256="another export")   # a new file, same content
    with open(env["tmp"] / "a.xlsx", "rb") as f:
        page = c.post(reverse("rma:upload"), {"kind": "PORTAL", "file": f}, follow=True).content.decode()
    assert "same as the last upload - no request changed" in page and "Now: Approval pending 1" in page
    res = svc.import_portal(env["clerk"], portal(env["tmp"], "b.xlsx", [("61107", "1187422-NATARAJA", "500", "Processed",
                                                                         "", "16043", LINES)]), "b.xlsx")
    assert not res["same"] and res["changed"] == 1


def test_mismatch_needs_a_reason_and_rejected_closure(env, monkeypatch):
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    _grn(env, monkeypatch)
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [
        ("28990", "1187422-NATARAJA", "12403", "Processed", "", "15999", [("F006980D98020000", 4, 1), ("F00D390991020000", 3, 1)]),
        ("28991", "1187422-NATARAJA", "99999", "DM Rejected", "", "", [("F006980D98020000", 1, 1)])]), "p.xlsx")
    svc.import_cn(clerk, cn_report(tmp, 15999, [("F006980D98020000", 4), ("F00D390991020000", 3)]), "cn.xlsx")
    from inward.irr.models import IrrTransaction
    b = ImportBatch.objects.create(kind="IRR", source="MANUAL", filename="irr.xlsx", sha256="y" * 64)
    IrrTransaction.objects.create(batch=b, type="Rma Receipt", type_key="RMA RECEIPT", trx_number="43101",
                                  trx_date=date(2026, 9, 27), sku="F006980D98020000", no_of_pack=Decimal(4))
    IrrTransaction.objects.create(batch=b, type="Rma Receipt", type_key="RMA RECEIPT", trx_number="43101",
                                  trx_date=date(2026, 9, 27), sku="F00D390991020000", no_of_pack=Decimal(3))
    svc.add_irr(clerk, "28990", "43101")
    g = Grn.objects.get()
    svc.update_grn(clerk, g.pk, {"gate_entry_no": "1"}, {ln.pk: "DAMAGED" for ln in g.lines.all()})
    r = Request.objects.get(pk="28990")
    assert svc.reconcile(r)["status"] == "MISMATCH"         # GRN says 4 pieces of the second SKU, the rest 3
    with pytest.raises(svc.RmaError, match="accept the difference"):
        svc.close(cfa, "28990", "Murugan", "")
    svc.close(cfa, "28990", "Murugan", "", "one pail short-credited, dealer informed", accept=True)
    assert Request.objects.get(pk="28990").close_kind == "ACCEPTED"
    with pytest.raises(svc.RmaError, match="what happened"):
        svc.close_rejected(cfa, "28991", "")
    svc.close_rejected(cfa, "28991", "sent back to the dealer on 30-09")
    assert Request.objects.get(pk="28991").close_kind == "REJECTED"


def test_other_depot_files_refused(env):
    tmp = env["tmp"]
    with pytest.raises(ImportFailed, match="not Depot 035"):
        imp.parse_portal(portal(tmp, "mdu.xlsx", [("1", "1-X", "1", "DM Pending", "A", "", LINES)], depot="Madurai-037"))
    with pytest.raises(ImportFailed, match="not Depot 035"):
        imp.parse_cn(cn_report(tmp, 1, [("F006980D98020000", 1)], org="037"))
    other = GRN_TEXT.replace("Return Depot : 035:Coimbatore", "Return Depot : 037:Madurai")
    assert imp.parse_grn_page(other).depot == "037"


def test_left_approver_redirects(env, monkeypatch):
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [("1", "1187422-NATARAJA", "5", "RSM Pending", "Old Rsm , B Maniganda",
                                                    "", LINES)]), "p.xlsx")
    old, b = Approver.objects.get(key="OLD RSM"), Approver.objects.get(key="B MANIGANDA")
    new = svc.approver_for("New Rsm")
    svc.save_approver(cfa, new.pk, "new@example.com", True)
    svc.save_approver(cfa, b.pk, "bm@example.com", True)
    with pytest.raises(svc.RmaError, match="replacement"):
        svc.save_approver(cfa, old.pk, "", False)
    svc.save_approver(cfa, old.pk, "", False, str(new.pk))
    groups, unmapped = svc.pending_by_approver(svc.rows())
    assert sorted(g["approver"].email for g in groups) == ["bm@example.com", "new@example.com"] and not unmapped


def test_screens(client, env, monkeypatch):
    _grn(env, monkeypatch)
    svc.import_portal(env["clerk"], portal(env["tmp"], "p.xlsx", [("28990", "1187422-NATARAJA", "12403", "ADMIN Pending",
                                                               "Jagan V", "", LINES)]), "p.xlsx")
    c = verified_client(client, env["cfa"])
    page = c.get(reverse("rma:index")).content.decode()
    assert "Material returns" in page and "28990" in page and "without an e-mail" in page
    for kind in ("GRN", "PORTAL", "CN", "CUSTOMER", "TSI"):          # one visible upload per file (UAT 21-Oct-2026)
        assert f'id="up-{kind}"' in page
    assert "Dealer master (Customer Master)" in page and "TSI Master" in page and "2 Bill-To sites" in page
    assert "RTN/2627/287" in c.get(reverse("rma:index") + "?stage=GRN").content.decode()
    g = Grn.objects.get()
    assert "Pick Good / Damaged" in c.get(reverse("rma:grn", args=[g.pk])).content.decode()
    resp = c.post(reverse("rma:grn", args=[g.pk]), {"gate_entry_no": "5", "transport_mode": "CFA",
                                                    **{f"cond_{ln.pk}": "GOOD" for ln in g.lines.all()}}, follow=True)
    assert "saved" in resp.content.decode()
    page = c.get(reverse("rma:detail", args=["28990"])).content.decode()
    assert "Jagan V" in page and "no e-mail" in page and "Prasad" in page
    assert c.get(reverse("rma:register")).status_code == 200
    assert "Approval pending - by approver" in c.get(reverse("rma:daily_preview")).content.decode()
    a = Approver.objects.get(key="JAGAN V")
    c.post(reverse("rma:approvers"), {f"email_{a.pk}": "jagan@example.com", f"redirect_{a.pk}": "",
                                      "so_29": "other@example.com"})
    assert Approver.objects.get(pk=a.pk).email == "jagan@example.com"
    assert svc.Territory.objects.get(pk="29").mail == "other@example.com"
    assert "Copy the <b>RCM</b>" in c.get(reverse("core:notifications")).content.decode()
    clerk = verified_client(client, env["clerk"])
    assert clerk.get(reverse("rma:approvers")).status_code == 403
    assert "Material returns" in clerk.get(reverse("rma:index")).content.decode()



def test_go_live_baseline(env, monkeypatch):
    """Go-live 22-Oct-2026: only pending requests (and those raised from go-live) are chased; a GRN link tracks one."""
    clerk, tmp = env["clerk"], env["tmp"]
    _grn(env, monkeypatch)
    res = svc.import_portal(clerk, portal(tmp, "p.xlsx", [
        ("1", "1187422-NATARAJA", "500", "DM Pending", "Jagan V", "", LINES),
        ("2", "1187422-NATARAJA", "501", "Processed", "", "15000", LINES),
        ("3", "1187422-NATARAJA", "502", "ADMIN Rejected", "", "", LINES)]), "p.xlsx")
    assert res["before_go_live"] == 2
    assert {x.r.request_id for x in svc.rows()} == {"1"}
    assert set(Request.objects.filter(tracked=False).values_list("pk", flat=True)) == {"2", "3"}
    g = Grn.objects.get()
    svc.link_invoice(clerk, g.pk, "12403", "2")          # the clerk links the old GRN -> back in the flow
    assert Request.objects.get(pk="2").tracked and {x.r.request_id for x in svc.rows()} == {"1", "2"}
    svc.set_tracked(clerk, "3")
    assert Request.objects.get(pk="3").tracked
    FeatureFlag.objects.filter(key="rma_go_live").update(value={"value": "2026-09-01"})
    svc.import_portal(clerk, portal(tmp, "q.xlsx", [("4", "1187422-NATARAJA", "503", "Processed", "", "15001", LINES)]),
                      "q.xlsx")
    assert Request.objects.get(pk="4").tracked           # raised after the go-live date


def test_auto_irr_link_only_when_certain(env, monkeypatch):
    clerk, tmp = env["clerk"], env["tmp"]
    _grn(env, monkeypatch)
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [("28990", "1187422-NATARAJA", "12403", "Processed", "", "15999",
                                                    LINES)]), "p.xlsx")
    from inward.irr.models import IrrTransaction
    b = ImportBatch.objects.create(kind="IRR", source="MANUAL", filename="irr.xlsx", sha256="z" * 64)
    for trx in ("500", "501"):                             # two receipts that both fit -> do not guess
        for sku in ("F006980D98020000", "F00D390991020000"):
            IrrTransaction.objects.create(batch=b, type="Rma Receipt", type_key="RMA RECEIPT", trx_number=trx,
                                          trx_date=date(2026, 9, 27), sku=sku, no_of_pack=Decimal(4))
    svc.import_cn(clerk, cn_report(tmp, 15999, [("F006980D98020000", 4), ("F00D390991020000", 4)]), "cn.xlsx")
    r = Request.objects.get(pk="28990")
    assert not r.irr_links.exists() and len(svc.irr_candidates(r)) == 2
    IrrTransaction.objects.filter(trx_number="501").delete()
    assert svc.auto_link_irr() == 1 and r.irr_links.get().trx_number == "500"


def test_steps_and_close_from_the_list(client, env, monkeypatch):
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    _grn(env, monkeypatch)
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [("28990", "1187422-NATARAJA", "12403", "Processed", "", "15999",
                                                    LINES)]), "p.xlsx")
    c = verified_client(client, cfa)
    page = c.get(reverse("rma:detail", args=["28990"])).content.decode()
    assert 'class="steps"' in page and "Next: Fill the GRN" in page
    from inward.irr.models import IrrTransaction
    b = ImportBatch.objects.create(kind="IRR", source="MANUAL", filename="irr.xlsx", sha256="w" * 64)
    for sku in ("F006980D98020000", "F00D390991020000"):
        IrrTransaction.objects.create(batch=b, type="Rma Receipt", type_key="RMA RECEIPT", trx_number="700",
                                      trx_date=date(2026, 9, 27), sku=sku, no_of_pack=Decimal(4))
    svc.import_cn(clerk, cn_report(tmp, 15999, [("F006980D98020000", 4), ("F00D390991020000", 4)]), "cn.xlsx")
    g = Grn.objects.get()
    svc.update_grn(clerk, g.pk, {"gate_entry_no": "9", "transport_mode": "CFA"}, {ln.pk: "GOOD" for ln in g.lines.all()})
    page = c.get(reverse("rma:index") + "?stage=CLOSE").content.decode()
    assert "All quantities agree" in page and "Print handover" in page and 'name="verified_by"' in page
    c.post(reverse("rma:close", args=["28990"]), {"verified_by": "Murugan"})
    assert Request.objects.get(pk="28990").close_kind == "RECONCILED"
    assert "Next:" not in c.get(reverse("rma:detail", args=["28990"])).content.decode()


def test_one_mail_per_pending_group_all_in_to(env):
    """21-Oct-2026: a request pending with several approvers -> one mail with all of them in To (any one approves)."""
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [
        ("1", "1187422-NATARAJA", "500", "DM Pending", "S Muthusamy , Vignesh Kuttalam , Jagan V", "", LINES),
        ("2", "1187422-NATARAJA", "501", "DM Pending", "S Muthusamy , Vignesh Kuttalam", "", LINES),
        ("3", "1187422-NATARAJA", "502", "RSM Pending", "Vignesh Kuttalam", "", LINES)]), "p.xlsx")
    for name, mail in (("S Muthusamy", "sm@example.com"), ("Vignesh Kuttalam", "vk@example.com")):
        svc.save_approver(cfa, svc.approver_for(name).pk, mail, True)      # Jagan V has no e-mail yet
    assert mails.due(timezone.make_aware(datetime(2026, 9, 26, 9, 45)))["digests"] == 2
    sent = {tuple(m.to): m for m in MailMessage.objects.filter(purpose="RMA_APPR")}
    both, alone = sent[("sm@example.com", "vk@example.com")], sent[("vk@example.com",)]
    assert "any one of you can approve" in both.html_body and "Jagan V" in both.html_body
    assert ">1<" in both.html_body and ">2<" in both.html_body and ">3<" not in both.html_body
    assert "any one of you" not in alone.html_body and both.cc[:2] == ["ao@example.com", "godown@example.com"]


class CnInbox:
    def __init__(self, received=None, files=()):
        self.received, self.files, self.calls = received, list(files), 0

    def fetch_latest(self, subject_contains, save_dir, extensions=(".xls", ".xlsx")):
        import shutil

        from inward.mail.transport import FetchedMail
        self.calls += 1
        if self.received is None:
            return None
        saved = []
        for f in self.files:
            target = save_dir / f"{self.received:%Y%m%d_%H%M%S}_{f.name}"
            shutil.copy(f, target)
            saved.append(target)
        return FetchedMail(message_id="<cn@x>", subject=f"{subject_contains} 035", received_at=self.received,
                           sender="oracle@bergerindia.com", attachments=saved)


def test_cn_report_fetched_from_gmail(env, monkeypatch):
    """22-Oct-2026: the RMA CN INV Report mail (subject contains 'XXGST BPIL RMA CN INV Report') is loaded at 08:30."""
    from django.core.cache import cache

    from inward.domain.fy import IST
    from inward.rma import autofetch
    clerk, tmp = env["clerk"], env["tmp"]
    _grn(env, monkeypatch)
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [("28990", "1187422-NATARAJA", "12403", "Processed", "", "15999",
                                                    LINES)]), "p.xlsx")
    cache.delete(autofetch.CACHE_KEY)
    day = timezone.localdate()
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=IST)   # noqa: E731
    assert autofetch.run(CnInbox(), at(8, 0)) == "idle"                                   # before 08:30
    empty = CnInbox()
    assert autofetch.run(empty, at(8, 35)) == "waiting" and autofetch.run(empty, at(8, 40)) == "wait"   # 10-min retry
    old = CnInbox(at(8, 31) - __import__("datetime").timedelta(days=1), [cn_report(tmp, 15999, [("F006980D98020000", 4)])])
    cache.delete(autofetch.CACHE_KEY)
    assert autofetch.run(old, at(8, 50)) == "waiting"                                    # yesterday's mail: not today's
    cache.delete(autofetch.CACHE_KEY)
    f = cn_report(tmp, 15999, [("F006980D98020000", 4), ("F00D390991020000", 4)])
    assert autofetch.run(CnInbox(at(8, 32), [f]), at(9, 0)) == "imported"
    assert ImportBatch.objects.get(kind="RMA_CN").source == "GMAIL" and svc.stage(Request.objects.get(pk="28990")) == "IRR"
    st = autofetch.today_status(day)
    assert st["ok"] and "2 line(s) of 1 credit note(s)" in st["text"]
    assert autofetch.run(CnInbox(at(8, 32), [f]), at(9, 30)) == "idle"                   # once a day
    assert "CN report from Gmail today: loaded" in __import__("django.template.loader", fromlist=["x"]).render_to_string(
        "rma/_daily.html", {"r": mails.daily_report(day)})
    out = autofetch.import_from_mail(CnInbox(at(8, 32), [f]), clerk)                    # same file again (Fetch now)
    assert out.status == "imported" and "already loaded" in out.message


def test_cn_report_late_is_still_loaded(env, monkeypatch):
    """The real mail came at 12:08 PM on 22-Oct-2026: after 11:00 it is flagged late but still loaded that day."""
    from django.core.cache import cache

    from inward.domain.fy import IST
    from inward.rma import autofetch
    cache.delete(autofetch.CACHE_KEY)
    day = timezone.localdate()
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=IST)   # noqa: E731
    assert autofetch.run(CnInbox(), at(11, 5)) == "late"
    monkeypatch.setattr(timezone, "localtime", lambda *a: at(11, 30) if not a else a[0].astimezone(IST))
    st = autofetch.today_status(day)
    assert not st["ok"] and st["text"] == "No CN report received by 11:00 AM - still checking every 10 minutes"
    cache.delete(autofetch.CACHE_KEY)
    f = cn_report(env["tmp"], 15999, [("F006980D98020000", 4)])
    assert autofetch.run(CnInbox(at(12, 8), [f]), at(12, 10)) == "imported"
    assert autofetch.today_status(day)["ok"]


def test_urgent_reminder_to_chosen_approvers_only(client, env):
    """22-Oct-2026: an urgent reminder by hand - chosen approver group(s), chosen request(s), reason, 2-hour guard."""
    clerk, cfa, tmp = env["clerk"], env["cfa"], env["tmp"]
    svc.import_portal(clerk, portal(tmp, "p.xlsx", [
        ("1", "1187422-NATARAJA", "500", "DM Pending", "S Muthusamy , Vignesh Kuttalam", "", LINES),
        ("2", "1187422-NATARAJA", "501", "DM Pending", "S Muthusamy , Vignesh Kuttalam", "", LINES),
        ("3", "1187422-NATARAJA", "502", "RSM Pending", "Jagan V", "", LINES)]), "p.xlsx")
    for name, mail in (("S Muthusamy", "sm@example.com"), ("Vignesh Kuttalam", "vk@example.com"), ("Jagan V", "jv@example.com")):
        svc.save_approver(cfa, svc.approver_for(name).pk, mail, True)
    groups = {tuple(a.email for a in g["approvers"]): g for g in mails.urgent_groups()}
    dm = groups[("sm@example.com", "vk@example.com")]
    with pytest.raises(svc.RmaError, match="why it is urgent"):
        mails.send_urgent(clerk, {dm["key"]: {"2"}}, "", "")
    sent = mails.send_urgent(clerk, {dm["key"]: {"2"}}, mails.URGENT_REASONS[0], "Nataraja account locked")
    m = sent[0].mail
    assert m.to == ["sm@example.com", "vk@example.com"] and m.subject.startswith("URGENT - material return approval needed - request 2 ")
    assert "Dealer account is locked until this return is approved - Nataraja account locked" in m.html_body
    assert ">2<" in m.html_body and ">1<" not in m.html_body                       # only the chosen request
    assert MailMessage.objects.filter(purpose="RMA_APPR").count() == 1               # Jagan V not mailed
    with pytest.raises(svc.RmaError, match="send again anyway"):
        mails.send_urgent(clerk, {dm["key"]: {"2"}}, "Dealer follow-up / escalation", "")
    assert mails.send_urgent(clerk, {dm["key"]: {"2"}}, "Dealer follow-up / escalation", "", force=True)
    c = verified_client(client, clerk)
    page = c.get(reverse("rma:urgent") + "?request=3").content.decode()
    assert "Jagan V" in page and "S Muthusamy" not in page                          # from one request's page
    jv = next(g for g in mails.urgent_groups("3"))
    resp = c.post(reverse("rma:urgent"), {"only": "3", f"g_{jv['key']}": "1", f"r_{jv['key']}_3": "1",
                                          "reason_kind": "Credit note needed urgently", "action": "preview"})
    assert "Preview - what will be sent" in resp.content.decode() and "jv@example.com" in resp.content.decode()
    c.post(reverse("rma:urgent"), {"only": "3", f"g_{jv['key']}": "1", f"r_{jv['key']}_3": "1",
                                   "reason_kind": "Credit note needed urgently", "action": "send"})
    assert MailMessage.objects.filter(purpose="RMA_APPR", to=["jv@example.com"]).exists()
    assert "Urgent reminder sent" in c.get(reverse("rma:detail", args=["3"])).content.decode()


def test_search_finds_a_request_in_any_stage(client, env):
    """29-Oct-2026: a search from the Approval tab found nothing for a request in another stage or from before
    go-live, and every tab showed 0. Now it searches all requests and opens the tab with the match."""
    svc.import_portal(env["clerk"], portal(env["tmp"], "p.xlsx", [
        ("61107", "1187422-NATARAJA", "500", "DM Pending", "Jagan V", "", LINES),
        ("60656", "1187422-NATARAJA", "501", "Processed", "", "", LINES),          # before go-live
        ("62701", "1187422-NATARAJA", "502", "DM Pending", "Jagan V", "", LINES)]), "p.xlsx")
    Request.objects.filter(pk="62701").update(status="Processed", rma_no="16043")  # tracked, waiting for the CN
    c = verified_client(client, env["cfa"])
    page = c.get(reverse("rma:index") + "?stage=APPROVAL&q=62701").content.decode()
    assert "Waiting for Credit Note · 1" in page and "62701" in page and "1 request found" in page
    page = c.get(reverse("rma:index") + "?stage=APPROVAL&q=60656").content.decode()
    assert "Before go-live · 1" in page and "60656" in page and "Reference only" in page
    page = c.get(reverse("rma:index") + "?stage=APPROVAL&q=99999").content.decode()
    assert "No request matches" in page and "Before go-live · 0" in page
    page = c.get(reverse("rma:index")).content.decode()             # no search: unchanged
    assert "Approval pending · 1" in page and "Before go-live · 1" in page
