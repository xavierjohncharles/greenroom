# config/

Three files, all safe to commit. **No credentials belong in this directory.**

| File | Who reads it | What it controls |
|---|---|---|
| `brand.yaml` | Writer, Negotiator | Who we are, the pitch, proof points, tone and copy rules. |
| `policy.yaml` | Negotiator, Scheduler | The deal envelope, meeting rules, escalation triggers, send caps, trust dial. |
| `targets.csv` | Researcher, Scheduler | The pipeline. **Also the allow-list: Greenroom refuses to send to any address not in this file.** |

## targets.csv

| Column | Required | Notes |
|---|---|---|
| `organisation` | yes | Display name, e.g. "Goldsmiths Students' Union". |
| `contact_name` | no | Leave blank if unknown; the Writer adapts the greeting. |
| `email` | yes | Must be unique. This doubles as the send allow-list. |
| `venue_notes` | no | Capacity, rooms, existing club nights — feeds the Researcher's hook. |
| `tier` | yes | 1, 2 or 3. Priority order for the send queue. |
| `context` | no | Anything you already know: a warm intro, a past conversation, a name to drop. |

Quote any field containing a comma.

## Editing policy.yaml

Every rule carries a stable `id`. When the Negotiator escalates, it cites that id, and
the dashboard shows you the exact line the counter-offer breached. If you add a rule,
give it an id and it becomes citable for free.
