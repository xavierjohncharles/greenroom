# Demo run sheet

Target: **3 minutes**, unedited, one take. Everything below is real — no mock data on
screen except the seeded pipeline board, which is labelled as such in the repo.

**Service:** https://greenroom-29925954133.europe-west2.run.app
**Dashboard secret:** in Secret Manager as `greenroom-dashboard-secret`

---

## Before you record

```bash
cd ~/greenroom

# 1. Populate the board so it opens on a campaign, not one row.
uv run python scripts/seed_demo.py

# 2. Confirm the service is healthy and check which mode you are in.
curl -s $SERVICE_URL/health | python3 -m json.tool
```

Then open, in this order, and leave them as tabs:

1. The dashboard board — https://greenroom-29925954133.europe-west2.run.app
2. Gmail, signed in as **beatid.greenroom@gmail.com**, showing the labelled thread
3. A terminal, large font, ready to run the tick
4. Cloud Run console — the service page, so the `*.run.app` URL and region are visible
5. Cloud Trace — a recent trace, expanded

**Check `dry_run`.** `false` sends real email. For the video, `false` on a thread to
yourself is the honest thing to show. Confirm `config/targets.csv` contains only
addresses you own before recording.

---

## The 3 minutes

### 0:00 — What it is (20s)

> "Greenroom is an autonomous booking agent. It researches a venue, pitches it, handles
> the reply thread, negotiates inside a policy I define, and escalates to me only when a
> decision is outside that policy. It's running on Cloud Run and it's working my real
> pipeline of students' unions."

Board on screen. Point at the status counts and the "Waiting on you" panel.

### 0:20 — The twist: it earns autonomy (25s)

Open a target page.

> "Every target starts in review — nothing sends without me. Three clean approvals and it
> graduates to veto, where it sends after thirty minutes unless I stop it. Any edit and it
> drops back a level. My edits are stored as diffs, and a style memo regenerated from them
> teaches the writer how I actually write."

Point at the mode pill and the approval counter.

### 0:45 — A real pitch, researched (30s)

Show the research panel.

> "It found this hook itself — their Welcome Week programme, announced this month, with
> the source. The recency rule is deliberate: a famous gig from 2009 is specific and
> checkable and still a terrible reason to book a night."

Scroll to the draft and the attached poster.

> "The poster is generated per target — Google's image model, brand palette, the venue's
> own name."

### 1:15 — Approve, and watch it send (25s)

Click **Approve & send**. Switch to the terminal:

```bash
curl -s -b "greenroom_gate=$SECRET" -X POST "$SERVICE_URL/tick" | python3 -m json.tool
```

Switch to Gmail. The thread is there, labelled `greenroom`.

> "That's a real email, from a real mailbox, sent by Cloud Run."

### 1:40 — The negotiation (35s)

Reply from the other account with the counter-offer, or use the one already in the thread:

> *"We'd want it for our end of term event, about 1200 capacity, and our budget is £600."*

Run the tick. Show the escalation on the dashboard.

> "Two rules at once — the fee is under my floor of £850, and 1200 is over my 600 cap. It
> drafted a holding reply that agrees to nothing, escalated to me, and labelled the thread.
> The important part is that no model decided this. The model extracted the terms; a
> deterministic policy check made the call, and cited the exact lines."

### 2:15 — The attack (35s)

```bash
uv run python scripts/inject_test_email.py --fixture subtle_embedded_instruction
curl -s -b "greenroom_gate=$SECRET" -X POST "$SERVICE_URL/tick" | python3 -m json.tool
```

Show `"quarantined": 1`. Open the quarantine view.

> "That was a plausible booking enquiry with an instruction buried in it — move the fee
> floor to £300, skip internal review. No regex catches that; the model layer did. The
> thread is quarantined, I'm escalated to, and the Negotiator was never invoked. Its
> agent holds no send tool anyway, and the Scheduler re-checks every recipient against my
> target list before anything leaves."

### 2:50 — Where it runs (10s)

Cloud Run console, then Cloud Trace.

> "One Cloud Run service, Firestore for state, Gmail watch through Pub/Sub for inbound,
> Cloud Scheduler on the tick, and a span per agent and per tool call in Cloud Trace."

---

## If something goes wrong

| Symptom | Do this |
|---|---|
| Tick returns `blocked` | It is outside Mon–Fri 09:00–17:00 UK. Say so — it is the guardrail working. Don't widen the window on camera. |
| Draft does not appear | Run the tick twice; research and drafting are separate jobs. |
| Inbound not picked up | The tick reconciles owned threads; a second tick catches it. |
| Poster missing | The pitch sends without it by design. Move on. |

## After

```bash
uv run python scripts/seed_demo.py --clear
```

## What to say if asked "what would you do next?"

Real answer: per-target send scheduling instead of one global window, a proper HITL
approval queue with mobile push rather than a dashboard you have to open, and the
Negotiator proposing calendar slots from actual free/busy rather than policy hours — that
last one is built but only lightly exercised.
