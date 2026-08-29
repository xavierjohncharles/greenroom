# Greenroom

**An autonomous booking-and-partnership agent for small event brands.**

Greenroom researches a target venue, writes and sends a personalised pitch, reads the
reply thread, negotiates inside a policy envelope its owner defines, books the call into
a calendar, and escalates to a human only when a decision falls outside policy. It earns
autonomy over time: every target starts in `review` and graduates to `veto` then
`autopilot` as its drafts survive human scrutiny unedited.

First customer: **Beat ID Ltd**, pitching its real pipeline of UK students' unions.

> Google *All Things Agentic* Hackathon — Track: **Taskmaster**.

---

## ⚠️ Housekeeping — do this before submission

- [ ] **Grant read access to the repo for judging:** `testing@devpost.com` and
      `cloudhackathons@google.com`
      (`gh api -X PUT repos/xavierjohncharles/greenroom/collaborators/<user> -f permission=pull`,
      or Settings → Collaborators in the GitHub UI).
- [ ] Confirm the Cloud Run URL in the demo video is visibly a `*.run.app` domain.
- [ ] Check `BUILDLOG.md` reads cleanly — it becomes the blog post and the
      "learnings" section below.

---

## Status

🚧 Under construction — 48-hour build. See `BUILDLOG.md` for the running log.

## Mandatory stack (verified against live docs, 29 Aug 2026)

| Component | Choice | Notes |
|---|---|---|
| Model | `gemini-3.5-flash` via **Vertex AI** | Same model ID on Gemini API and Vertex. Vertex chosen so there is no API key to store — auth is ADC on Cloud Run. |
| Agent framework | **Google ADK** `2.8.0` (Python) | Current release, 26 Aug 2026. ADK 2.x is a breaking rewrite of 1.x (graph Workflow Runtime). |
| Runtime | **Cloud Run** | Agent + dashboard in one service. We own the FastAPI app and drive agents through `google.adk.runners.Runner`; see BUILDLOG for why not `adk deploy cloud_run`. |
| State | **Firestore** (Native mode) | |
| Inbound | **Gmail watch → Pub/Sub → push → `/inbound`** | Watch scoped to the `greenroom` label, never INBOX. |
| Ticks | **Cloud Scheduler** | Hourly tick + an 08:00 Europe/London morning brief. |
| Secrets | **Secret Manager** | OAuth refresh token lives here and nowhere else. |
| Tracing | **Cloud Trace** via OpenTelemetry | One trace per inbound/tick, one span per agent and per tool call. |
| Images | Imagen on Vertex AI | See `BUILDLOG.md` — the Imagen line is deprecation-flagged; model ID is a single config constant with a Gemini-image fallback. |
| Language | **Python 3.12** | |

## OAuth scopes requested

Narrowest set that satisfies the mailbox rules:

| Scope | Why it is needed |
|---|---|
| `https://www.googleapis.com/auth/gmail.send` | Send pitches and replies. |
| `https://www.googleapis.com/auth/gmail.modify` | Required to *apply* the `greenroom` labels, to read the agent's own threads, and to register `users.watch`. `gmail.labels` only manages label definitions and cannot attach them to a thread, so `readonly` + `labels` is not sufficient. `gmail.modify` cannot delete mail. |
| `https://www.googleapis.com/auth/calendar.events` | Create the booked call on the primary calendar. |
| `https://www.googleapis.com/auth/calendar.freebusy` | Propose slots without granting read access to event contents. |

`gmail.modify` is broader than Greenroom's own rules allow. **The rules are enforced in
code, not by the scope.** See "Mailbox containment" below.

## Mailbox containment

`admin@beatidapp.com` is a live shared company inbox. Greenroom is constrained so that
it can only ever touch its own footprint:

- **Sending** is refused unless the recipient address appears in `config/targets.csv`.
- **Reading** is only possible via a thread ID Greenroom itself recorded in Firestore,
  or a `label:greenroom` query. There is no code path that fetches an arbitrary message.
- The Gmail tool wrapper exposes **no** archive, delete, trash, or untrash method at all.
- Labels are only ever *added*, and only to threads Greenroom created.
- `users.watch` is scoped to the `greenroom` label, not `INBOX`.
- Calendar: create-only. The wrapper has no patch or delete method.

## Quick start

Full spin-up-from-a-clean-project instructions land at step 9. For now:

```bash
make setup      # Python 3.12 venv + dependencies
make test       # pytest
make run-local  # dashboard + agent on :8080, dry-run (logs sends, never sends)
make deploy     # Cloud Run via Cloud Build
```

## Architecture

Diagram lands at step 9 (Mermaid + exported PNG).

## Learnings

See `BUILDLOG.md`.
