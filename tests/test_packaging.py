"""SKU packaging master: company list import, clerk edits, drastic-change review (decisions 15-Oct-2026)."""
import shutil
from decimal import Decimal

import openpyxl
import pytest
from django.urls import reverse

from inward.core import packaging
from inward.core.models import FeatureFlag, PackagingChange, SkuPackaging
from tests.conftest import sample
from tests.helpers import verified_client

pytestmark = pytest.mark.django_db
HEAD = ["SKU Code", "SKU Name", "UOM", "Volume", "Pack Type", "Units Per Box"]


@pytest.fixture
def env(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    return {"clerk": make_user("clerk_pk", "DEPOT_CLERK"), "cfa": make_user("cfa_pk", "CFA_MANAGER"), "tmp": tmp_path}


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEAD)
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def _load(env, rows, name="list.xlsx", user="clerk"):
    up = packaging.create_upload(env[user], _xlsx(env["tmp"] / name, rows), name)
    return packaging.apply_upload(env[user], up.pk)


@pytest.mark.samples
def test_real_company_list(env):
    path = env["tmp"] / "SKU Packaging.xlsx"
    shutil.copy(sample("SKU Packaging.xlsx"), path)
    up = packaging.create_upload(env["clerk"], path, path.name)
    assert (up.counts["new"], up.counts["changed"], up.counts["errors"], up.counts["verify"]) == (2783, 0, 0, 10)
    packaging.apply_upload(env["clerk"], up.pk)
    assert SkuPackaging.objects.count() == 2783
    grout = SkuPackaging.objects.get(sku="F0013T0000005001")
    assert grout.package_type_id == "DRUM" and grout.units_per_package == 4 and "Verify physically" in grout.verify_note
    assert SkuPackaging.objects.get(sku="F007720C88000230").units_per_package is None   # blank in the list
    assert SkuPackaging.objects.get(sku="F000000134001000").package_type_id == "CARTON"
    assert SkuPackaging.objects.get(sku="F003810991CLRNTS").package_type_id == "SHOTS"
    again = packaging.create_upload(env["clerk"], path, path.name)
    assert (again.counts["new"], again.counts["changed"], again.counts["unchanged"]) == (0, 0, 2783)


def test_drastic_rules_and_review(env):
    _load(env, [("F000000134001000", "LUXOL SKY BLUE 1 L", "L", 1, "CARTON BOX", 6),
                ("F000000160001000", "LUXOL PHIROZA 1 L", "L", 1, "CARTON BOX", 6),
                ("F000000166020000", "LUXOL OXFORD 20 L", "L", 20, "DRUM", 1)])
    _load(env, [("F000000134001000", "LUXOL SKY BLUE 1 L", "L", 1, "CARTON BOX", 8),    # 6 -> 8: logged only
                ("F000000160001000", "LUXOL PHIROZA 1 L", "L", 1, "CARTON BOX", 12),   # 6 -> 12: drastic
                ("F000000166020000", "LUXOL OXFORD 20 L", "L", 20, "CARTON BOX", 1)],  # group change: drastic
          name="v2.xlsx")
    pending = {c.sku: c for c in PackagingChange.objects.filter(review="PENDING")}
    assert set(pending) == {"F000000160001000", "F000000166020000"}
    assert SkuPackaging.objects.get(sku="F000000160001000").units_per_package == 12   # applied immediately
    with pytest.raises(Exception):
        packaging.review(env["clerk"], pending["F000000160001000"].pk, True)          # clerk cannot review
    packaging.review(env["cfa"], pending["F000000160001000"].pk, False)                # revert
    assert SkuPackaging.objects.get(sku="F000000160001000").units_per_package == 6
    packaging.review(env["cfa"], pending["F000000166020000"].pk, True)
    assert SkuPackaging.objects.get(sku="F000000166020000").package_type_id == "CARTON"


def test_bulk_upload_is_flagged(env):
    FeatureFlag.objects.filter(key="packaging_bulk_change_limit").update(value={"value": 1})
    rows = [("F000000134001000", "A", "L", 1, "CARTON BOX", 6), ("F000000160001000", "B", "L", 1, "CARTON BOX", 6)]
    _load(env, rows)
    up = _load(env, [(s, d, u, v, p, 8) for s, d, u, v, p, _ in rows], name="v2.xlsx")
    assert up.bulk_flagged and PackagingChange.objects.filter(review="PENDING").count() == 2


def test_clerk_add_edit_and_verify(env):
    clerk = env["clerk"]
    _load(env, [("F0013T0000005001", "HS EPOXY TILE GROUT 3P-WHITE-5 K", "K", 5, "DRUM", 4)])
    ch = packaging.save_sku(clerk, "F00N090TGH030001", group="BAG", units="1",
                            description="Solitaire - Granite-Brazilian Black Gr.-30 K", uom="K", pack_size="30")
    assert ch.action == "ADDED" and SkuPackaging.objects.get(sku="F00N090TGH030001").source == "CLERK"
    with pytest.raises(packaging.PackagingError, match="reason is mandatory"):
        packaging.save_sku(clerk, "F0013T0000005001", group="DRUM", units="1")
    v = packaging.save_sku(clerk, "F0013T0000005001", group="DRUM", units="4", verified=True)
    assert v.action == "VERIFIED" and SkuPackaging.objects.get(sku="F0013T0000005001").verify_note == ""
    e = packaging.save_sku(clerk, "F0013T0000005001", group="DRUM", units="1", reason="Physical count differs")
    assert e.drastic and e.review == "PENDING"   # 4 -> 1 per drum


def test_pieces_to_packages(env):
    _load(env, [("F000000134001000", "LUXOL 1 L", "L", 1, "CARTON BOX", 6),
                ("F003810991CLRNTS", "NT COLORANT SHOTS", "L", 0.000154, "SHOTS", 0.0001)])
    carton = SkuPackaging.objects.get(sku="F000000134001000")
    p = packaging.to_packages(Decimal("33"), carton)
    assert (p.group, p.full, p.loose) == ("CARTON", 5, 3)
    assert packaging.to_packages(Decimal("30"), carton).loose == 0
    assert packaging.to_packages(Decimal("10"), SkuPackaging.objects.get(sku="F003810991CLRNTS")).pieces_only
    assert packaging.to_packages(Decimal("7"), None).pieces_only


def test_screens(client, env):
    c = verified_client(client, env["clerk"])
    with open(_xlsx(env["tmp"] / "s.xlsx", [("F000000134001000", "LUXOL 1 L", "L", 1, "CARTON BOX", 6)]), "rb") as fh:
        resp = c.post(reverse("core:packaging_upload"), {"file": fh}, follow=True)
    assert "Confirm and apply" in resp.content.decode()
    up_pk = resp.context["up"].pk
    c.post(reverse("core:packaging_preview", args=[up_pk]), {"action": "apply"})
    assert SkuPackaging.objects.filter(sku="F000000134001000").exists()
    resp = c.post(reverse("core:packaging_sku", args=["F000000134001000"]),
                  {"group": "CARTON", "units": "12", "reason": "Physical count differs from the list"}, follow=True)
    assert "flagged to the CFA Manager" in resp.content.decode()
    assert c.get(reverse("core:packaging_review")).status_code == 403   # clerk is not a reviewer
    m = verified_client(client, env["cfa"])
    assert "units per package 6 -&gt; 12" in m.get(reverse("core:packaging_review")).content.decode()
