"""Inward masters as files (Phase 2, 21-Nov-2026): source organisations with their MRA e-mails, vendors, Non-Stock
senders, transporters and the Notification e-mails. Same download - edit - upload - preview - confirm as Phase 1
(inward.core.masterfiles). An e-mail removed or replaced needs a Change reason; a removed organisation e-mail is
retired (kept in the history, no longer mailed). E-mails added by someone who may verify e-mails are Verified, else
they wait on the Verify e-mails page like the ones typed at the MRA."""
from __future__ import annotations

import re

from inward.accounts.models import User
from inward.accounts.services import modes_for
from inward.core import audit
from inward.core.masterfiles import Spec, empty_plan, register
from inward.core.models import EmailStatus, NotificationRecipient, Organisation, OrganisationEmail, Sender, \
    Transporter, Vendor
from inward.domain.normalize import search_key
from inward.iw.comms import EMAIL_RE

REASON_HELP = "Change reason: needed when an e-mail is removed or replaced (e.g. 'person left', 'wrong address')."
_SPLIT = re.compile(r"[\s,;]+")


def _status(text: str, allowed: tuple[str, ...]) -> str | None:
    s = (text or allowed[0]).strip().lower()
    return s if s in allowed else None


def _mails(text: str) -> tuple[list[str], list[str]]:
    """'a@x.com, b@y.com' -> (good, bad), lower case, in order, no repeats."""
    good, bad = [], []
    for a in _SPLIT.split((text or "").strip().lower()):
        if a and a not in good:
            (good if EMAIL_RE.match(a) else bad).append(a)
    return good, bad


def _new_status(user: User) -> str:
    return EmailStatus.VERIFIED if "GRANT" in modes_for(user, "EMAIL_VERIFY") else EmailStatus.UNVERIFIED


def _yes(text: str) -> bool | None:
    t = (text or "").strip().lower()
    return True if t in ("yes", "y", "true", "1") else False if t in ("no", "n", "false", "0", "") else None


def _err(p: dict, r: dict, key: str):
    return lambda m: p["errors"].append({"row": r["_row"], "key": key or "-", "message": m})

# ---------------------------------------------------------------- source organisations + e-mails


ORG_COLS = ["Code", "Name", "Type", "State", "MRA To e-mails", "MRA Cc e-mails", "Status", "Change reason"]
ORG_TYPES = {t.lower(): t for t in Organisation.OrgType.values}


def _org_mails(org_pk: str, rows) -> tuple[list[str], list[str]]:
    to = [e.email.lower() for e in rows if e.organisation_id == org_pk and e.kind == "TO"]
    cc = [e.email.lower() for e in rows if e.organisation_id == org_pk and e.kind == "CC"]
    return to, cc


def org_export() -> list[dict]:
    live = list(OrganisationEmail.objects.exclude(status=EmailStatus.RETIRED).order_by("id"))
    out = []
    for o in Organisation.objects.order_by("code"):
        to, cc = _org_mails(o.code, live)
        unver = [e.email for e in live if e.organisation_id == o.code and e.status == EmailStatus.UNVERIFIED]
        out.append({"Code": o.code, "Name": o.name, "Type": o.org_type, "State": o.state, "MRA To e-mails": ", ".join(to),
                    "MRA Cc e-mails": ", ".join(cc), "Status": "Active" if o.active else "Inactive",
                    "E-mails not yet verified": ", ".join(unver),
                    "Organisation verified": "Yes" if o.verified else "No - added on the IW screen"})
    return out


def _code(raw: str, cur: dict) -> str:
    """'1' typed in Excel for '001' -> '001' when that organisation exists (Excel drops leading zeros)."""
    code = raw.removesuffix(".0").strip().upper()
    if code.isdigit() and code not in cur:
        for n in sorted({len(c) for c in cur if c.isdigit()}):
            if code.zfill(n) in cur:
                return code.zfill(n)
    return code


def org_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {o.code: o for o in Organisation.objects.all()}
    live = list(OrganisationEmail.objects.exclude(status=EmailStatus.RETIRED))
    seen = set()
    for r in rows:
        code = _code(r.get("Code", ""), cur)
        err = _err(p, r, code)
        name, state, reason = r.get("Name", ""), r.get("State", ""), r.get("Change reason", "")
        kind = ORG_TYPES.get(r.get("Type", "").lower(), "") if r.get("Type") else ""
        status = _status(r.get("Status", ""), ("active", "inactive"))
        to, bad_to = _mails(r.get("MRA To e-mails", ""))
        cc, bad_cc = _mails(r.get("MRA Cc e-mails", ""))
        if not code:
            err("Code is empty.")
            continue
        if len(code) > 10:
            err(f"Code {code} is longer than 10 characters.")
            continue
        if code in seen:
            err(f"Organisation {code} appears twice - only the first row is used.")
            continue
        seen.add(code)
        if status is None:
            err("Status must be Active or Inactive.")
            continue
        if r.get("Type") and not kind:
            err(f"Type must be one of {', '.join(Organisation.OrgType.values)}.")
            continue
        if bad_to or bad_cc:
            err(f"Not a valid e-mail: {', '.join(bad_to + bad_cc)}.")
            continue
        cc = [a for a in cc if a not in to]
        o = cur.get(code)
        if o is None:
            if not name:
                err(f"Organisation {code} is new - give its Name.")
                continue
            p["new"].append({"key": code, "label": f"{code} {name}", "changes": [
                c for c in (["Name", "", name], ["Type", "", kind or "OTHER"], ["State", "", state],
                            ["MRA To", "", ", ".join(to)], ["MRA Cc", "", ", ".join(cc)]) if c[2]]})
            p["ops"].append({"op": "NEW", "code": code, "name": name, "type": kind or "OTHER", "state": state,
                             "active": status == "active", "to": to, "cc": cc, "reason": reason})
            continue
        c_to, c_cc = _org_mails(code, live)
        changes = []
        for label, new, old in (("Name", name, o.name), ("Type", kind, o.org_type), ("State", state, o.state)):
            if new and new != old:
                changes.append([label, old, new])
        if (status == "active") != o.active:
            changes.append(["Status", "Active" if o.active else "Inactive", status.title()])
        dropped = [a for a in c_to if a not in to] + [a for a in c_cc if a not in cc]
        if to != c_to:
            changes.append(["MRA To", ", ".join(c_to), ", ".join(to)])
        if cc != c_cc:
            changes.append(["MRA Cc", ", ".join(c_cc), ", ".join(cc)])
        if dropped and not reason:
            err(f"Organisation {code}: {', '.join(dropped)} would be removed - give the Change reason.")
            continue
        if not to and c_to and status == "active":
            err(f"Organisation {code}: the MRA would have no To e-mail - keep at least one, or mark it Inactive.")
            continue
        if not changes:
            p["unchanged"] += 1
            continue
        p["changed"].append({"key": code, "label": f"{code} {o.name}", "changes": changes, "reason": reason})
        p["ops"].append({"op": "CHANGE", "code": code, "name": name or o.name, "type": kind or o.org_type,
                         "state": state if state else o.state, "active": status == "active", "to": to, "cc": cc,
                         "reason": reason})
    p["missing"] = [f"{c} {cur[c].name}" for c in sorted(set(cur) - seen)]
    return p


def org_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0, "e-mails added": 0, "e-mails removed": 0}
    status = _new_status(user)
    for op in ops:
        o = Organisation.objects.filter(pk=op["code"]).first()
        before = None
        if o is None:
            o = Organisation.objects.create(code=op["code"], name=op["name"], org_type=op["type"], state=op["state"],
                                            active=op["active"], verified=True, created_by=user)
            n["new"] += 1
        else:
            before = {"name": o.name, "type": o.org_type, "state": o.state, "active": o.active}
            o.name, o.org_type, o.state, o.active = op["name"], op["type"], op["state"], op["active"]
            o.save(update_fields=["name", "org_type", "state", "active"])
            n["changed"] += 1
        rows = {(e.email.lower(), e.kind): e for e in OrganisationEmail.objects.filter(organisation=o)}
        want = {(a, "TO") for a in op["to"]} | {(a, "CC") for a in op["cc"]}
        old = {k for k, e in rows.items() if e.status != EmailStatus.RETIRED}
        for k in sorted(old - want):
            OrganisationEmail.objects.filter(pk=rows[k].pk).update(status=EmailStatus.RETIRED)
            n["e-mails removed"] += 1
        for k in sorted(want - old):
            if k in rows:
                OrganisationEmail.objects.filter(pk=rows[k].pk).update(status=status, entered_by=user)
            else:
                OrganisationEmail.objects.create(organisation=o, email=k[0], kind=k[1], status=status, entered_by=user)
            n["e-mails added"] += 1
        audit.record("ORGANISATION_MASTER_FILE", "organisation", o.code, user=user, before=before and {
            **before, "to": sorted(a for a, k in old if k == "TO"), "cc": sorted(a for a, k in old if k == "CC")},
            after={"name": o.name, "type": o.org_type, "state": o.state, "active": o.active, "to": op["to"],
                   "cc": op["cc"], "reason": op["reason"]})
    return n

# ---------------------------------------------------------------- vendors


VEND_COLS = ["Code", "Name", "Oracle vendor ID", "Site", "MRA e-mail", "Status", "Change reason"]


def vend_export() -> list[dict]:
    return [{"Code": v.code, "Name": v.name, "Oracle vendor ID": v.oracle_vendor_id, "Site": v.site,
             "MRA e-mail": v.email, "Status": "Active" if v.active else "Inactive",
             "E-mail status": v.get_email_status_display() if v.email else ""} for v in Vendor.objects.order_by("code")]


def vend_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {v.code.upper(): v for v in Vendor.objects.all()}
    seen = set()
    fields = (("Name", "name"), ("Oracle vendor ID", "oracle_vendor_id"), ("Site", "site"))
    for r in rows:
        code = r.get("Code", "").strip().upper()
        err = _err(p, r, code)
        mails, bad = _mails(r.get("MRA e-mail", ""))
        status, reason = _status(r.get("Status", ""), ("active", "inactive")), r.get("Change reason", "")
        vals = {f: r.get(c, "").removesuffix(".0") if f == "oracle_vendor_id" else r.get(c, "") for c, f in fields}
        if not code:
            err("Code is empty.")
            continue
        if code in seen:
            err(f"Vendor {code} appears twice - only the first row is used.")
            continue
        seen.add(code)
        if status is None:
            err("Status must be Active or Inactive.")
            continue
        if bad or len(mails) > 1:
            err("MRA e-mail: give one valid e-mail." if not bad else f"'{bad[0]}' is not a valid e-mail.")
            continue
        mail = mails[0] if mails else ""
        v = cur.get(code)
        if v is None:
            if not vals["name"]:
                err(f"Vendor {code} is new - give its Name.")
                continue
            p["new"].append({"key": code, "label": f"{code} {vals['name']}", "changes": [
                [c, "", vals[f]] for c, f in fields if vals[f]] + ([["MRA e-mail", "", mail]] if mail else [])})
            p["ops"].append({"op": "NEW", "code": code, **vals, "mail": mail, "active": status == "active",
                             "reason": reason})
            continue
        changes = [[c, getattr(v, f), vals[f]] for c, f in fields if vals[f] and vals[f] != getattr(v, f)]
        if mail != v.email.lower():
            if v.email and not reason:
                err(f"Vendor {code}: the MRA e-mail {v.email} would be {'replaced' if mail else 'removed'} - "
                    "give the Change reason.")
                continue
            changes.append(["MRA e-mail", v.email, mail])
        if (status == "active") != v.active:
            changes.append(["Status", "Active" if v.active else "Inactive", status.title()])
        if not changes:
            p["unchanged"] += 1
            continue
        p["changed"].append({"key": code, "label": f"{code} {v.name}", "changes": changes, "reason": reason})
        p["ops"].append({"op": "CHANGE", "code": v.code, **{f: vals[f] or getattr(v, f) for _, f in fields},
                         "mail": mail, "active": status == "active", "reason": reason})
    p["missing"] = [f"{c} {cur[c].name}" for c in sorted(set(cur) - seen)]
    return p


def vend_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0}
    status = _new_status(user)
    for op in ops:
        v = Vendor.objects.filter(pk=op["code"]).first()
        before = v and {"name": v.name, "oracle_vendor_id": v.oracle_vendor_id, "site": v.site, "email": v.email,
                        "active": v.active}
        if v is None:
            v = Vendor(code=op["code"])
        mail_new = op["mail"] != (v.email or "").lower()
        v.name, v.oracle_vendor_id, v.site, v.active = op["name"], op["oracle_vendor_id"], op["site"], op["active"]
        if mail_new:
            v.email, v.email_status = op["mail"], status
        v.save()
        n["new" if before is None else "changed"] += 1
        audit.record("VENDOR_MASTER_FILE", "vendor", v.code, user=user, before=before,
                     after={"name": v.name, "oracle_vendor_id": v.oracle_vendor_id, "site": v.site, "email": v.email,
                            "active": v.active, "reason": op["reason"]})
    return n

# ---------------------------------------------------------------- Non-Stock senders


SEND_COLS = ["Name", "E-mail", "Status", "Change reason"]


def send_export() -> list[dict]:
    return [{"Name": s.name, "E-mail": s.email, "Status": "Active" if s.active else "Inactive",
             "E-mail status": s.get_status_display()} for s in Sender.objects.order_by("name")]


def send_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {search_key(s.name): s for s in Sender.objects.all()}
    seen = set()
    for r in rows:
        name = r.get("Name", "")
        key = search_key(name)
        err = _err(p, r, name)
        mails, bad = _mails(r.get("E-mail", ""))
        status, reason = _status(r.get("Status", ""), ("active", "inactive")), r.get("Change reason", "")
        if not key:
            err("Name is empty.")
            continue
        if key in seen:
            err(f"{name} appears twice - only the first row is used.")
            continue
        seen.add(key)
        if status is None:
            err("Status must be Active or Inactive.")
            continue
        if bad or len(mails) != 1:
            err("E-mail: give one valid e-mail." if not bad else f"'{bad[0]}' is not a valid e-mail.")
            continue
        mail = mails[0]
        s = cur.get(key)
        if s is None:
            p["new"].append({"key": name, "label": name, "changes": [["E-mail", "", mail]]})
            p["ops"].append({"op": "NEW", "name": name.upper(), "mail": mail, "active": status == "active",
                             "reason": reason})
            continue
        changes = []
        if mail != s.email.lower():
            if not reason:
                err(f"{s.name}: the e-mail {s.email} would be replaced - give the Change reason.")
                continue
            changes.append(["E-mail", s.email, mail])
        if (status == "active") != s.active:
            changes.append(["Status", "Active" if s.active else "Inactive", status.title()])
        if not changes:
            p["unchanged"] += 1
            continue
        p["changed"].append({"key": s.name, "label": s.name, "changes": changes, "reason": reason})
        p["ops"].append({"op": "CHANGE", "pk": s.pk, "mail": mail, "active": status == "active", "reason": reason})
    p["missing"] = [cur[k].name for k in sorted(set(cur) - seen)]
    return p


def send_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0}
    status = _new_status(user)
    for op in ops:
        if op["op"] == "NEW":
            s = Sender.objects.create(name=op["name"], email=op["mail"], status=status, active=op["active"])
            before = None
            n["new"] += 1
        else:
            s = Sender.objects.get(pk=op["pk"])
            before = {"email": s.email, "active": s.active}
            if op["mail"] != s.email.lower():
                s.email, s.status = op["mail"], status
            s.active = op["active"]
            s.save(update_fields=["email", "status", "active"])
            n["changed"] += 1
        audit.record("SENDER_MASTER_FILE", "sender", s.pk, user=user, before=before,
                     after={"name": s.name, "email": s.email, "active": s.active, "reason": op["reason"]})
    return n

# ---------------------------------------------------------------- transporters


TRANS_COLS = ["ID", "Name", "Verified", "Status"]


def trans_export() -> list[dict]:
    from django.db.models import Count

    from inward.iw.models import Iw
    used = dict(Iw.objects.exclude(transporter=None).values_list("transporter").annotate(n=Count("id")))
    return [{"ID": t.pk, "Name": t.name, "Verified": "Yes" if t.verified else "No",
             "Status": "Active" if t.active else "Inactive", "IW vehicles": used.get(t.pk, 0)}
            for t in Transporter.objects.order_by("name")]


def trans_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    cur = {str(t.pk): t for t in Transporter.objects.all()}
    keys = {t.search_key: str(t.pk) for t in cur.values()}
    seen, taken = set(), {}
    for r in rows:
        tid, name = r.get("ID", "").removesuffix(".0"), r.get("Name", "").upper()
        key = search_key(name)
        err = _err(p, r, tid or name)
        verified, status = _yes(r.get("Verified", "")), _status(r.get("Status", ""), ("active", "inactive"))
        if not key:
            err("Name is empty.")
            continue
        if verified is None:
            err("Verified must be Yes or No.")
            continue
        if status is None:
            err("Status must be Active or Inactive.")
            continue
        if tid and tid not in cur:
            err(f"ID {tid} is not a transporter of this app - leave ID empty for a new one.")
            continue
        if tid in seen:
            err(f"ID {tid} appears twice - only the first row is used.")
            continue
        other = taken.get(key) or (keys.get(key) if keys.get(key) != tid else None)
        if other:
            err(f"{name}: the same name as transporter {other} ({cur[other].name if other in cur else 'this file'}) - "
                "two names for one transporter cannot be merged here.")
            continue
        taken[key] = tid or name
        if not tid:
            p["new"].append({"key": name, "label": name, "changes": [["Verified", "", "Yes" if verified else "No"]]})
            p["ops"].append({"op": "NEW", "name": name, "verified": verified, "active": status == "active"})
            continue
        seen.add(tid)
        t = cur[tid]
        changes = []
        if name != t.name:
            changes.append(["Name", t.name, name])
        if verified != t.verified:
            changes.append(["Verified", "Yes" if t.verified else "No", "Yes" if verified else "No"])
        if (status == "active") != t.active:
            changes.append(["Status", "Active" if t.active else "Inactive", status.title()])
        if not changes:
            p["unchanged"] += 1
            continue
        p["changed"].append({"key": tid, "label": f"{tid} {t.name}", "changes": changes})
        p["ops"].append({"op": "CHANGE", "pk": int(tid), "name": name, "verified": verified,
                         "active": status == "active"})
    p["missing"] = [f"{k} {cur[k].name}" for k in sorted(set(cur) - seen, key=int)]
    return p


def trans_apply(user: User, ops: list[dict]) -> dict:
    n = {"new": 0, "changed": 0}
    for op in ops:
        if op["op"] == "NEW":
            t = Transporter.objects.create(name=op["name"], search_key=search_key(op["name"]), verified=op["verified"],
                                           active=op["active"])
            before = None
            n["new"] += 1
        else:
            t = Transporter.objects.get(pk=op["pk"])
            before = {"name": t.name, "verified": t.verified, "active": t.active}
            t.name, t.search_key, t.verified, t.active = op["name"], search_key(op["name"]), op["verified"], op["active"]
            t.save(update_fields=["name", "search_key", "verified", "active"])
            n["changed"] += 1
        audit.record("TRANSPORTER_MASTER_FILE", "transporter", t.pk, user=user, before=before,
                     after={"name": t.name, "verified": t.verified, "active": t.active})
    return n

# ---------------------------------------------------------------- Notification e-mails


NOTE_COLS = ["Role", "E-mail"]
MRA_CC_ROW = "MRA default Cc (every MRA mail)"


def _roles() -> dict[str, list[NotificationRecipient]]:
    out: dict[str, list[NotificationRecipient]] = {}
    for r in NotificationRecipient.objects.filter(active=True).exclude(purpose="MRA_CC").order_by("role_label", "purpose"):
        out.setdefault(r.role_label, []).append(r)
    return out


def note_export() -> list[dict]:
    from inward.core.views_notify import PURPOSES
    out = []
    for role, rows in _roles().items():
        out.append({"Role": role, "E-mail": next((r.email for r in rows if r.email), ""),
                    "Receives": " | ".join(f"{PURPOSES.get(r.purpose, r.purpose).split(' - ')[0]} ({r.get_kind_display()}"
                                           f"{', mandatory' if r.mandatory else ''})" for r in rows)})
    cc = NotificationRecipient.objects.filter(purpose="MRA_CC").order_by("id").values_list("email", flat=True)
    out.append({"Role": MRA_CC_ROW, "E-mail": ", ".join(cc), "Receives": "Cc of every MRA mail - several e-mails allowed"})
    return out


def note_plan(rows: list[dict]) -> dict:
    p = empty_plan()
    roles = _roles()
    cur = {k.lower(): (k, next((r.email.lower() for r in v if r.email), "")) for k, v in roles.items()}
    cc_now = [e.lower() for e in NotificationRecipient.objects.filter(purpose="MRA_CC").order_by("id")
              .values_list("email", flat=True)]
    seen = set()
    for r in rows:
        role = r.get("Role", "")
        err = _err(p, r, role)
        mails, bad = _mails(r.get("E-mail", ""))
        if role.lower() in seen:
            err(f"{role} appears twice - only the first row is used.")
            continue
        seen.add(role.lower())
        if bad:
            err(f"Not a valid e-mail: {', '.join(bad)}.")
            continue
        if role.lower() == MRA_CC_ROW.lower():
            if mails == cc_now:
                p["unchanged"] += 1
            else:
                p["changed"].append({"key": role, "label": MRA_CC_ROW, "changes": [["E-mail", ", ".join(cc_now),
                                                                                   ", ".join(mails)]]})
                p["ops"].append({"op": "MRA_CC", "mails": mails})
            continue
        if role.lower() not in cur:
            err(f"'{role}' is not a role of the Notification e-mails - the roles come from the app; change the e-mail "
                "of an existing role.")
            continue
        if len(mails) > 1:
            err(f"{role}: one e-mail per role (the MRA default Cc row takes several).")
            continue
        name, now = cur[role.lower()]
        mail = mails[0] if mails else ""
        if mail == now:
            p["unchanged"] += 1
            continue
        mandatory = any(x.mandatory for x in roles[name])
        p["changed"].append({"key": name, "label": name, "changes": [["E-mail", now, mail]],
                             "reason": "mandatory mails will have nobody" if not mail and mandatory else ""})
        p["ops"].append({"op": "ROLE", "role": name, "mail": mail})
    p["missing"] = [cur[k][0] for k in sorted(set(cur) - seen)] + ([MRA_CC_ROW] if MRA_CC_ROW.lower() not in seen else [])
    return p


def note_apply(user: User, ops: list[dict]) -> dict:
    from inward.core.views_notify import save_mra_cc
    n = {"changed": 0}
    for op in ops:
        if op["op"] == "MRA_CC":
            save_mra_cc(user, ", ".join(op["mails"]))
        else:
            rows = NotificationRecipient.objects.filter(role_label=op["role"], active=True)
            before = sorted({r.email for r in rows})
            rows.update(email=op["mail"])
            audit.record("NOTIFICATION_EMAIL_CHANGED", "notification_recipient", op["role"], user=user,
                         before={"emails": before}, after={"email": op["mail"], "via": "master file"})
        n["changed"] += 1
    return n


def setup() -> None:
    register(Spec("ORGS", "Source organisations & MRA e-mails", "Stock inward", ORG_COLS, ["Code", "Name"],
                  ["E-mails not yet verified", "Organisation verified"], "IW_EDIT", org_export, org_plan, org_apply,
                  count=Organisation.objects.count, help=[
                      "One row per depot / RDC / factory: the MRA of a vehicle from it goes to the To e-mails, "
                      "Cc to the Cc e-mails (several e-mails: separate them with commas).",
                      "A new organisation: add a row with its SIT code (e.g. 001) and name.",
                      "Organisation no longer used: Status 'Inactive'. " + REASON_HELP]))
    register(Spec("VENDORS", "Vendors & MRA e-mails", "Stock inward", VEND_COLS, ["Code", "Name"], ["E-mail status"],
                  "IW_EDIT", vend_export, vend_plan, vend_apply, count=Vendor.objects.count, help=[
                      "One row per vendor: the MRA of a vendor load goes to its MRA e-mail.",
                      "A new vendor: add a row with its code (e.g. V54). " + REASON_HELP]))
    register(Spec("SENDERS", "Non-Stock senders", "Stock inward", SEND_COLS, ["Name", "E-mail"], ["E-mail status"],
                  "IW_EDIT", send_export, send_plan, send_apply, count=Sender.objects.count, help=[
                      "One row per sender of Non-Stock (Others) material: the MRA goes to this e-mail.",
                      "A new sender: add a row. " + REASON_HELP]))
    register(Spec("TRANSPORTERS", "Transporters", "Stock inward", TRANS_COLS, ["ID", "Name"], ["IW vehicles"],
                  "IW_EDIT", trans_export, trans_plan, trans_apply, count=Transporter.objects.count, help=[
                      "One row per transporter (names typed on the IW screen are added automatically, Verified No).",
                      "Correct a spelling: change the Name on its row (keep the ID). Checked: Verified 'Yes'.",
                      "A new transporter: add a row with the ID empty. Not used any more: Status 'Inactive'."]))
    register(Spec("NOTIFICATIONS", "Notification e-mails", "Stock inward", NOTE_COLS, ["Role", "E-mail"], ["Receives"],
                  "EMAIL_VERIFY", note_export, note_plan, note_apply,
                  count=lambda: len(_roles()) + 1, help=[
                      "One row per role (Depot Manager, Admin Officer, CFA Manager, Clerk...): its e-mail is used on "
                      "every list that role is on (see 'Receives').",
                      f"'{MRA_CC_ROW}': several e-mails separated by commas.",
                      "New roles cannot be added here - they come with the app."]))

