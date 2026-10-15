"""OneDrive mirror (SoT 9) with rclone replaced by a fake."""
import json
import subprocess

import pytest

from inward.core import documents, mirror
from inward.core.models import Document, ExceptionEvent
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


class FakeRclone:
    def __init__(self):
        self.remote = {}

    def __call__(self, *args, timeout=300):
        if args[0] == "copyto":
            self.remote[args[2].split("Inward Bundles/", 1)[1]] = "h-" + open(args[1], "rb").read()[:4].hex()
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "lsjson":
            return subprocess.CompletedProcess(args, 0, json.dumps(
                [{"Path": p, "Hashes": {"quickxor": h}} for p, h in self.remote.items()]), "")
        if args[0] == "hashsum":
            return subprocess.CompletedProcess(args, 0, f"h-{open(args[2], 'rb').read()[:4].hex()}  x\n", "")
        raise AssertionError(args)


@pytest.fixture
def fake(settings, monkeypatch):
    settings.INWARD = {**settings.INWARD, "ONEDRIVE_REMOTE": "onedrive:Inward Bundles"}
    f = FakeRclone()
    monkeypatch.setattr(mirror, "_rclone", f)
    return f


def test_off_without_remote(verified):  # noqa: F811
    assert mirror.mirror_pending() == {"mirror": "off"} and not mirror.status()["enabled"]


def test_readable_paths_and_integrity(verified, fake):  # noqa: F811
    out = mirror.mirror_pending(older_than_s=-5)
    assert out["mirrored"] >= 1 and out["failed"] == 0
    iw_no = verified["iw"].iw_no
    photo = Document.objects.get(doc_type="DAMAGE_PHOTO")
    assert photo.cloud_path.startswith(f"FY2627/{iw_no}/2 Damage photos/") and photo.cloud_path.endswith("d.jpg")
    assert not Document.objects.filter(doc_type="DAMAGE_PHOTO_PREVIEW", cloud_mirrored_at__isnull=False).exists()
    assert mirror.integrity_check()["changed"] == 0
    fake.remote[photo.cloud_path] = "h-tampered"
    del fake.remote[next(p for p in fake.remote if p != photo.cloud_path)]
    r = mirror.integrity_check()
    assert r["changed"] == 1 and r["missing"] == 1
    assert ExceptionEvent.objects.filter(code="ONEDRIVE_CHANGED", severity="CRITICAL").exists()
    assert mirror.status()["pending"] == 1          # the missing one is uploaded again
    assert documents.read(photo)                     # the local copy stays authoritative
