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
