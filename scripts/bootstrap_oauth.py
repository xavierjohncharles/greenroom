"""One-time: consent as admin@beatidapp.com and put the refresh token in Secret Manager.

https://developers.google.com/identity/protocols/oauth2/native-app
https://docs.cloud.google.com/secret-manager/docs/create-secret-quickstart

Run this once, locally, from the machine where you can open a browser:

    uv run python scripts/bootstrap_oauth.py --client-json ~/Downloads/client_secret_*.json

It opens a browser, asks you to sign in as the agent mailbox, and writes two secrets:

    greenroom-oauth-client          the OAuth client JSON
    greenroom-oauth-refresh-token   {"refresh_token": "..."}

The refresh token is never written to disk by this script and never printed. Delete the
downloaded client JSON afterwards — the copy in Secret Manager is the one that matters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google.cloud import secretmanager
from google_auth_oauthlib.flow import InstalledAppFlow
from oauthlib.oauth2.rfc6749.errors import MismatchingStateError, OAuth2Error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from greenroom.tools.google_auth import SCOPES  # noqa: E402


def upsert_secret(
    client: secretmanager.SecretManagerServiceClient, project: str, name: str, payload: str
) -> str:
    parent = f"projects/{project}"
    secret_path = f"{parent}/secrets/{name}"
    try:
        client.get_secret(request={"name": secret_path})
    except Exception:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"  created secret {name}")
    version = client.add_secret_version(
        request={"parent": secret_path, "payload": {"data": payload.encode("utf-8")}}
    )
    return version.name


def _authorise(flow, args):
    """Run the local-server OAuth dance. Split out so main() can handle its errors."""
    return flow.run_local_server(
        port=args.port,
        open_browser=args.open_browser,
        authorization_prompt_message=(
            "\n" + "=" * 70 + "\n"
            "  COPY THE URL BELOW into a PRIVATE / INCOGNITO window.\n"
            f"  Sign in as: {args.expect_mailbox}\n"
            "  Do not open it in your normal browser — it will reuse the account you\n"
            "  are already signed into.\n" + "=" * 70 + "\n\n{url}\n\n"
            "Waiting for you to finish in the browser...\n"
        ),
        access_type="offline",
        prompt="select_account consent",
        login_hint=args.expect_mailbox,
    )


def main() -> int:
    # stdout is block-buffered whenever this is not attached to a terminal — under make,
    # or when a caller backgrounds it because the local server blocks for as long as a
    # human takes to consent. A buffered authorisation URL means the script sits waiting
    # for a callback nobody was ever shown how to trigger, which looks exactly like a
    # hang. Flush every line instead of trusting the caller's environment.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # not a real stream (captured, piped oddly)
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-json", required=True, help="OAuth client JSON from the console")
    parser.add_argument("--project", help="GCP project id (defaults to gcloud's current)")
    parser.add_argument("--client-secret-name", default="greenroom-oauth-client")
    parser.add_argument("--token-secret-name", default="greenroom-oauth-refresh-token")
    parser.add_argument("--port", type=int, default=8765, help="local redirect port")
    parser.add_argument(
        "--expect-mailbox",
        default=None,
        help="the mailbox that must be authorised (defaults to the configured agent mailbox)",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help=(
            "open the default browser instead of printing the URL. Off by default: the "
            "default browser reuses whichever Google account is already signed in."
        ),
    )
    args = parser.parse_args()

    if not args.expect_mailbox:
        from greenroom.settings import get_settings

        args.expect_mailbox = get_settings().agent_mailbox

    if not args.expect_mailbox:
        print(
            "No agent mailbox configured. Set GREENROOM_MAILBOX in .env (it must be a\n"
            "Google account — the Gmail API cannot serve a mailbox hosted elsewhere),\n"
            "or pass --expect-mailbox.",
            file=sys.stderr,
        )
        return 1

    project = args.project
    if not project:
        import subprocess

        project = subprocess.run(
            ["gcloud", "config", "get-value", "project"], capture_output=True, text=True
        ).stdout.strip()
    if not project or project == "(unset)":
        print("No project. Pass --project or run: gcloud config set project <id>", file=sys.stderr)
        return 1

    client_path = Path(args.client_json).expanduser()
    if not client_path.exists():
        print(f"Client JSON not found: {client_path}", file=sys.stderr)
        return 1

    print(f"Project: {project}")
    print("Scopes being requested:")
    for s in SCOPES:
        print(f"  {s}")
    print()
    print("=" * 70)
    print(f"  SIGN IN AS: {args.expect_mailbox}")
    print("  NOT your personal account. Chrome will reuse whichever account you are")
    print("  already signed into, so use a private/incognito window if unsure.")
    print("  This script refuses to store a token for any other mailbox.")
    print("=" * 70)
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=list(SCOPES))

    # `access_type=offline` + `prompt=consent` guarantee a refresh token even if this
    # account has consented before; without prompt=consent Google returns none on a
    # repeat authorisation and the deployed agent silently cannot refresh.
    #
    # `select_account` and `login_hint` exist because of two failed runs: opening the
    # default browser meant Chrome silently reused the account already signed in, and
    # authorised a personal inbox twice in a row. The browser is now NOT opened by
    # default — the URL is printed for pasting into a private window, which is the only
    # reliable way to control which account consents.
    try:
        creds = _authorise(flow, args)
    except MismatchingStateError:
        print(
            "\nFAILED: CSRF state mismatch.\n\n"
            "This means the browser completed an authorisation from an EARLIER run of\n"
            "this script. Each run generates a fresh state value, so an old tab landing\n"
            f"on http://localhost:{args.port} is rejected.\n\n"
            "To fix it:\n"
            "  1. Quit the private/incognito window completely (Cmd-Shift-W), so no old\n"
            "     tab can fire a callback.\n"
            "  2. Re-run this script.\n"
            "  3. Copy the NEW url it prints — not one from scrollback.\n\n"
            "Nothing has been written.",
            file=sys.stderr,
        )
        return 1
    except OAuth2Error as exc:
        print(
            f"\nFAILED: authorisation was refused: {exc}\nNothing has been written.",
            file=sys.stderr,
        )
        return 1

    if not creds.refresh_token:
        print(
            "No refresh token returned. Re-run; the consent screen must be shown.", file=sys.stderr
        )
        return 1

    # --- verify BEFORE writing anything ---------------------------------
    #
    # An earlier version of this script trusted `creds.scopes` and skipped the mailbox
    # check entirely. Both failed silently in practice: the OAuth library reports the
    # scopes it *requested*, not the ones actually granted, and Chrome happily
    # authorised a personal account that the operator never intended. A refresh token
    # for the wrong mailbox is the single worst thing this script could produce, so it
    # is now proven to work before it is allowed anywhere near Secret Manager.
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    print("\nVerifying the token actually works...")

    try:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = gmail.users().getProfile(userId="me").execute()
    except HttpError as exc:
        print(f"\nFAILED: could not read the Gmail profile: {exc}", file=sys.stderr)
        print("Gmail scopes were probably not granted. Re-run and tick every box.", file=sys.stderr)
        return 1

    authorised = profile.get("emailAddress", "")
    if authorised.lower() != args.expect_mailbox.lower():
        print(
            f"\nFAILED: you authorised {authorised!r}, but Greenroom is configured for "
            f"{args.expect_mailbox!r}.\n\n"
            "Nothing has been written. Re-run and sign in as the agent mailbox — use a\n"
            "private/incognito window, because Chrome will otherwise reuse whichever\n"
            "account you are already signed into.\n",
            file=sys.stderr,
        )
        return 1
    print(f"  Gmail    OK — authorised as {authorised} ({profile.get('messagesTotal')} messages)")

    try:
        calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        primary = calendar.calendars().get(calendarId="primary").execute()
    except HttpError as exc:
        print(f"\nFAILED: Calendar access does not work: {exc}\n", file=sys.stderr)
        print(
            "The calendar scopes were not granted. Two things to check:\n"
            "  1. Data Access on the consent screen lists calendar.events and\n"
            "     calendar.freebusy\n"
            "     https://console.cloud.google.com/auth/scopes?project=" + project + "\n"
            "  2. You ticked every permission box on the consent screen.\n"
            "Nothing has been written.",
            file=sys.stderr,
        )
        return 1
    print(f"  Calendar OK — primary is {primary.get('id')} ({primary.get('timeZone')})")

    client = secretmanager.SecretManagerServiceClient()
    print("\nWriting secrets...")
    upsert_secret(client, project, args.client_secret_name, client_path.read_text())
    upsert_secret(
        client, project, args.token_secret_name, json.dumps({"refresh_token": creds.refresh_token})
    )

    print("\n✓ Done. Both secrets are in Secret Manager.")
    print("\nNext:")
    print(f"  1. rm {client_path}          # the console copy is no longer needed")
    print("  2. Grant the Cloud Run service account read access:")
    print(
        f"     gcloud secrets add-iam-policy-binding {args.token_secret_name} \\\n"
        f"       --member=serviceAccount:<RUNTIME_SA> --role=roles/secretmanager.secretAccessor"
    )
    print("  3. Verify from the deployed service:  curl $SERVICE_URL/readyz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
