"""Screen 01 - SIT import and monitoring (SoT 3)."""
from decimal import Decimal
import pytest
from django.urls import reverse

from inward.core.models import Document, ExceptionEvent
from inward.sit import services
from inward.sit.models import SitChangeEvent, SitImport, SitShip
from tests.conftest import sample
from tests.helpers import sit_html, verified_client

pytestmark = pytest.mark.django_db

A = ("'390:Hindupur - Mfg", "52497520", "13-JUN-2026")
B = ("'036:Calicut", "52464774", "09-JUN-2026")
C = ("'279:RDC - WB-Sreerampore", "52252922", "20-MAY-2026")


def _rows(a_qty=49, include_c=True, b_qty=50, extra=False):
    rows = [(*A, "F002980000020000", "WEATHERCOAT GLOW WHITE-WHITE-20 L", "RETAIL", "L", "20", str(a_qty)),
            (*A, "F00XG90991003600", "EASY CLEAN . W1 BASE-.-3.6 L", "RETAIL", "L", "3.6", "96"),
            (*B, "F00AQR0000020000", "PLATINA WEATHERCOAT WC300 WHITE 20 L", "RETAIL", "L", "20", str(b_qty))]
    if include_c:
        rows.append((*C, "F00Z00021G001000", "BERGER SHADE TOOL-FANDECK 2500", "RETAIL", "L", "1", "50"))
    if extra:
        rows.append((*B, "F004690800020000", "FAST BAKE PRIMER BLACK 20 L", "AUTOMOTIVE", "L", "20", "5"))
    return rows


@pytest.fixture(autouse=True)
def _docs(settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    settings.INWARD = {**settings.INWARD, "MODE": "LIVE"}   # live rules; the UAT merge has its own test below


def test_first_import_creates_ships_and_refuses_duplicates(tmp_path, seeded):
    path = sit_html(tmp_path / "sit1.xls", _rows())
    imp = services.import_sit_file(path, source="MANUAL")
    assert (imp.ship_count, imp.line_count, imp.critical_count) == (3, 4, 0)
    assert set(SitShip.objects.values_list("ship_id", flat=True)) == {"52497520", "52464774", "52252922"}
    assert set(imp.events.values_list("event", flat=True)) == {"NEW_SHIP"}
    assert Document.objects.filter(doc_type="SIT_REPORT", sha256=imp.batch.sha256).exists()
    with pytest.raises(services.AlreadyImported):
        services.import_sit_file(path, source="MANUAL")


def test_changes_between_imports(tmp_path, seeded):
    services.import_sit_file(sit_html(tmp_path / "1.xls", _rows()), source="MANUAL")
    # Ship A partly processed (49 -> 20), Ship C gone and never linked, Ship B grows and gains a SKU (anomalies)
    second = sit_html(tmp_path / "2.xls", _rows(a_qty=20, include_c=False, b_qty=60, extra=True),
                      system_date="27-JUN-2026")
    imp = services.import_sit_file(second, source="MANUAL")
    got = set(imp.events.values_list("ship_id", "event"))
    assert got == {("52497520", "SKU_REDUCED"), ("52252922", "SHIP_REMOVED_NEVER_LINKED"),
                   ("52464774", "QTY_INCREASED"), ("52464774", "SKU_ADDED")}
    assert imp.critical_count == 3
    assert SitShip.objects.get(pk="52497520").status == SitShip.Status.PARTIAL
    assert SitShip.objects.get(pk="52252922").status == SitShip.Status.REMOVED_NEVER_LINKED
    assert ExceptionEvent.objects.filter(severity="CRITICAL").count() == 3


def test_older_report_is_refused_in_live(tmp_path, seeded, settings):
    settings.INWARD = {**settings.INWARD, "MODE": "LIVE"}
    services.import_sit_file(sit_html(tmp_path / "new.xls", _rows(), system_date="27-JUN-2026"), source="MANUAL")
    with pytest.raises(services.SitImportRejected, match="older than the current"):
        services.import_sit_file(sit_html(tmp_path / "old.xls", _rows(a_qty=48), system_date="20-JUN-2026"),
                                 source="MANUAL")


def test_pre_go_live_ships_raise_no_exceptions(tmp_path, seeded, make_user):
    clerk = make_user("clerk_sit", "DEPOT_CLERK")
    services.import_sit_file(sit_html(tmp_path / "1.xls", _rows()), source="MANUAL")
    assert services.mark_pre_go_live(clerk, ["52252922"]) == 1
    imp = services.import_sit_file(sit_html(tmp_path / "2.xls", _rows(include_c=False), system_date="27-JUN-2026"),
                                   source="MANUAL")
    assert not SitChangeEvent.objects.filter(sit_import=imp, ship_id="52252922").exists()


@pytest.mark.samples
def test_real_sample_sit(seeded):
    imp = services.import_sit_file(sample("SIT.xlsx"), source="MANUAL")
    assert imp.line_count > 0 and imp.critical_count == 0
    assert SitShip.objects.filter(pk="52497520").exists()


def test_screens(client, tmp_path, seeded, make_user):
    clerk = verified_client(client, make_user("clerk_scr", "DEPOT_CLERK"))
    assert clerk.get(reverse("sit:home")).status_code == 200
    path = sit_html(tmp_path / "up.xls", _rows())
    with open(path, "rb") as fh:
        resp = clerk.post(reverse("sit:upload"), {"file": fh}, follow=True)
    assert "Imported up.xls: 3 Ships" in resp.content.decode()
    with open(path, "rb") as fh:
        resp = clerk.post(reverse("sit:upload"), {"file": fh}, follow=True)
    assert "Already imported" in resp.content.decode()
    assert clerk.get(reverse("sit:ships") + "?q=52497520").status_code == 200
    detail = clerk.get(reverse("sit:ship", args=["52497520"])).content.decode()
    assert "298 000" in detail and "RETAIL" in detail
    resp = clerk.post(reverse("sit:mark_pre_go_live"), {"ship_id": ["52252922"]}, follow=True)
    assert "1 Ship(s) marked Pre-go-live" in resp.content.decode()


def test_viewer_cannot_import(client, tmp_path, seeded, make_user):
    viewer = verified_client(client, make_user("viewer1", "VIEWER"))
    assert viewer.get(reverse("sit:home")).status_code == 200
    with open(sit_html(tmp_path / "v.xls", _rows()), "rb") as fh:
        assert viewer.post(reverse("sit:upload"), {"file": fh}).status_code == 403


def test_ship_totals_and_serial_numbers(client, tmp_path, seeded, make_user):
    rows = _rows() + [(*A, "F00B470633002001", "QD A/C PUTTY-GREY-2 K", "AUTOMOTIVE", "K", "2", "70")]
    services.import_sit_file(sit_html(tmp_path / "t.xls", rows), source="MANUAL")
    clerk = verified_client(client, make_user("clerk_tot", "DEPOT_CLERK"))
    resp = clerk.get(reverse("sit:ship", args=["52497520"]))
    totals = resp.context["totals"]
    assert totals["total"]["skus"] == 3 and totals["total"]["pcs"] == 215
    assert totals["total"]["ltr"] == Decimal("1325.6") and totals["total"]["kg"] == 140
    by_wh = dict(totals["warehouses"])
    assert by_wh["RETAIL"]["ltr"] == Decimal("1325.6") and by_wh["INDUSTRIAL"]["kg"] == 140
    html = resp.content.decode()
    assert "Total: 3 SKU(s)" in html and '<td class="num">3</td>' in html  # serial numbers 1..3
    listing = clerk.get(reverse("sit:ships")).content.decode()
    assert "1325.6" in listing and "140" in listing


def test_uat_merge_any_order_skips_duplicates(tmp_path, seeded, settings):
    settings.INWARD = {**settings.INWARD, "MODE": "TEST"}
    first = services.import_sit_file(sit_html(tmp_path / "new.xls", _rows(include_c=False), system_date="27-JUN-2026"),
                                     source="MANUAL")
    assert first.back_dated and first.ship_count == 2 and first.known_ships == 0
    a_before = SitShip.objects.get(ship_id=A[1])
    older = _rows(a_qty=60, include_c=True) + [("'390:Hindupur - Mfg", "52000001", "10-JUN-2026", "F00Z00021G001000",
                                                 "BERGER SHADE TOOL-FANDECK 2500", "RETAIL", "L", "1", "7")]
    imp = services.import_sit_file(sit_html(tmp_path / "old.xls", older, system_date="20-JUN-2026"), source="MANUAL")
    assert imp.ship_count == 2 and imp.known_ships == 2 and imp.event_count == 0          # older date accepted
    assert set(SitShip.objects.filter(first_seen=imp).values_list("ship_id", flat=True)) == {C[1], "52000001"}
    a_after = SitShip.objects.get(ship_id=A[1])
    assert (a_after.last_seen_id, a_after.status) == (a_before.last_seen_id, a_before.status)   # duplicate skipped
    newer = services.import_sit_file(sit_html(tmp_path / "next.xls", _rows(include_c=False, extra=True),
                                              system_date="30-JUN-2026"), source="MANUAL")
    assert newer.ship_count == 0 and newer.known_ships == 2                                  # nothing new
    assert SitShip.objects.filter(status=SitShip.Status.IN_SIT).count() == 4                 # nothing removed
    assert not ExceptionEvent.objects.exists() and "UAT merge" in services.describe(imp)


def test_uat_merge_refused_outside_test(tmp_path, seeded, settings):
    from inward.importers.sit import parse_sit
    path = sit_html(tmp_path / "x.xls", _rows())
    with pytest.raises(services.SitImportRejected, match="only in the UAT"):
        services._import_uat_merge(path, parse_sit(path), "abc", "x.xls", "MANUAL", None, "", None)
