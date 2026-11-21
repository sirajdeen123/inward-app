"""Material returns masters as files (Phase 1, 21-Nov-2026): TSI Master, Customer Master, portal approvers.
Download - edit in Excel - upload - preview - confirm (inward.core.masterfiles). A Sales Officer / approver whose
e-mail changes needs a Change reason; Resigned / Transferred put the person on the 'left - never copied' list."""
from __future__ import annotations

from inward.accounts.models import User
from inward.core import audit
from inward.core.masterfiles import Spec, empty_plan, register
from inward.iw.comms import EMAIL_RE
from inward.rma import importers as imp
from inward.rma import services as svc
from inward.rma.models import Approver, CustomerSite, SalesOfficerChange, SalesOfficerLeft, Territory

PERSON_REASONS = {"resign": "RESIGNED", "transfer": "TRANSFERRED", "correct": "CORRECTION", "new": "NEW"}
REASON_HELP = "Change reason: Resigned / Transferred (the old e-mail is never copied again) / Correction / New."


def _reason(text: str) -> str:
    t = text.lower()
    return next((v for k, v in PERSON_REASONS.items() if k in t), "")


def _status(text: str, allowed: tuple[str, ...]) -> str | None:
    s = (text or allowed[0]).strip().lower()
    return s if s in allowed else None


def _bad_mail(mail: str) -> bool:
    return bool(mail) and not EMAIL_RE.match(mail)


def _when(dt) -> str:
    from django.utils import timezone
    return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M") if dt else ""

# ---------------------------------------------------------------- TSI Master


TSI_COLS = ["Territory", "Sales Officer", "BL", "E-mail", "Status", "Change reason"]


def _tsi_now(t: Territory, left: set) -> tuple[str, str, bool]:
    if t.vacant:
        return "", "", True
    mail = (t.email_override or t.email).lower()
    return t.officer, mail, mail in left


def tsi_export() -> list[dict]:
    left = set(SalesOfficerLeft.objects.values_list("email", flat=True))
    last = {}
    for c in SalesOfficerChange.objects.select_related("by").order_by("territory_id", "-at"):
        last.setdefault(c.territory_id, c)
    out = []
    for t in Territory.objects.all():
        name, mail, gone = _tsi_now(t, left)
        c = last.get(t.territory_no)
        out.append({"Territory": t.territory_no, "Sales Officer": name, "BL": t.business_line, "E-mail": mail,
                    "Status": "Left" if gone else "Active", "TSI file - Sales Officer": t.sales_officer,
                    "TSI file - E-mail": t.email, "Last changed": _when(c.at) if c else "",
                    "Changed by": str(c.by) if c else "", "Last change": c.reason if c else ""})
    return out


def tsi_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {t.territory_no: t for t in Territory.objects.all()}
    left = set(SalesOfficerLeft.objects.values_list("email", flat=True))
    seen = set()
    for r in rows:
        no, name, bl = r.get("Territory", "").removesuffix(".0"), r.get("Sales Officer", ""), r.get("BL", "")
        mail, reason = r.get("E-mail", "").lower(), _reason(r.get("Change reason", ""))
        status = _status(r.get("Status", ""), ("active", "left"))
        err = lambda m: p["errors"].append({"row": r["_row"], "key": no or "-", "message": m})  # noqa: E731
        if not no:
            err("Territory is empty.")
            continue
        if no in seen:
            err(f"Territory {no} appears twice - only the first row is used.")
            continue
        seen.add(no)
        if status is None:
            err("Status must be Active or Left.")
            continue
        if _bad_mail(mail):
            err(f"'{mail}' is not a valid e-mail.")
            continue
        t = cur.get(no)
        if t is None:
            if status == "left":
                err(f"Territory {no} is new - it cannot be Left.")
                continue
            p["new"].append({"key": no, "label": f"Territory {no}", "changes": [["Sales Officer", "", name],
                             ["E-mail", "", mail], ["BL", "", bl]]})
            p["ops"].append({"op": "NEW", "no": no, "name": name, "mail": mail, "bl": bl})
            continue
        c_name, c_mail, gone = _tsi_now(t, left)
        if status == "left":
            if t.vacant:
                p["unchanged"] += 1
            elif reason not in ("RESIGNED", "TRANSFERRED"):
                err(f"Territory {no}: Status Left needs the Change reason Resigned or Transferred.")
            else:
                p["left"].append({"key": no, "label": f"Territory {no}", "reason": reason.title(),
                                  "changes": [["Sales Officer", f"{c_name} {c_mail}".strip(), "none - nobody copied"]]})
                p["ops"].append({"op": "LEFT", "no": no, "reason": reason})
            continue
        if t.vacant and not (name or mail):
            err(f"Territory {no}: Active needs the new Sales Officer's name and e-mail (or keep Status Left).")
            continue
        changes = []
        if name and name != c_name:
            changes.append(["Sales Officer", c_name, name])
        if mail and mail != c_mail:
            if mail in left:
                err(f"Territory {no}: {mail} is on the 'left' list - undo it on the Approvers & Sales Officers page first.")
                continue
            if c_mail and reason not in ("RESIGNED", "TRANSFERRED", "CORRECTION"):
                err(f"Territory {no}: the e-mail changes - give the Change reason (Resigned, Transferred or Correction).")
                continue
            changes.append(["E-mail", c_mail, mail])
        if bl and bl != t.business_line:
            changes.append(["BL", t.business_line, bl])
        if not changes:
            p["unchanged"] += 1
            continue
        person = any(c[0] != "BL" for c in changes)
        p["changed"].append({"key": no, "label": f"Territory {no}", "changes": changes,
                             "reason": (reason or "Correction").title() if person else ""})
        mail_changed = any(c[0] == "E-mail" for c in changes)
        p["ops"].append({"op": "CHANGE", "no": no, "name": name or c_name, "mail": mail or c_mail, "bl": bl,
                         "person": person, "reason": reason if mail_changed and reason in ("RESIGNED", "TRANSFERRED")
                         else "CORRECTION"})
    p["missing"] = [f"Territory {no} {cur[no].officer}" for no in sorted(set(cur) - seen)]
    return p


def tsi_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0, "left": 0}
    for op in ops:
        if op["op"] == "NEW":
            t = Territory.objects.create(territory_no=op["no"], sales_officer=op["name"], business_line=op["bl"],
                                         email=op["mail"])
            SalesOfficerChange.objects.create(territory=t, reason="New (master file)", before={},
                                              after={"sales_officer": op["name"], "email": op["mail"], "vacant": False},
                                              by=user)
            audit.record("RMA_SALES_OFFICER_CHANGED", "rma_territory", t.pk, user=user,
                         after={"sales_officer": op["name"], "email": op["mail"], "reason": "New (master file)"})
            n["new"] += 1
        elif op["op"] == "LEFT":
            svc._update_sales_officer(user, op["no"], op["reason"])
            n["left"] += 1
        else:
            if op["bl"]:
                Territory.objects.filter(pk=op["no"]).update(business_line=op["bl"])
            if op["person"]:
                svc._update_sales_officer(user, op["no"], op["reason"], op["name"], op["mail"])
            n["changed"] += 1
    return n

# ---------------------------------------------------------------- Customer Master


CUST_COLS = ["Site code", "Account number", "Customer name", "Territory", "BL type", "Customer type", "City", "Status"]
CUST_FIELDS = {"Account number": "account_no", "Customer name": "name", "Territory": "territory", "BL type": "bl_type",
               "Customer type": "customer_type", "City": "city"}


def cust_export() -> list[dict]:
    so = svc.SalesOfficers()
    out = []
    for s in CustomerSite.objects.order_by("territory_no", "name"):
        who = so.territory(s.territory_no)
        out.append({"Site code": s.site_code, **{c: getattr(s, f) for c, f in CUST_FIELDS.items()}, "Status": "Active",
                    "Territory No": s.territory_no, "Sales Officer now": who[0] if who else ""})
    return out


def cust_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {s.site_code: s for s in CustomerSite.objects.all()}
    seen = set()
    for r in rows:
        code = r.get("Site code", "").removesuffix(".0")
        status = _status(r.get("Status", ""), ("active", "remove"))
        err = lambda m: p["errors"].append({"row": r["_row"], "key": code or "-", "message": m})  # noqa: E731
        if not code:
            err("Site code is empty.")
            continue
        if code in seen:
            err(f"Site {code} appears twice - only the first row is used.")
            continue
        seen.add(code)
        if status is None:
            err("Status must be Active or Remove.")
            continue
        vals = {f: r.get(c, "").removesuffix(".0") if f == "account_no" else r.get(c, "") for c, f in CUST_FIELDS.items()}
        s = cur.get(code)
        if status == "remove":
            if s:
                p["removed"].append({"key": code, "label": f"Site {code} {s.name}", "changes": []})
                p["ops"].append({"op": "REMOVE", "code": code})
            continue
        if not vals["name"] or not vals["territory"]:
            err(f"Site {code}: Customer name and Territory are needed.")
            continue
        if s is None:
            p["new"].append({"key": code, "label": f"Site {code} {vals['name']}",
                             "changes": [[c, "", vals[f]] for c, f in CUST_FIELDS.items() if vals[f]]})
            p["ops"].append({"op": "NEW", "code": code, **vals})
            continue
        changes = [[c, getattr(s, f), vals[f]] for c, f in CUST_FIELDS.items() if vals[f] != getattr(s, f)]
        if changes:
            p["changed"].append({"key": code, "label": f"Site {code} {s.name}", "changes": changes})
            p["ops"].append({"op": "CHANGE", "code": code, **vals})
        else:
            p["unchanged"] += 1
    p["missing"] = [f"Site {c} {cur[c].name}" for c in sorted(set(cur) - seen)]
    return p


def cust_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0, "removed": 0}
    for op in ops:
        if op["op"] == "REMOVE":
            CustomerSite.objects.filter(pk=op["code"]).delete()
            n["removed"] += 1
        else:
            vals = {f: op[f] for f in CUST_FIELDS.values()}
            vals["territory_no"] = imp.territory_no(vals["territory"])[:10]
            CustomerSite.objects.update_or_create(site_code=op["code"], defaults=vals)
            n["new" if op["op"] == "NEW" else "changed"] += 1
    audit.record("RMA_CUSTOMER_MASTER_FILE", "rma_customer_site", "-", user=user,
                 after={**n, "sites": [{"op": o["op"], "site": o["code"]} for o in ops][:300]})
    return n

# ---------------------------------------------------------------- portal approvers


APPR_COLS = ["Name (as in the portal)", "E-mail", "Status", "Mails go to (replacement)", "Change reason"]


def appr_export() -> list[dict]:
    return [{"Name (as in the portal)": a.name, "E-mail": a.email, "Status": "Active" if a.active else "Left",
             "Mails go to (replacement)": a.redirect_to.name if a.redirect_to else "", "Change reason": "",
             "Last changed": _when(a.updated_at) if a.updated_by_id else "", "Changed by": str(a.updated_by or "")}
            for a in Approver.objects.select_related("redirect_to", "updated_by")]


def appr_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {a.key: a for a in Approver.objects.select_related("redirect_to")}
    in_file = {svc.name_key(r.get("Name (as in the portal)", "")) for r in rows}
    seen = set()
    for r in rows:
        name = r.get("Name (as in the portal)", "")
        key, mail = svc.name_key(name), r.get("E-mail", "").lower()
        rep, reason = svc.name_key(r.get("Mails go to (replacement)", "")), _reason(r.get("Change reason", ""))
        status = _status(r.get("Status", ""), ("active", "left"))
        err = lambda m: p["errors"].append({"row": r["_row"], "key": name or "-", "message": m})  # noqa: E731
        if not key:
            err("Name is empty.")
            continue
        if key in seen:
            err(f"{name} appears twice - only the first row is used.")
            continue
        seen.add(key)
        if status is None:
            err("Status must be Active or Left.")
            continue
        if _bad_mail(mail):
            err(f"'{mail}' is not a valid e-mail.")
            continue
        if rep and (rep == key or (rep not in cur and rep not in in_file)):
            err(f"{name}: 'Mails go to' must be another approver on this list.")
            continue
        if status == "left" and not rep:
            err(f"{name}: Status Left needs 'Mails go to (replacement)'.")
            continue
        a = cur.get(key)
        if a is None:
            p["new"].append({"key": name, "label": name, "changes": [["E-mail", "", mail], ["Status", "", status.title()],
                             ["Mails go to", "", r.get("Mails go to (replacement)", "")]]})
            p["ops"].append({"op": "NEW", "name": name, "mail": mail, "active": status == "active", "rep": rep,
                             "reason": reason})
            continue
        changes = []
        if mail != a.email.lower():
            if a.email and reason not in ("RESIGNED", "TRANSFERRED", "CORRECTION"):
                err(f"{name}: the e-mail changes - give the Change reason (Resigned, Transferred or Correction).")
                continue
            changes.append(["E-mail", a.email, mail])
        if (status == "active") != a.active:
            changes.append(["Status", "Active" if a.active else "Left", status.title()])
        if rep != (a.redirect_to.key if a.redirect_to else ""):
            changes.append(["Mails go to", a.redirect_to.name if a.redirect_to else "", r.get("Mails go to (replacement)", "")])
        if not changes:
            p["unchanged"] += 1
            continue
        item = {"key": name, "label": name, "changes": changes, "reason": reason.title() if reason else ""}
        p["left" if status == "left" and a.active else "changed"].append(item)
        p["ops"].append({"op": "CHANGE", "name": name, "mail": mail, "active": status == "active", "rep": rep,
                         "reason": reason})
    p["missing"] = [cur[k].name for k in sorted(set(cur) - seen)]
    return p


def appr_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0}
    for op in ops:                                   # names first, so a replacement on the same file exists
        if op["op"] == "NEW":
            svc.approver_for(op["name"])
    keys = {a.key: a.pk for a in Approver.objects.all()}
    for op in ops:
        a = Approver.objects.get(key=svc.name_key(op["name"]))
        svc._save_approver(user, a.pk, op["mail"], op["active"], str(keys.get(op["rep"], "")) if op["rep"] else "",
                           reason=(op["reason"] or "").title())
        n["new" if op["op"] == "NEW" else "changed"] += 1
    return n


def setup() -> None:
    register(Spec("RMA_TSI", "TSI Master (Sales Officers)", "Material returns", TSI_COLS,
                  ["Territory", "Sales Officer", "E-mail"],
                  ["TSI file - Sales Officer", "TSI file - E-mail", "Last changed", "Changed by", "Last change"],
                  "RMA_WORK", tsi_export, tsi_plan, tsi_apply, count=Territory.objects.count, help=[
                      "One row per territory: the Sales Officer copied on the approval mails of its dealers.",
                      "Sales Officer resigned: type the new name and e-mail, Change reason 'Resigned'. "
                      "Nobody yet: Status 'Left' with Change reason 'Resigned' - then nobody is copied.",
                      REASON_HELP, "A new territory: add a row (Status Active)."]))
    register(Spec("RMA_CUSTOMER", "Customer Master (dealer sites)", "Material returns", CUST_COLS,
                  ["Site code", "Account number", "Customer name", "Territory"], ["Territory No", "Sales Officer now"],
                  "RMA_WORK", cust_export, cust_plan, cust_apply, count=CustomerSite.objects.count, help=[
                      "One row per Bill-To site: the territory decides which Sales Officer is copied.",
                      "Move a dealer to another territory: change Territory (e.g. S.S1.035.19).",
                      "Delete a site: Status 'Remove'. A new site: add a row."]))
    register(Spec("RMA_APPROVERS", "Portal approvers", "Material returns", APPR_COLS,
                  ["Name (as in the portal)", "E-mail", "Status"], ["Last changed", "Changed by"],
                  "RMA_WORK", appr_export, appr_plan, appr_apply, count=Approver.objects.count, help=[
                      "One row per name as the RMA portal prints it under 'Pending At'.",
                      "Approver left: Status 'Left' and 'Mails go to (replacement)' = the person who now approves.",
                      REASON_HELP]))
