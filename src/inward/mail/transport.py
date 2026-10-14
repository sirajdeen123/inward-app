"""Mail transport interface (SoT 14.2). Implementation: Gmail SMTP + IMAP. Others by configuration only."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class OutgoingMessage:
    stamp: str                      # unique reference, e.g. "IW-2627-000123 MRA-R01 #C-000456"
    message_id: str                 # generated before the first attempt; reused by every retry
    to: list[str]
    subject: str
    html_body: str
    cc: list[str] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)

    @property
    def stamped_subject(self) -> str:
        return f"{self.subject} [{self.stamp}]"


@dataclass
class SentEvidence:
    stamp: str
    message_id: str
    sent_at: datetime
    to: str
    cc: str
    subject: str
    eml: bytes  # complete message as stored in Sent Mail, kept in the IW document bundle


@dataclass
class FetchedMail:
    message_id: str
    subject: str
    received_at: datetime
    sender: str
    attachments: list[Path]


class MailTransport(Protocol):
    def send(self, message: OutgoingMessage) -> None:
        """Hand the message to the mail system. Afterwards it is QUEUED, not SENT."""

    def confirm_sent(self, message: OutgoingMessage) -> SentEvidence | None:
        """Evidence only when the stamped message is found in the sent-mail folder."""

    def fetch_latest(self, subject_contains: str, save_dir: Path,
                     extensions: tuple[str, ...] = (".xls", ".xlsx")) -> FetchedMail | None:
        """Latest inbox message whose subject contains the text; matching attachments saved to save_dir."""
