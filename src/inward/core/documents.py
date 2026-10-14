"""Document store (SoT 9, 14): content-addressed, write-once, revisioned.

The SHA-256 is the storage key, so stored evidence is never overwritten and filenames are never
identities. Files live under DOCUMENT_ROOT on the VPS; the OneDrive mirror copies them later.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from django.conf import settings
from django.db import transaction

from inward.core import audit
from inward.core.models import Document, DocumentLink


class IntegrityError(IOError):
    pass


def _path(key: str) -> Path:
    return Path(settings.DOCUMENT_ROOT) / key[:2] / key


def _put(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = _path(digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return digest


def read(document: Document) -> bytes:
    data = _path(document.storage_key).read_bytes()
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise IntegrityError(f"Integrity check failed for document {document.id}")
    return data


@transaction.atomic
def store(data: bytes, *, filename: str, content_type: str, doc_type: str, user=None,
          links: list[tuple[str, str, str]] = (), supersedes: Document | None = None) -> Document:
    digest = _put(data)
    doc = Document.objects.create(
        doc_type=doc_type, original_filename=filename, content_type=content_type, size_bytes=len(data),
        sha256=digest, storage_key=digest, created_by=user if getattr(user, "is_authenticated", False) else None,
        revision=(supersedes.revision + 1) if supersedes else 1, supersedes=supersedes,
    )
    for entity_type, entity_id, role in links:
        DocumentLink.objects.create(document=doc, entity_type=entity_type, entity_id=str(entity_id), role=role)
    audit.record("DOCUMENT_STORED", "document", doc.id, user=user,
                 after={"doc_type": doc_type, "filename": filename, "sha256": digest, "revision": doc.revision})
    return doc
