"""Numbering, audit trail, stale edits, seed data, document store (SoT 4, 9, 12, 14.1)."""
import threading
from datetime import datetime

import pytest
from django.db import DatabaseError, connection, connections, transaction
from django.urls import reverse

from inward.core import audit, documents
from inward.core.models import (
    AuditEvent, BusinessLineWarehouse, DepotOperator, NotificationRecipient, RateMaster,
)
from inward.core.numbering import next_iw_number
from inward.core.versioning import StaleEdit, update_versioned
from inward.domain.fy import IST


@pytest.mark.django_db(transaction=True)
def test_concurrent_iw_numbers_are_unique_and_sequential():
    arrival = datetime(2027, 3, 31, 23, 10, tzinfo=IST)  # FY 2627
    results, errors = [], []

    def worker():
        try:
            with transaction.atomic():
                results.append(next_iw_number(arrival))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == [f"IW-2627-{n:06d}" for n in range(1, 13)]


@pytest.mark.django_db(transaction=True)
def test_rollback_returns_the_number_and_fy_restarts():
    april = datetime(2027, 4, 2, 9, 0, tzinfo=IST)  # FY 2728
    with pytest.raises(RuntimeError), transaction.atomic():
        assert next_iw_number(april) == "IW-2728-000001"
        raise RuntimeError("IW creation failed")
    with transaction.atomic():
        assert next_iw_number(april) == "IW-2728-000001"
    with pytest.raises(RuntimeError):
        next_iw_number(april)  # refuses to run outside a transaction


@pytest.mark.django_db
def test_audit_trail_is_append_only():
    ev = audit.record("TEST", "x", 1, after={"a": 1})
    for sql in ("UPDATE audit_event SET action = 'HACK' WHERE id = %s", "DELETE FROM audit_event WHERE id = %s"):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [ev.id])
    assert AuditEvent.objects.get(pk=ev.id).action == "TEST"


@pytest.mark.django_db
def test_stale_edit_is_rejected(make_user, seeded):
    from inward.accounts.models import User
    user = make_user("stale", "DEPOT_CLERK")
    a, b = User.objects.get(pk=user.pk), User.objects.get(pk=user.pk)
    update_versioned(a, full_name="first")
    with pytest.raises(StaleEdit):
        update_versioned(b, full_name="second")
    assert User.objects.get(pk=user.pk).full_name == "first"


@pytest.mark.django_db
def test_seed_reference_data(seeded):
    op = DepotOperator.objects.get(depot_id="035")
    assert op.cfa_name == "RAINBOW LOGISTICS"
    assert RateMaster.objects.get(rate_type="UNLOADING_PER_TON").amount == 300
    assert dict(BusinessLineWarehouse.objects.values_list("business_line", "warehouse")) == {
        "RETAIL": "RETAIL", "PROTECTON": "INDUSTRIAL", "POWDER": "INDUSTRIAL", "GI": "INDUSTRIAL",
        "AUTOMOTIVE": "INDUSTRIAL"}
    sed_approval = set(NotificationRecipient.objects.filter(purpose="SED_APPROVAL").values_list("role_label", "kind"))
    assert sed_approval == {("Depot Manager", "TO"), ("Admin Officer", "CC")}


@pytest.mark.django_db
def test_document_store_is_content_addressed(settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path
    d1 = documents.store(b"scan", filename="a.pdf", content_type="application/pdf", doc_type="SIGNED_MRA",
                         links=[("iw", "1", "signed_mra")])
    d2 = documents.store(b"scan-v2", filename="a.pdf", content_type="application/pdf", doc_type="SIGNED_MRA",
                         supersedes=d1)
    assert d2.revision == 2 and d1.sha256 != d2.sha256
    assert documents.read(d1) == b"scan"
    (tmp_path / d1.storage_key[:2] / d1.storage_key).write_bytes(b"tampered")
    with pytest.raises(documents.IntegrityError):
        documents.read(d1)


def test_healthz_needs_no_login(client):
    resp = client.get(reverse("web:healthz"))
    assert resp.status_code == 200 and resp.content == b"ok"
