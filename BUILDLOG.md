# Greenroom build log

A running, honest log of the 48-hour build. Decisions, dead ends and timings.
This becomes the blog post and the README "learnings" section.

---

## Sat 29 Aug — Step 0: project from nothing

**Stage One stack verification (blocking, before any code).** Fetched live docs rather
than trusting memory. Findings:

- `gemini-3.5-flash` is the exact model ID on **both** the Gemini API and Vertex AI, and
  is GA/stable on both. Chose **Vertex AI** so there is no API key anywhere in the
  system — Cloud Run uses Application Default Credentials. That removes a whole class of
  credential-handling risk and gives one auth path shared with Imagen and Cloud Trace.
  Noted that `gemini-3.7-flash` and `3.6-flash` are now stable and newer; 3.5 Flash is
  used because the hackathon rules mandate it, and there is a comment in
  `config/models.py` saying so, so it does not read as staleness.
- `google-adk` current release is **2.8.0** (26 Aug 2026). ADK 2.x is a *breaking*
  rewrite of 1.x — the "Workflow Runtime" replaces the hierarchical agent executor with
  a graph-based engine. Almost all third-party ADK material online is 1.x. Decision: pin
  `google-adk==2.8.0` and read the 2.0 docs per module rather than pattern-match from
  memory or blog posts.
- Deployment: not using `adk deploy cloud_run`. We need `/inbound`, `/tick` and the
  dashboard on the same service, which the CLI-generated app does not provide. Using our
  own container with `get_fast_api_app()` from `google.adk.cli.fast_api` mounted inside
  a FastAPI app we control.
- **Imagen is deprecation-flagged.** Google's own migration table lists every
  `imagen-*` endpoint (including `imagen-4.0-generate-001`) as deprecated with a
  recommended migration date of **30 June 2026** — already past — pointing at the Gemini
  image family instead. The hackathon bonus names Imagen specifically. Mitigation: the
  image model is one config constant, and we make a single real call early on Sunday to
  find out whether it still serves, with `gemini-3.1-flash-image` as the fallback.
  Better to discover this on Sunday afternoon than Monday evening.
- Imagen supports 1:1, 3:4, 4:3, 16:9, 9:16 — **not 4:5**. The 1080×1350 poster is
  generated at 3:4 / 2K and centre-cropped with Pillow.
- Gmail push needs `roles/pubsub.publisher` on the topic for
  `gmail-api-push@system.gserviceaccount.com`, and `watch()` must be re-called at least
  every 7 days. The notification payload carries **no message content** — only
  `{emailAddress, historyId}` — so the read path is explicitly ours to scope, which
  suits the containment rules.
- Risk logged: a label-scoped watch may not reliably fire for inbound *replies* into an
  already-labelled thread. Rather than bet the live demo on it, the hourly tick also
  reconciles via `history.list` over threads we already own. Push is the fast path, the
  tick is the guarantee.

**Environment.** `git config user.email` was already `crazyxydj@gmail.com` — correct
owner, no change needed. `gh` already authenticated. Two gaps found: no Python 3.12 on
the machine (3.13 and 3.14 only) and no local Docker. Installed `uv` to manage a pinned
3.12 toolchain without touching the system Python. No Docker is a non-issue — Cloud Run
source deploys build on Cloud Build, so `make deploy` needs no local daemon.

**Repo.** `git init -b main`, targeted `.gitignore` (deliberately *not* a blanket
`*.json` ignore, which would silently swallow fixtures; instead specific
credential-shaped filenames plus `credentials/` and `.env*`), Makefile with
`setup / test / run-local / deploy`, README with the housekeeping checklist for granting
judge access.

---

## Sun 30 Aug (00:xx) — Step 1: scaffold, config, ADK round trip

**Verified the ADK API against the installed package rather than the docs.** Worth the
five minutes: `adk.dev`'s quickstart page still serves 1.x-flavoured examples, but
introspecting `google-adk==2.8.0` confirmed the real surface —
`LlmAgent(model=, name=, instruction=, tools=[], output_schema=, before_tool_callback=…)`,
`Runner(app_name=, agent=, session_service=, plugins=)`,
`runner.run_async(user_id=, session_id=, new_message=)`. Two finds that shape later
steps: `LlmAgent.output_schema` gives the Gatekeeper structured output for free, and
`before_tool_callback` is the right place to enforce tool scoping and emit an OTel span
per tool call, rather than trusting an instruction.

**Decision: not using `adk deploy cloud_run` or `get_fast_api_app()`.** Greenroom needs
`/inbound`, `/tick` and the dashboard on the same service; the ADK-generated app exposes
none of them, and ADK's own docs say its bundled web UI is not for production. We own
the FastAPI app and call `Runner` in-process, which is a supported path and keeps one
service, one container, one URL for the demo.

**Config is strict on purpose.** Every schema is `extra="forbid"` and cross-field
validated: `fee.floor` above `fee.standard` is rejected (the agent would have no room to
negotiate and would escalate every counter-offer), a follow-up scheduled after
`close_after_days` is rejected (it could never fire), and a date window ending before it
starts is rejected. A misspelled policy key now fails at container start rather than
silently changing what the agent will agree to at 2am. 24 tests, all passing.

**Two defaults chosen so that forgetting something is safe rather than expensive:**
`GREENROOM_DRY_RUN` defaults to `true`, and `trust.default_mode` is `review`. The failure
mode of a mistake is "nothing happened", not "we emailed a real students' union".

**Snag: `pydantic[email]`.** `EmailStr` needs the extra, which is not obvious until it
raises at import time. Added to `pyproject.toml`.

**Blocked on:** the GCP project ID. Everything above runs and is tested locally; the
Cloud Run half of step 1 (prove the round trip on real infrastructure) needs
`gcloud auth login` and a project. `gcloud config get-value project` is currently unset.

---

## Sun 30 Aug — Step 2: OAuth, Gmail and Calendar wrappers

**Chose a user OAuth client over a service account with domain-wide delegation.** DWD
would have been quicker to wire up, but it grants access to *every* mailbox in the
Workspace domain and is configured at the org level. A single user-consented refresh
token for admin@beatidapp.com can only ever reach that one mailbox, and Xavier can
revoke it himself without touching the admin console. Smaller blast radius for a system
whose whole pitch is "it acts on my real inbox".

**Containment is structural, not instructional.** The scope we are forced to hold
(`gmail.modify` — nothing narrower can attach a label to a thread) is broader than the
rules Greenroom must obey, so the gap is closed in code:

  * `GmailTool` has no `delete`, `trash`, `untrash`, `archive` or `remove_label` method.
    The capability is absent, not discouraged. There is a test that fails if anyone
    adds one.
  * `CalendarTool` has no `update`, `patch`, `delete` or `move`. Create-only, asserted.
  * Every send re-checks the recipient against `targets.csv`, including replies — so a
    poisoned reply-to header cannot walk the conversation to a new address.
  * Reads take a threadId Greenroom recorded, or a `label:greenroom` query. There is no
    "fetch arbitrary message" entry point for an injection to reach for.
  * `freebusy` is used instead of `events.list`, so proposing a slot never reads the
    contents of Xavier's other meetings — only whether a window is busy.

Nine of the 38 tests exist purely to assert the absent capabilities. That felt like
over-testing until I wrote the note above: these are the only things standing between a
prompt injection and a live company inbox.

**Idempotency on the calendar.** The job's idempotency key becomes the Calendar event
`id`, so a re-run of a crashed booking job collides (409) instead of double-booking. The
409 is caught and treated as success, because it is.

**`prompt=consent` matters more than it looks.** Without `access_type=offline` *and*
`prompt=consent`, Google returns no refresh token when an account has consented before
— the local flow appears to succeed and the deployed agent then silently cannot refresh.
`scripts/bootstrap_oauth.py` forces both and hard-fails if any scope was not granted,
rather than letting that surface as a 403 mid-negotiation on Monday.

**Blocked on:** project ID, and the OAuth client itself (Xavier's hands — see checklist).

---

## Sun 30 Aug — Step 1 completed: live on Cloud Run

Project `beatid-greenroom` (`29925954133`), `europe-west2`, Firestore Native, dedicated
least-privilege runtime service account. `/health`, `/readyz` and `/hello` all green:
Firestore reachable and ADK → Gemini 3.5 Flash answering on Vertex AI.

No Cloud organisation exists on this account, so the OAuth client has to be **External**.
That would normally mean 7-day refresh-token expiry, which would leave a judge
reproducing this in September with a dead token. Checked the rules rather than assuming:
an app in **In Production** status, even unverified, issues non-expiring refresh tokens —
the cost is an "unverified app" warning screen and a 100-user cap. We have one user.
So: External + published to Production, and the README warns judges about the screen.

**Three bugs the first deploy found, all worth the trip:**

1. **`config_dir()` was resolved relative to the installed package.** Fine in a checkout,
   nonsense in a container where the package lives in site-packages and `config/` is at
   `/app/config`. The container refused to boot with
   `missing config file: /usr/local/lib/python3.12/config/brand.yaml` — which is the
   fail-fast startup behaviour working exactly as intended, on its first real outing.
   Now searches an ordered candidate list and names every path it tried on failure.

2. **Cloud Run's front end swallows `/healthz`.** It answered our request with Google's
   own 404 page before it ever reached the container — `/readyz` and `/hello` on the same
   revision were fine, and the route was definitely registered. Renamed to `/health`.
   Ten confusing minutes; noting it here so it is ten minutes nobody spends again.

3. **The genai SDK reads `GOOGLE_GENAI_USE_VERTEXAI` from `os.environ`, not from our
   Settings object.** On Cloud Run the vars are set on the service so it worked by
   accident; locally, values loaded from `.env` were invisible to the SDK and it demanded
   an API key. `Settings.export_genai_env()` now mirrors them, so local and deployed
   behave identically — which matters, because "works on Cloud Run only" is not a thing
   you want to discover on Monday.

**One demo-credibility fix.** The hello agent was instructed to name its own model and
confidently answered "Gemini 1.5 Pro" while being served by `gemini-3.5-flash`. Models
do not reliably know their own ID. Removed the instruction: the model ID a judge sees now
comes from our config constant and the Cloud Trace span, both of which are facts. Worth
remembering for the Stage One check — a screenshot of a model naming itself proves
nothing.
