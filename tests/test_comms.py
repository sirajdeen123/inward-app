"""Screen 04 part D - MRA / SED mails, Sent Mail evidence, gates, release, Depot Manager approval (SoT 7.6 - 7.8)."""
import email
import email.policy
from datetime import timedelta
from email.message import EmailMessage

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.core import documents
from inward.core.models import EmailStatus, NotificationRecipient, OrganisationEmail
from inward.iw import comms, mra
from inward.iw.models import Iw, MailMessage, MailRelease, SedApproval
from inward.iw.services import IwError
from inward.mail.gmail import GmailTransport
from inward.mail.transport import SentEvidence
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
M = MailMessage.Status
DM = "dm.coimbatore@example.com"


class FakeGmail:
    """Stands in for Gmail: records SMTP sends, confirms them in 'Sent Mail' when told to, serves inbox replies."""

    def __init__(self):
        self.builder = GmailTransport("inward@example.com", "app-password")
        self.sent: dict[str, EmailMessage] = {}
        self.confirm = True
        self.fail_with: Exception | None = None
        self.inbox: list[bytes] = []

    def send(self, message):
        if self.fail_with:
            raise self.fail_with
        self.sent[message.stamp] = self.builder.build(message)

    def confirm_sent(self, message):
        built = self.sent.get(message.stamp)
        if not (self.confirm and built):
            return None
        return SentEvidence(message.stamp, built["Message-ID"], timezone.now(), built["To"], built.get("Cc", ""),
                            built["Subject"], built.as_bytes())

    def fetch_raw(self, subject_contains, since_days=30):
        return [r for r in self.inbox if subject_contains.encode() in r]


@pytest.fixture
def issued(verified, settings):  # noqa: F811
    settings.INWARD = {**settings.INWARD, "MODE": "LIVE", "GMAIL_ADDRESS": "inward@example.com"}
    w = verified
    rev, _ = mra.issue(w["clerk"], w["iw"].pk, reconfirm=True)
    if rev.pdf is None:  # no Pango on a development PC: stand-in PDF
        rev.pdf = documents.store(b"%PDF-1.7 test", filename=f"MRA {w['iw'].iw_no} R01.pdf",
                                  content_type="application/pdf", doc_type="MRA_PDF")
        rev.save(update_fields=["pdf"])
    for purpose, label, addr in [("SED_IMMEDIATE", "Depot Manager", DM), ("SED_IMMEDIATE", "Admin Officer", "ao@example.com"),
                                 ("SED_IMMEDIATE", "CFA Manager", "cfa@example.com"), ("SED_APPROVAL", "Depot Manager", DM),
                                 ("SED_APPROVAL", "Admin Officer", "ao@example.com")]:
        NotificationRecipient.objects.filter(purpose=purpose, role_label=label).update(email=addr)
    w["rev"], w["gmail"] = rev, FakeGmail()
    return w


def _reply(subject, body, sender=DM, auth="spf=pass smtp.mailfrom=example.com; dkim=pass", mid="<r1@example.com>"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = sender, "inward@example.com", subject, mid
    msg["Date"] = email.utils.format_datetime(timezone.now())
    if auth:
        msg["Authentication-Results"] = f"mx.google.com; {auth}"
    msg.set_content(body + "\n\n> original request quoted here")
    return msg.as_bytes()


def test_missing_source_email_entered_on_the_spot(issued):
    w = issued
    assert any("No e-mail for 390" in b for b in comms.send_blockers(w["iw"]))
    with pytest.raises(IwError, match="not a valid"):
        comms.capture_email(w["clerk"], w["iw"].pk, "ORG", "390", "not-an-email")
    comms.capture_email(w["clerk"], w["iw"].pk, "ORG", "390", "Hindupur.Dispatch@Example.com")
    t = comms.mra_targets(w["iw"])[0]
    assert t.to == ["hindupur.dispatch@example.com"] and t.unverified == t.to
    assert not comms.send_blockers(w["iw"])
    assert [r["email"] for r in comms.unverified_emails()] == ["hindupur.dispatch@example.com"]


def test_mandatory_internal_recipients_needed(issued):
    w = issued
    OrganisationEmail.objects.create(organisation_id="390", email="h@example.com", kind="TO", status=EmailStatus.VERIFIED)
    NotificationRecipient.objects.filter(purpose="SED_APPROVAL", role_label="Depot Manager").update(email="")
    assert any("Depot Manager in the Notification Master" in b for b in comms.send_blockers(w["iw"]))


@pytest.fixture
def sent(issued):
    w = issued
    OrganisationEmail.objects.create(organisation_id="390", email="h@example.com", kind="TO", status=EmailStatus.VERIFIED)
    OrganisationEmail.objects.create(organisation_id="390", email="h.cc@example.com", kind="CC", status=EmailStatus.VERIFIED)
    w["mails"] = comms.send_mra(w["clerk"], w["iw"].pk, "clerk.copy@example.com")
    return w


def test_send_queues_mra_and_sed_mails(sent):
    w = sent
    purposes = [m.purpose for m in w["mails"]]
    assert purposes == ["MRA", "SED_APPROVAL"]      # one SED mail (22-Oct-2026), no separate notification
    m = w["mails"][0]
    assert m.to == ["h@example.com"] and m.cc == ["h.cc@example.com", "clerk.copy@example.com"]
    assert m.stamp == f"{w['iw'].iw_no} MRA-R01 #C-{m.pk:06d}" and "BERGER PAINTS INDIA LTD" in m.html_body
    appr = w["mails"][1]
    assert appr.stamp.startswith(f"SED-APPR {w['iw'].iw_no} R01") and appr.to == [DM]
    assert appr.cc == ["ao@example.com", "cfa@example.com"] and "claimable Rs" in appr.subject
    assert appr.attachments.count() == 2      # MRA PDF + damage photo preview
    assert SedApproval.objects.get(iw=w["iw"]).status == "REQUESTED"
    with pytest.raises(IwError, match="already been sent"):
        comms.send_mra(w["clerk"], w["iw"].pk)
    assert Iw.objects.get(pk=w["iw"].pk).status == Iw.Status.PHYSICALLY_VERIFIED   # waits for SENT


def test_worker_confirms_and_completes_screen04(sent):
    w, g = sent, sent["gmail"]
    g.confirm = False
    stats = comms.process_outbox(g)
    assert stats["queued"] == 2 and set(MailMessage.objects.values_list("status", flat=True)) == {M.QUEUED}
    built = g.sent[w["mails"][0].stamp]
    assert built["Subject"].endswith(f"[{w['mails'][0].stamp}]") and built["X-Inward-Stamp"] == w["mails"][0].stamp
    parts = [p.get_content_type() for p in built.walk()]
    assert "application/pdf" in parts and "image/png" in parts       # MRA attached, logo inline
    g.confirm = True
    comms.process_outbox(g)
    m = MailMessage.objects.get(pk=w["mails"][0].pk)
    assert m.status == M.SENT and m.eml is not None and m.sent_at
    iw = Iw.objects.get(pk=w["iw"].pk)
    assert iw.status == Iw.Status.MRA_ISSUED and all(x.ok for x in comms.gates(iw))


def test_permanent_failure_retry_and_unconfirmed(sent):
    import smtplib
    w, g = sent, sent["gmail"]
    g.fail_with = smtplib.SMTPRecipientsRefused({"h@example.com": (550, b"no such user")})
    comms.process_outbox(g)
    assert set(MailMessage.objects.values_list("status", flat=True)) == {M.FAILED}
    comms.retry(w["clerk"], w["mails"][0].pk)
    g.fail_with, g.confirm = None, False
    comms.process_outbox(g)
    m = MailMessage.objects.get(pk=w["mails"][0].pk)
    assert m.status == M.QUEUED
    later = timezone.now() + timedelta(minutes=31)
    comms.process_outbox(g, now=later)          # not in Sent Mail in time: checked again, then sent again
    m.refresh_from_db()
    assert m.status == M.QUEUED and m.attempts == 2
    g.fail_with = OSError("network down")       # outage: the mail just waits in the queue
    MailMessage.objects.filter(pk=m.pk).update(queued_at=timezone.now() - timedelta(minutes=40))
    comms.process_outbox(g)
    m.refresh_from_db()
    assert m.status == M.FAILED_UNCONFIRMED and "network down" in m.last_error and m.attempts == 2


def _approval_subject(w):
    return f"Re: SED {w['iw'].iw_no} R01 [{w['mails'][1].stamp}]"


def test_depot_manager_reply(sent):
    w = sent
    subj = _approval_subject(w)
    assert comms.handle_reply(_reply(subj, "APPROVED", sender="someone@example.com", mid="<a@x>")).startswith("ignored")
    assert comms.handle_reply(_reply(subj, "APPROVED", auth="spf=fail; dkim=fail", mid="<b@x>")).startswith("ignored")
    assert comms.handle_reply(_reply(subj, "Looks fine", mid="<c@x>")).startswith("ignored: first line")
    assert comms.handle_reply(_reply(subj, "REJECTED damage qty to be rechecked", mid="<d@x>")) == "rejected"
    ap = SedApproval.objects.get(iw=w["iw"])
    assert ap.status == "REJECTED" and ap.decision_text == "damage qty to be rechecked" and ap.evidence
    assert comms.handle_reply(_reply(subj, "REJECTED again", mid="<d@x>")) == "skip"   # same message twice
    assert comms.handle_reply(_reply(subj, "Approved.", mid="<e@x>")) == "approved"
    ap.refresh_from_db()
    assert ap.status == "APPROVED" and ap.reply_from == DM


def test_reminder_after_24_hours(sent):
    w, g = sent, sent["gmail"]
    comms.process_outbox(g)
    comms.process_outbox(g)
    assert comms.send_reminders() == 0
    assert comms.send_reminders(now=timezone.now() + timedelta(hours=25)) == 1
    rem = MailMessage.objects.get(purpose="SED_REMINDER")
    assert rem.subject.startswith("REMINDER:") and "REMINDER" in rem.html_body and rem.to == [DM]
    assert comms.send_reminders(now=timezone.now() + timedelta(hours=50)) == 0


def test_mail_pending_release(sent, make_user):
    w = sent
    cfa = make_user("cfa_rel", "CFA_MANAGER")
    with pytest.raises(IwError, match="pending for 2 hours"):
        comms.release(cfa, w["iw"].pk, "Gmail down at the depot")
    MailMessage.objects.filter(iw=w["iw"]).update(created_at=timezone.now() - timedelta(hours=3))
    with pytest.raises(IwError, match="reason"):
        comms.release(cfa, w["iw"].pk, " ")
    iw = comms.release(cfa, w["iw"].pk, "Gmail down at the depot")
    assert iw.status == Iw.Status.MRA_ISSUED and MailRelease.objects.get(iw=iw).pending


def test_manual_evidence(sent, make_user):
    w = sent
    cfa = make_user("cfa_ev", "CFA_MANAGER")
    ap = SedApproval.objects.get(iw=w["iw"])
    with pytest.raises(IwError, match=".eml or PDF"):
        comms.upload_evidence(w["clerk"], ap.pk, b"x", "approval.txt")
    comms.upload_evidence(w["clerk"], ap.pk, b"%PDF-1.7 approval", "whatsapp-approval.pdf")
    ap = comms.decide_evidence(cfa, ap.pk, True)
    assert ap.status == "MANUAL_OK" and ap.accepted_by == cfa


def test_email_verification_by_cfa(issued, make_user):
    w = issued
    cfa = make_user("cfa_mail", "CFA_MANAGER")
    comms.capture_email(w["clerk"], w["iw"].pk, "ORG", "390", "wrong@example.com")
    e = OrganisationEmail.objects.get(organisation_id="390")
    comms.verify_email(cfa, "ORG", str(e.pk), "right@example.com")
    e.refresh_from_db()
    assert (e.email, e.status) == ("right@example.com", EmailStatus.VERIFIED) and not comms.unverified_emails()


def test_screens(client, sent, make_user):
    w = sent
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:mra", args=[w["iw"].pk])).content.decode()
    assert "Screen 04 gates" in page and "G04-MAIL" in page and "SED approval request" in page
    assert "Approval requested" in page
    cfa = make_user("cfa_scr", "CFA_MANAGER")
    comms.capture_email(w["clerk"], w["iw"].pk, "ORG", "390", "new@example.com")
    c2 = verified_client(client, cfa)
    page = c2.get(reverse("iw:email_verify")).content.decode()
    assert "new@example.com" in page


def test_sed_audit_memo_is_complete_and_versioned(sent, monkeypatch):
    from inward.core import pdf
    from inward.iw import audit_memo
    monkeypatch.setattr(pdf, "html_to_pdf", lambda html, stylesheet="": b"%PDF-1.7 " + html.encode()[:2000])
    w, g = sent, sent["gmail"]
    comms.process_outbox(g)
    comms.process_outbox(g)                                   # all SENT -> memo V1 when the request is sent
    v = audit_memo.versions(w["iw"])
    assert len(v) == 1 and v[0].doc_type == "SED_AUDIT_MEMO"
    assert comms.handle_reply(_reply(_approval_subject(w), "APPROVED", mid="<ok@x>")) == "approved"
    v = audit_memo.versions(Iw.objects.get(pk=w["iw"].pk))
    assert len(v) == 2 and v[0].supersedes == v[1]          # new version, the earlier one kept
    html = audit_memo.render(Iw.objects.get(pk=w["iw"].pk), 3)
    for text in ("SED AUDIT MEMO", "Declaration at physical verification", "Approved - mail on file",
                 "passed Gmail sender authentication", DM, "Damage photos", "SHA-256", "(copy pending)",
                 "Where the records are kept", "Short memo", "BERGER PAINTS INDIA LTD"):
        assert text in html, text


def test_audit_memo_screens(client, sent):
    c = verified_client(client, sent["clerk"])
    page = c.get(reverse("iw:mra", args=[sent["iw"].pk])).content.decode()
    assert "SED Audit Memo" in page
    assert "SED AUDIT MEMO" in c.get(reverse("iw:audit_memo_view", args=[sent["iw"].pk])).content.decode()


def test_notification_emails_screen(client, seeded, make_user):
    cfa = verified_client(client, make_user("cfa_notif", "CFA_MANAGER"))
    page = cfa.get(reverse("core:notifications")).content.decode()
    assert "Notification e-mails" in page and "Depot Manager" in page and "No e-mail yet for" in page
    roles = sorted(set(NotificationRecipient.objects.values_list("role_label", flat=True)))
    data = {f"role_{i}": f"{r.split()[0].lower()}@example.com" for i, r in enumerate(roles)}
    resp = cfa.post(reverse("core:notifications"), data, follow=True)
    assert "Notification e-mails saved" in resp.content.decode()
    assert set(NotificationRecipient.objects.filter(role_label="Depot Manager").values_list("email", flat=True)) == {"depot@example.com"}
    assert comms.internal("SED_APPROVAL")[0] == ["depot@example.com"]
    bad = cfa.post(reverse("core:notifications"), {**data, "role_0": "not-an-email"}, follow=True)
    assert "is not a valid e-mail" in bad.content.decode()
    clerk = make_user("clerk_notif", "DEPOT_CLERK")
    client.logout()
    assert verified_client(client, clerk).get(reverse("core:notifications")).status_code == 403


def test_default_mra_cc(client, issued, make_user):
    """21-Oct-2026: Notification e-mails -> default Cc on every MRA mail."""
    w = issued
    cfa = verified_client(client, make_user("cfa_mracc", "CFA_MANAGER"))
    roles = sorted(set(NotificationRecipient.objects.values_list("role_label", flat=True)))
    data = {f"role_{i}": (NotificationRecipient.objects.filter(role_label=r).exclude(email="")
                          .values_list("email", flat=True).first() or "x@example.com") for i, r in enumerate(roles)}
    bad = cfa.post(reverse("core:notifications"), {**data, "mra_cc": "ok@example.com, wrong"}, follow=True)
    assert "Default MRA Cc: &#x27;wrong&#x27;" in bad.content.decode() and not comms.default_mra_cc()
    cfa.post(reverse("core:notifications"), {**data, "mra_cc": "Copy1@Example.com; copy2@example.com"})
    assert comms.default_mra_cc() == ["copy1@example.com", "copy2@example.com"]
    page = cfa.get(reverse("core:notifications")).content.decode()
    assert "copy1@example.com, copy2@example.com" in page and "MRA default Cc</b>" not in page   # not a person row
    OrganisationEmail.objects.create(organisation_id="390", email="h@example.com", kind="TO", status=EmailStatus.VERIFIED)
    m = comms.send_mra(w["clerk"], w["iw"].pk, "copy2@example.com")[0]
    assert m.to == ["h@example.com"] and m.cc == ["copy1@example.com", "copy2@example.com"]
