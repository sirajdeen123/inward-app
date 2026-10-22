"""Daily RMA CN INV Report from Gmail (decision 22-Oct-2026).

Oracle mails the "XXGST BPIL RMA CN INV Report" to 035cfa@gmail.com every morning around 08:30. From the fetch time
the mail worker looks - every 10 minutes - for today's mail whose subject contains the configured text and loads its
.xls / .xlsx attachment through the same import as the manual upload (depot check, repeated lines counted once,
each credit note replaced by its newest lines, the automatic IRR link afterwards). Once per day (ReportRun
RMA_CN_FETCH); a file already uploaded by hand is recognised. After the deadline the report counts as late (the
Material returns report says so) but the worker keeps looking for the rest of the day - on 22-Oct-2026 the mail came
at 12:08 PM. The Upload button stays as the fallback.
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

log = logging.getLogger("inward.rma")
KIND = "RMA_CN_FETCH"
RETRY = timedelta(minutes=10)
CACHE_KEY = "rma_cn_fetch_last"


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
    return {"subject": str(_flag("rma_cn_mail_subject", "XXGST BPIL RMA CN INV Report")).strip(),
            "fetch_time": _time("rma_cn_fetch_time", "08:30"), "deadline": _time("rma_cn_fetch_deadline", "11:00")}


@dataclass
class Outcome:
    status: str          # waiting / imported / failed / missing
    message: str = ""


def today_status(day=None) -> dict | None:
    """For the daily report and the Uploads row: what happened with today's CN mail."""
    from inward.iw.models import ReportRun
    day = day or timezone.localdate()
    run = ReportRun.objects.filter(kind=KIND, period=day.isoformat()).first()
    if run is not None:
        return {"ok": True, "text": run.skipped, "at": timezone.localtime(run.created_at)}
    deadline = settings_now()["deadline"]
    now = timezone.localtime()
    if day < now.date() or now.time() >= deadline:
        still = " - still checking every 10 minutes" if day == now.date() else ""
        return {"ok": False, "text": f"No CN report received by {deadline:%I:%M %p}{still}", "at": None}
    return None


def _finish(day, note: str):
    from inward.iw.models import ReportRun
    ReportRun.objects.get_or_create(kind=KIND, period=day.isoformat(), defaults={"skipped": note[:200]})


def import_from_mail(transport, user=None, day=None) -> Outcome:
    """Fetch the newest CN mail and load it (also the 'Fetch CN report from Gmail now' button)."""
    from inward.importers.common import ImportFailed
    from inward.rma import services
    s = settings_now()
    with tempfile.TemporaryDirectory() as tmp:
        mail = transport.fetch_latest(s["subject"], Path(tmp))
        if mail is None:
            return Outcome("waiting", f"No e-mail with '{s['subject']}' in the subject.")
        received = timezone.localtime(mail.received_at)
        if day and received.date() != day:
            return Outcome("waiting", f"Newest CN report mail is from {received:%d-%b-%Y %I:%M %p} - today's has not "
                                      "come yet.")
        if not mail.attachments:
            return Outcome("failed", f"The mail '{mail.subject}' has no .xls / .xlsx attachment.")
        notes = []
        for path in mail.attachments:
            original = path.name.split("_", 2)[-1]
            try:
                res = services.import_cn(user, path, original, source="GMAIL")
                notes.append(f"{original}: {res['lines']} line(s) of {res['credit_notes']} credit note(s)"
                             + (f", {res['irr_linked']} IRR receipt(s) linked" if res["irr_linked"] else ""))
            except services.RmaError as exc:
                if "already uploaded" not in str(exc):
                    raise
                notes.append(f"{original} (already loaded)")
            except ImportFailed as exc:
                return Outcome("failed", f"{original}: {exc}")
        return Outcome("imported", f"CN report of {received:%d-%b-%Y %I:%M %p} loaded - " + "; ".join(notes))


def run(transport, now: datetime | None = None) -> str:
    """Called by the mail worker every round."""
    from inward.iw.models import ReportRun
    now = timezone.localtime(now or timezone.now())
    s = settings_now()
    day = now.date()
    if now.time() < s["fetch_time"] or ReportRun.objects.filter(kind=KIND, period=day.isoformat()).exists():
        return "idle"
    last = cache.get(CACHE_KEY)
    if last and now - last < RETRY:
        return "wait"
    cache.set(CACHE_KEY, now, 3600)
    try:
        out = import_from_mail(transport, None, day=day)
    except Exception as exc:  # noqa: BLE001 - the worker must never stop; tried again in 10 minutes
        log.warning("RMA CN fetch: %s", exc)
        out = Outcome("failed", str(exc)[:180])
    if out.status == "imported":
        _finish(day, out.message)
        return "imported"
    if now.time() >= s["deadline"]:
        return "late"          # flagged as late in the report; still looked for until the day ends
    return out.status
