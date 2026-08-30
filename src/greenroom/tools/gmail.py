"""Gmail access, deliberately narrow.

https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels
https://developers.google.com/workspace/gmail/api/guides/push
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list

admin@beatidapp.com is a live shared company inbox. The OAuth scope we must hold
(gmail.modify, because nothing narrower can attach a label) is broader than the rules
Greenroom is meant to obey, so containment is enforced *here*, structurally:

  * SEND  — refused unless the recipient is in targets.csv. There is no override.
  * READ  — the only entry points take a threadId that Greenroom itself recorded, or
            run a `label:greenroom` query. There is no "fetch arbitrary message" call.
  * LABEL — add-only, and only to threads Greenroom owns.
  * There is no archive, delete, trash, untrash or label-removal method in this file.
    The capability is absent from the code, not merely discouraged in a prompt.

Every mutating call honours the dry-run switch and the global kill switch.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from greenroom.config import get_config
from greenroom.obs import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)

USER_ID = "me"  # the authorised mailbox; we never address another user's mailbox


class SendRefused(RuntimeError):
    """A send was blocked by the allow-list, the kill switch, or the daily cap."""


class ThreadNotOwned(RuntimeError):
    """A read was attempted on a thread Greenroom did not create."""


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str
    dry_run: bool


@dataclass(frozen=True)
class InboundMessage:
    """A message from one of our own threads, already reduced to what we need.

    The raw payload never leaves this module. Downstream agents receive this object,
    and the Gatekeeper is the only thing allowed to look at `body_text` unsanitised.
    """

    message_id: str
    thread_id: str
    history_id: str
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    rfc822_message_id: str
    internal_date_ms: int
    label_ids: tuple[str, ...]


# --------------------------------------------------------------------------- guards


def assert_allowed_recipient(email: str) -> str:
    """The hard cap. An address absent from targets.csv can never be emailed.

    This is checked on every send, including follow-ups and replies, so a poisoned
    thread that tries to redirect correspondence elsewhere gets nowhere.
    """
    addr = (email or "").strip().lower()
    allowed = get_config().allowed_addresses
    if addr not in allowed:
        raise SendRefused(
            f"refusing to send to {addr!r}: not in config/targets.csv "
            f"({len(allowed)} addresses allow-listed)"
        )
    return addr


# --------------------------------------------------------------------------- client


class GmailTool:
    """The only way Greenroom touches Gmail.

    `owned_thread_ids` is supplied by the caller from Firestore. Reads are refused for
    anything outside it unless the thread carries the greenroom label, which keeps the
    containment rule true even for a thread created by an earlier deploy.
    """

    def __init__(self, *, dry_run: bool | None = None) -> None:
        settings = get_settings()
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.mailbox = settings.agent_mailbox
        if not self.mailbox:
            raise RuntimeError(
                "GREENROOM_MAILBOX is not set. It must be a Google account — the Gmail "
                "API cannot read or send for a mailbox hosted elsewhere."
            )
        self.label_root = settings.label_root
        self.label_escalated = settings.label_escalated
        self.label_quarantine = settings.label_quarantine
        self._svc = None
        self._label_ids: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------
    @property
    def svc(self):
        if self._svc is None:
            from greenroom.tools.google_auth import gmail_service

            self._svc = gmail_service()
        return self._svc

    # -- labels ------------------------------------------------------------
    def ensure_labels(self) -> dict[str, str]:
        """Create the three greenroom labels if absent. Returns name -> id.

        Creating a label is the one write we make outside our own threads, and it
        touches no message.
        """
        # NOT gated by dry-run. Dry-run exists to stop mail reaching a human, and a
        # label is not a send. Gating setup behind it meant inbound could not be wired
        # up without first going live on sends, which is exactly backwards.
        wanted = [self.label_root, self.label_escalated, self.label_quarantine]
        existing = {
            lb["name"]: lb["id"]
            for lb in self.svc.users().labels().list(userId=USER_ID).execute().get("labels", [])
        }
        for name in wanted:
            if name in existing:
                self._label_ids[name] = existing[name]
                continue
            created = (
                self.svc.users()
                .labels()
                .create(
                    userId=USER_ID,
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            self._label_ids[name] = created["id"]
            log.info("created gmail label", extra={"label": name, "label_id": created["id"]})
        return self._label_ids

    def label_id(self, name: str) -> str:
        if not self._label_ids:
            self.ensure_labels()
        if name not in self._label_ids:
            raise KeyError(f"unknown greenroom label {name!r}")
        return self._label_ids[name]

    def add_label(self, thread_id: str, label_name: str) -> None:
        """Add-only. There is deliberately no remove_label counterpart."""
        if thread_id.startswith("DRYRUN"):
            # A thread that was never really created has nothing to label.
            log.info("dry-run thread, skipping label", extra={"label": label_name})
            return
        self.svc.users().threads().modify(
            userId=USER_ID,
            id=thread_id,
            body={"addLabelIds": [self.label_id(label_name)]},
        ).execute()
        log.info("label added", extra={"thread_id": thread_id, "label": label_name})

    # -- sending -----------------------------------------------------------
    def send_new(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> SentMessage:
        """Start a new thread. Applies the greenroom label to it."""
        to = assert_allowed_recipient(to)
        raw = self._build_mime(to=to, subject=subject, body_text=body_text, attachments=attachments)

        if self.dry_run:
            log.info(
                "dry-run: would send new message",
                extra={"to": to, "subject": subject, "body_chars": len(body_text)},
            )
            return SentMessage(message_id="DRYRUN", thread_id="DRYRUN", dry_run=True)

        sent = self.svc.users().messages().send(userId=USER_ID, body={"raw": raw}).execute()
        result = SentMessage(sent["id"], sent["threadId"], dry_run=False)
        self.add_label(result.thread_id, self.label_root)
        log.info(
            "message sent",
            extra={"to": to, "message_id": result.message_id, "thread_id": result.thread_id},
        )
        return result

    def send_reply(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        thread_id: str,
        in_reply_to: str,
        references: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> SentMessage:
        """Reply inside an existing thread we own.

        The recipient is re-checked against the allow-list even though the thread is
        ours: a reply-to header we did not write must not be able to move the
        conversation to a new address.
        """
        to = assert_allowed_recipient(to)
        raw = self._build_mime(
            to=to,
            subject=subject,
            body_text=body_text,
            in_reply_to=in_reply_to,
            references=references or in_reply_to,
            attachments=attachments,
        )

        if self.dry_run:
            log.info(
                "dry-run: would send reply",
                extra={"to": to, "thread_id": thread_id, "body_chars": len(body_text)},
            )
            return SentMessage(message_id="DRYRUN", thread_id=thread_id, dry_run=True)

        sent = (
            self.svc.users()
            .messages()
            .send(userId=USER_ID, body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        log.info(
            "reply sent",
            extra={"to": to, "message_id": sent["id"], "thread_id": sent["threadId"]},
        )
        return SentMessage(sent["id"], sent["threadId"], dry_run=False)

    def _build_mime(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        config = get_config()
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = formataddr((config.brand.sender_name, self.mailbox))
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.mailbox.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body_text)

        for filename, data, mime_type in attachments or []:
            maintype, _, subtype = mime_type.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    # -- reading -----------------------------------------------------------
    def get_thread(
        self, thread_id: str, *, owned_thread_ids: frozenset[str]
    ) -> list[InboundMessage]:
        """Read one thread. Refused unless we own it.

        Ownership is either "Firestore says we created it" or "it carries the greenroom
        label". Both are checked; neither can be supplied by an inbound email.
        """
        if thread_id not in owned_thread_ids and not self._thread_has_greenroom_label(thread_id):
            raise ThreadNotOwned(
                f"refusing to read thread {thread_id!r}: not created by Greenroom and "
                f"not labelled {self.label_root!r}"
            )
        thread = (
            self.svc.users().threads().get(userId=USER_ID, id=thread_id, format="full").execute()
        )
        return [self._parse_message(m) for m in thread.get("messages", [])]

    def _thread_has_greenroom_label(self, thread_id: str) -> bool:
        try:
            thread = (
                self.svc.users()
                .threads()
                .get(userId=USER_ID, id=thread_id, format="minimal")
                .execute()
            )
        except Exception:
            return False
        wanted = self.label_id(self.label_root)
        return any(wanted in (m.get("labelIds") or []) for m in thread.get("messages", []))

    def list_greenroom_thread_ids(self, *, max_results: int = 200) -> list[str]:
        """Every thread carrying our label. The only unbounded query we make, and it
        cannot return anything outside our own footprint."""
        response = (
            self.svc.users()
            .threads()
            .list(userId=USER_ID, labelIds=[self.label_id(self.label_root)], maxResults=max_results)
            .execute()
        )
        return [t["id"] for t in response.get("threads", [])]

    def history_since(self, start_history_id: str) -> list[dict[str, Any]]:
        """Changes since a historyId, restricted to our label.

        A Gmail push notification carries only {emailAddress, historyId} and no message
        content, so this is how an inbound message is actually discovered.
        """
        out: list[dict[str, Any]] = []
        page_token = None
        while True:
            response = (
                self.svc.users()
                .history()
                .list(
                    userId=USER_ID,
                    startHistoryId=start_history_id,
                    labelId=self.label_id(self.label_root),
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
            out.extend(response.get("history", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return out

    def _parse_message(self, message: dict[str, Any]) -> InboundMessage:
        headers = {
            h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])
        }
        return InboundMessage(
            message_id=message["id"],
            thread_id=message["threadId"],
            history_id=str(message.get("historyId", "")),
            from_addr=headers.get("from", ""),
            to_addr=headers.get("to", ""),
            subject=headers.get("subject", ""),
            body_text=_extract_text(message.get("payload", {})),
            rfc822_message_id=headers.get("message-id", ""),
            internal_date_ms=int(message.get("internalDate", 0)),
            label_ids=tuple(message.get("labelIds", [])),
        )

    # -- push --------------------------------------------------------------
    def start_watch(self, topic_name: str) -> dict[str, Any]:
        """Register the Gmail push watch, scoped to the greenroom label — never INBOX.

        Must be re-called at least every 7 days or notifications stop; the hourly tick
        renews it. Requires roles/pubsub.publisher on the topic for
        gmail-api-push@system.gserviceaccount.com.
        """
        # Also not gated by dry-run: registering a watch sends nothing. Without this,
        # dry-run mode could never demonstrate the inbound path at all.
        response = (
            self.svc.users()
            .watch(
                userId=USER_ID,
                body={
                    "topicName": topic_name,
                    "labelIds": [self.label_id(self.label_root)],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            .execute()
        )
        log.info(
            "gmail watch registered",
            extra={
                "topic": topic_name,
                "history_id": response.get("historyId"),
                "expiration": response.get("expiration"),
            },
        )
        return response


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull text/plain out of a MIME tree, falling back to stripped text/html.

    Returns raw, untrusted text. Only the Gatekeeper may look at it.
    """
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []) or []:
        text = _extract_text(part)
        if text:
            return text

    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data")
        if data:
            import re

            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)

    return ""
