"""Screen 01 - SIT import and monitoring (SoT 3)."""
import pytest
from django.urls import reverse

from inward.core.models import Document, ExceptionEvent
from inward.sit import services
from inward.sit.models import SitChangeEvent, SitShip
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


def test_older_report_is_refused(tmp_path, seeded):
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
