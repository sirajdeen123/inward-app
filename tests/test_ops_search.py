"""Screens 06 / 07 - Operations Control Centre, pending actions, universal search (SoT 10, 11)."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.iw import erp, ops, search
from inward.iw.models import Iw, MailMessage
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_erp_recon import at_erp  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import EASYCLEAN
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def test_actions_follow_the_status(world):  # noqa: F811
    iw = Iw.objects.get(pk=world["iw"].pk)
    assert "Tally every outbound Ship" in ops.actions(iw)[0].text
    from tests.test_checklist import _tally
    _tally(world)
    assert ops.actions(Iw.objects.get(pk=iw.pk))[0].text == "Generate the unloading checklist"


def test_actions_at_erp_stage(at_erp):
    w = at_erp
    texts = [a.text for a in ops.actions(w["iw"])]
    assert any("enter the Stock Receipt No" in t for t in texts)
    assert f"Ship {w['link'].ship_id}: record the LSCCR No" in texts
    erp.set_lscrr(w["clerk"], w["link"].pk, "LSC1")
    texts = [a.text for a in ops.actions(Iw.objects.get(pk=w["iw"].pk))]
    assert any(t.endswith("created by LSCCR LSC1") for t in texts)


def test_progress_strip(at_erp):
    p = dict(ops.progress(at_erp["iw"]))
    assert p["Verified"] == "done" and p["MRA"] == "current" and p["Closed"] == "todo"


def test_dashboard_and_mail_alert(at_erp):
    w = at_erp
    d = ops.dashboard()
    assert d["counts"]["open"] == 1 and d["counts"]["erp_pending"] == 1 and d["sed"]["lines"] == 2
    assert d["mail"]["show"] is False
    m = MailMessage.objects.create(iw=w["iw"], purpose="MRA", stamp="s1", subject="x", html_body="",
                                   created_by=w["clerk"])
    MailMessage.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(hours=1))
    assert ops.mail_banner()["late"] == 1


def test_search_by_any_reference(at_erp):
    w = at_erp
    iw = w["iw"]
    for q, label in [(iw.iw_no, "IW number"), ("tn 60 e 8139", "vehicle"), (w["link"].ship_id, "Ship / origin"),
                     (EASYCLEAN, "SKU on Ship"), ("XG9991", "SKU on Ship"), ("hindupur", "Ship / origin")]:
        kind, hits = search.search(q)
        assert hits and hits[0].iw == iw and label in hits[0].matched, q
    erp.add_receipt(w["clerk"], iw.pk, f"SHIP:{w['link'].pk}", "NORMAL", "42637")
    kind, hits = search.search("42637")
    assert kind == "Receipt No" and "ERP receipt" in hits[0].matched
    assert search.search("42637", status="CLOSED")[1] == []
    assert search.detect("52497520") == "Ship ID" and search.detect("TN60E8139") == "Vehicle"


def test_screens(client, at_erp):
    w = at_erp
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("web:home")).content.decode()
    assert "Operations Control Centre" in page and w["iw"].iw_no in page and "SED approval board" in page
    assert "record the LSCCR No" in c.get(reverse("iw:actions")).content.decode()
    page = c.get(reverse("iw:search"), {"q": "TN60E8139"}).content.decode()
    assert w["iw"].iw_no in page and "looks like: Vehicle" in page


def test_dossier_zip(client, at_erp):
    import io
    import zipfile
    w = at_erp
    c = verified_client(client, w["clerk"])
    resp = c.get(reverse("iw:dossier", args=[w["iw"].pk]))
    z = zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content)))
    names = z.namelist()
    assert "index.html" in names and "audit_trail.csv" in names
    assert any(n.startswith("2 Damage photos/") for n in names)
    assert w["iw"].iw_no in z.read("index.html").decode()
