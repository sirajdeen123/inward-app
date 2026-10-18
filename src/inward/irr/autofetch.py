"""Daily IRR from Gmail (decision 18-Oct-2026).

The IRR report reaches the mailbox every morning (around 08:30). From the fetch time the worker looks - every 10
minutes - for today's mail whose subject contains the configured text, imports its .xls / .xlsx attachment (the IRR
import merges overlaps and runs the reconciliation engine), and mails the IRR Reconciliation Summary for yesterday
to the CFA Manager, Admin Officer and System Supervisor. If no IRR mail has come by the deadline, the summary is
still sent, saying the IRR did not arrive. Once per day; a manual import of the same file is recognised.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from django.core.cache import cache
from django.utils import timezone

from inward.core.models import FeatureFlag

log = logging.getLogger("inward.irr")
KIND = "IRR_FETCH"
RETRY = timedelta(minutes=10)


def _flag(key: str, default):
    f = FeatureFlag.objects.filter(key=key).first()
    v = (f.value or {}).get("value") if f else None
    return default if v in (None, "") else v


def _time(key: str, default: str) -> time:
    text = str(_flag(key, default))
    try:
        h, m = (int(x) for x in text.split(":"))
        return time(h, m)
    except ValueError:
        h, m = (int(x) for x in default.split(":"))
        return time(h, m)


def settings_now() -> dict:
    return {"subject": str(_flag("irr_mail_subject", "CBPPROD2 : BPIL Issue - Receipt Register")).strip(),
            "fetch_time": _time("irr_fetch_time", "08:30"), "deadline": _time("irr_fetch_deadline", "11:00")}


@dataclass
class Outcome:
    status: str          # not_yet / done / waiting / imported / missing / failed
    message: str = ""


def _done_today(day) -> bool:
    from inward.iw.models import ReportRun
    return ReportRun.objects.filter(kind=KIND, period=day.isoformat()).exists()


def _finish(day, note: str, fetched: dict | None):
    from inward.iw import reports
    from inward.iw.models import ReportRun
    run, created = ReportRun.objects.get_or_create(kind=KIND, period=day.isoformat(), defaults={"skipped": note[:200]})
    if created:
        reports.queue_report("IRR_EXC", day.isoformat(), {"r": reports.irr_summary(day, fetched=fetched)})
    return run


def import_from_mail(transport, user=None, day=None) -> Outcome:
    """Fetch the newest IRR mail and import it (also the 'Fetch IRR from Gmail now' button)."""
    from inward.irr import services
    from inward.iw.reports import _system_user
    s = settings_now()
    user = user or _system_user()
    with tempfile.TemporaryDirectory() as tmp:
        mail = transport.fetch_latest(s["subject"], Path(tmp))
        if mail is None:
            return Outcome("waiting", f"No e-mail with '{s['subject']}' in the subject.")
        received = timezone.localtime(mail.received_at)
        if day and received.date() != day:
            return Outcome("waiting", f"Newest IRR mail is from {received:%d-%b-%Y %I:%M %p} - today's has not come yet.")
        if not mail.attachments:
            return Outcome("failed", f"The IRR mail '{mail.subject}' has no .xls / .xlsx attachment.")
        added, files = 0, []
        for path in mail.attachments:
            original = path.name.split("_", 2)[-1]
            try:
                b = services.import_irr(user, path, original, source="GMAIL")
                added += b.accepted_count
                files.append(original)
            except services.AlreadyImported:
                files.append(f"{original} (already imported)")
        return Outcome("imported", f"{', '.join(files)} from the mail of {received:%d-%b-%Y %I:%M %p}: {added} new row(s).")


def run(transport, now: datetime | None = None) -> str:
    """Called by the mail worker every round."""
    now = timezone.localtime(now or timezone.now())
    s = settings_now()
    day = now.date()
    if now.time() < s["fetch_time"] or _done_today(day):
        return "idle"
    last = cache.get("irr_fetch_last")
    if last and now - last < RETRY:
        return "wait"
    cache.set("irr_fetch_last", now, 3600)
    try:
        out = import_from_mail(transport, day=day)
    except Exception as exc:  # noqa: BLE001 - the worker must never stop; tried again in 10 minutes
        log.warning("IRR fetch: %s", exc)
        out = Outcome("failed", str(exc)[:180])
    if out.status == "imported":
        _finish(day, out.message, {"ok": True, "text": out.message})
        return "imported"
    if now.time() >= s["deadline"]:
        note = f"No IRR received by {s['deadline']:%I:%M %p}: {out.message}"
        _finish(day, note, {"ok": False, "text": note})
        return "missing"
    return out.status
