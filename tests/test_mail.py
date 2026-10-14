"""Gmail transport, offline parts (SoT 14.2). Live sending is proven with `manage.py mail_spike`."""
from email import message_from_bytes, policy

from inward.mail.gmail import STAMP_HEADER, GmailTransport, new_message_id
from inward.mail.transport import OutgoingMessage


def test_message_carries_stamp_id_and_attachment(tmp_path):
    pdf = tmp_path / "MRA_IW-2627-000001_R01.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    msg = OutgoingMessage(stamp="IW-2627-000001 MRA-R01 #C-000001",
                          message_id=new_message_id("inward.rainbowlogistics.co.in"),
                          to=["source@example.com"], cc=["cc@example.com"], subject="MRA IW-2627-000001",
                          html_body="<p>MRA attached</p>", attachments=[pdf])
    built = GmailTransport("app@example.com", "abcd efgh ijkl mnop").build(msg)
    parsed = message_from_bytes(built.as_bytes(), policy=policy.default)
    assert parsed["Subject"] == "MRA IW-2627-000001 [IW-2627-000001 MRA-R01 #C-000001]"
    assert parsed["Message-ID"] == msg.message_id and msg.message_id.endswith("@inward.rainbowlogistics.co.in>")
    assert parsed[STAMP_HEADER] == msg.stamp
    assert parsed["Cc"] == "cc@example.com"
    names = [p.get_filename() for p in parsed.iter_attachments()]
    assert names == ["MRA_IW-2627-000001_R01.pdf"]


def test_sent_folder_found_by_special_use_flag():
    class FakeImap:
        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                          b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']
    assert GmailTransport._sent_folder(FakeImap()) == '"[Gmail]/Sent Mail"'


def _msg():
    return OutgoingMessage(stamp="IW-2627-000002 MRA-R01 #C-000002", message_id=new_message_id("example.com"),
                           to=["source.org@example.com"], cc=["manager@example.com"], subject="MRA IW-2627-000002",
                           html_body="<p>MRA</p>")


def test_test_mode_redirects_every_message():
    built = GmailTransport("app@example.com", "pw", test_redirect=["tester@example.com"]).build(_msg())
    parsed = message_from_bytes(built.as_bytes(), policy=policy.default)
    assert parsed["To"] == "tester@example.com" and parsed["Cc"] is None
    assert parsed["Subject"].startswith("[TEST] ")
    assert parsed["X-Inward-Original-To"] == "source.org@example.com"
    assert "would have gone to: source.org@example.com" in parsed.get_body(("html",)).get_content()


def test_test_mode_refuses_to_send_without_a_test_address(settings):
    import pytest

    from inward.mail.gmail import from_settings
    settings.INWARD = {**settings.INWARD, "MODE": "TEST", "TEST_MAIL_TO": [],
                       "GMAIL_ADDRESS": "app@example.com", "GMAIL_APP_PASSWORD": "pw"}
    with pytest.raises(ValueError, match="INWARD_TEST_MAIL_TO"):
        from_settings()
    settings.INWARD = {**settings.INWARD, "MODE": "LIVE"}
    assert from_settings().test_redirect is None
