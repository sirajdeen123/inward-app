"""Screen 04 part C - MRA revisions and SED memos (SoT 7.3, 7.4)."""
import pytest
from django.urls import reverse

from inward.core import pdf
from inward.iw import linking, mra, sed, unloading
from inward.iw.models import Iw, ShipLink
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_sed import EASYCLEAN, WEATHERCOAT, _in, _jpeg
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture
def verified(ready):  # noqa: F811
    w, clerk = ready, ready["clerk"]
    sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, short_qty="3", short_value="1500"))
    dmg = sed.save_line(clerk, w["iw"].pk, _in(w, WEATHERCOAT, damage_qty="1", damage_kind="MATERIAL",
                                               damage_remarks="drum leaking", damage_value="2400"))
    sed.add_photo(clerk, dmg.pk, _jpeg(), "d.jpg")
    sed.confirm_verification(clerk, w["iw"].pk)
    unloading.save_counts(clerk, w["iw"].pk, {})
    unloading.record(clerk, w["iw"].pk, unloading.UnloadingInput(tonnage="13.2", status="PART_PAID",
                                                                 paid_amount="2000", payment_mode="CASH"))
    w["iw"] = Iw.objects.get(pk=w["iw"].pk)
    return w


def test_blockers_until_everything_recorded(unloading_iw):  # noqa: F811
    b = mra.blockers(unloading_iw["iw"])
    assert any("physical verification" in x for x in b) and any("package counts" in x for x in b)
    assert any("unloading charge" in x for x in b)
    with pytest.raises(IwError):
        mra.issue(unloading_iw["clerk"], unloading_iw["iw"].pk)


def test_content_and_revisions(verified):
    w, clerk = verified, verified["clerk"]
    rev, created = mra.issue(clerk, w["iw"].pk)
    assert created and rev.label == "R01"
    c = rev.content
    assert c["depot"]["cfa"] == "RAINBOW LOGISTICS" and c["depot"]["seal"].startswith("For RAINBOW LOGISTICS, CFA")
    assert c["sed_totals"]["claimable"] == "3900.00" and c["unloading"]["outstanding"] == "1960.00"
    assert "Retail warehouse" in c["package_counts"] and "rate" not in c["unloading"]   # rate never printed
    kinds = {m["kind"]: m for m in c["memos"]}
    assert set(kinds) == {"SHORT", "DAMAGE"}
    assert kinds["SHORT"]["rows"][0]["action"] == "Issue TRX"       # LSCRR not declared
    assert "not yet declared" in kinds["SHORT"]["rows"][0]["note"]
    assert kinds["DAMAGE"]["rows"][0]["action"] == "Subinventory transfer to DMG"
    assert mra.issue(clerk, w["iw"].pk) == (rev, False)               # unchanged: no new revision
    # a change needs a reason and makes R02
    unloading.record(clerk, w["iw"].pk, unloading.UnloadingInput(tonnage="13.2", status="PAID", payment_mode="CASH"))
    assert not mra.is_current(w["iw"], rev)
    with pytest.raises(IwError, match="Enter what changed"):
        mra.issue(clerk, w["iw"].pk)
    rev2, created = mra.issue(clerk, w["iw"].pk, "balance unloading paid")
    assert created and rev2.label == "R02" and rev2.content["unloading"]["outstanding"] == "0.00"


def test_lscrr_applied_memo(verified):
    w = verified
    ShipLink.objects.filter(iw=w["iw"]).update(lscrr_applied=True, lscrr_no="LS-77")
    c = mra.build(Iw.objects.get(pk=w["iw"].pk))
    memo = {m["kind"]: m for m in c["memos"]}
    assert memo["SHORT"]["rows"][0]["action"] == "LSCRR-covered" and memo["SHORT"]["all_lscrr"]
    assert "LS-77" in memo["DAMAGE"]["rows"][0]["note"]


def test_missing_value_blocks(verified):
    w = verified
    sed.reopen_verification(w["clerk"], w["iw"].pk, "recount")
    line = w["iw"].sed_lines.get(sku=EASYCLEAN)
    line.short_value = None
    line.save()
    sed.confirm_verification(w["clerk"], w["iw"].pk)
    assert any("financial value" in b for b in mra.blockers(Iw.objects.get(pk=w["iw"].pk)))


def test_html_render_and_screens(client, verified):
    w = verified
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:mra", args=[w["iw"].pk])).content.decode()
    assert "Issue MRA R01" in page
    resp = c.post(reverse("iw:mra_issue", args=[w["iw"].pk]), follow=True)
    assert "MRA R01 issued" in resp.content.decode()
    html = c.get(reverse("iw:mra_view", args=[w["iw"].pk, 1])).content.decode()
    assert "DRIVER COPY" in html and "DEPOT COPY" in html and "Operated by: RAINBOW LOGISTICS (CFA)" in html
    assert "SHORT MEMO" in html and "DAMAGE MEMO" in html and "EXCESS MEMO" not in html
    assert "300" not in html.split("Unloading</h2>")[1].split("</p>")[0]   # no rate on the MRA
    if pdf.available():
        resp = c.get(reverse("iw:mra_pdf", args=[w["iw"].pk, 1]))
        assert resp["Content-Type"] == "application/pdf" and resp.content.startswith(b"%PDF")


def test_material_change_after_mra_needs_reopen(verified):
    w = verified
    mra.issue(w["clerk"], w["iw"].pk)
    with pytest.raises(IwError, match="reopen"):
        linking.add_colour_bank(w["clerk"], w["iw"].pk, direction="IN", challan_no="CB-5")
