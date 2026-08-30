"""Insert a test inbound email into an owned thread, to demonstrate the Gatekeeper.

    uv run python scripts/inject_test_email.py --list
    uv run python scripts/inject_test_email.py --fixture subtle_embedded_instruction

Uses Gmail's `users.messages.insert`, which places a message directly into the mailbox
WITHOUT sending anything. Nothing leaves the building; no real address receives mail.
That matters for a demo: the quarantine path can be shown on command, repeatedly, on
camera, without needing a second human to send an attack email at the right moment.

Guard rails, because this writes into a live mailbox:
  * It refuses to insert into a thread Greenroom does not own.
  * The sender is forced to the target's real address from targets.csv, so the inserted
    message looks exactly like a genuine reply and exercises the real code path.
  * Every inserted message carries an X-Greenroom-Test header, so a test injection can
    always be told apart from a real one afterwards.

This is the mechanism behind seed_demo.py's "one injection email queued".
"""

from __future__ import annotations

import argparse
import base64
import sys
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures.inbound_emails import ALL_FIXTURES  # noqa: E402

from greenroom.state.db import get_db  # noqa: E402
from greenroom.tools.google_auth import gmail_service  # noqa: E402

TEST_HEADER = "X-Greenroom-Test"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="fixture key from tests/fixtures/inbound_emails.py")
    parser.add_argument("--list", action="store_true", help="list available fixtures")
    parser.add_argument("--thread-id", help="thread to insert into (default: the only one)")
    parser.add_argument("--body", help="custom body instead of a fixture")
    parser.add_argument("--subject", default="", help="subject for a custom body")
    args = parser.parse_args()

    if args.list:
        print(f"{'key':34} {'attack':7} note")
        for f in ALL_FIXTURES:
            print(f"  {f.key:32} {str(f.expect_injection):7} {f.note[:60]}")
        return 0

    if not args.fixture and not args.body:
        print("Pass --fixture KEY or --body TEXT. Use --list to see fixtures.", file=sys.stderr)
        return 1

    db = get_db()
    threads = {d.id: d.to_dict() for d in db.collection("threads").stream()}
    if not threads:
        print("No threads yet — send a pitch first.", file=sys.stderr)
        return 1

    thread_id = args.thread_id or next(iter(threads))
    if thread_id not in threads:
        print(f"Refusing: thread {thread_id} is not one Greenroom created.", file=sys.stderr)
        return 1

    target_doc = db.collection("targets").document(threads[thread_id]["target_id"]).get()
    if not target_doc.exists:
        print("Thread has no target.", file=sys.stderr)
        return 1
    target = target_doc.to_dict()

    if args.body:
        subject, body, label = args.subject or "Re: your enquiry", args.body, "custom"
    else:
        fixture = next((f for f in ALL_FIXTURES if f.key == args.fixture), None)
        if fixture is None:
            print(f"No fixture named {args.fixture!r}. Use --list.", file=sys.stderr)
            return 1
        subject, body, label = fixture.subject, fixture.body, fixture.key

    svc = gmail_service()
    original = threads[thread_id].get("subject", "")
    msg = EmailMessage()
    msg["To"] = "me"
    sender_name = target.get("contact_name") or target["organisation"]
    msg["From"] = formataddr((sender_name, target["email"]))
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {original}"
    msg["Message-ID"] = make_msgid(domain="greenroom-test.invalid")
    msg[TEST_HEADER] = label
    msg.set_content(body)

    inserted = (
        svc.users()
        .messages()
        .insert(
            userId="me",
            internalDateSource="receivedTime",
            body={
                "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
                "threadId": thread_id,
                "labelIds": ["INBOX", "UNREAD"],
            },
        )
        .execute()
    )

    print(f"Inserted test message into thread {thread_id}")
    print(f"  fixture   : {label}")
    print(f"  from      : {target['email']}")
    print(f"  message id: {inserted['id']}")
    print("\nNothing was sent. Run the tick to have Greenroom process it:")
    print("  curl -s -X POST $SERVICE_URL/tick | python3 -m json.tool")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
