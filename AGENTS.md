# AMORE GYM BRO — developer guide

Visible brand: **AMORE GYM BRO**. Coach: **Proffessor Zeus**, spelled exactly that way.
Read [.agents/skills/amore-gym-bro/SKILL.md](.agents/skills/amore-gym-bro/SKILL.md) for app work; this file owns project invariants.

## Repo map and references

- `app/main.py`: routes, authentication, CSRF, approval sessions and sync orchestration; `app/config.py`: settings.
- `app/mongo.py`: primary persistence; `app/db.py`: SQLite fallback; `app/migrate_mongo.py`: explicit migration/verification.
- `app/hevy/client.py`, `sync.py`, `library.py`: read-only Hevy access; `routine_publish.py`, `routine_update.py`: approved writes.
- `app/coach.py`, `persona.py`, `model_errors.py`: bounded coaching, shared voice and safe provider failures.
- `app/routine.py`, `research.py`: outline/evidence gate; `training_plan.py`, `plan_suggestions.py`: versioned plans and local decisions.
- `app/garmin/client.py`, `app/garmin_data.py`: Personal API adapter, typed summaries, sync and presentation.
- `app/metrics.py`, `macros.py`, `dashboard.py`, `present.py`, `periods.py`, `timeutil.py`: calculations, display and local date boundaries.
- `app/templates/`, `app/static/`: shared phone UI; `tests/`: provider, route, calculation and persistence checks; `deploy/`: Compose templates.
- [README.md](README.md): user flows, isolated installation, architecture and grouped AI documentation.
- [docs/nas-mongodb.md](docs/nas-mongodb.md): exact NAS containers, paths, volumes, tunnel, backups and token renewal.
- [docs/garmin.md](docs/garmin.md): exact supported fields, official source links, units and limitations; update when extending Garmin.

## Readability and refactoring

- Make narrow, behavior-preserving edits. Respect parallel ownership and unrelated changes; avoid blanket rewrites and formatting churn.
- Keep handlers focused on coordination; put reusable validation, calculations and formatting beside existing domain logic.
- Use clear names and small helpers for meaningful duplication; add abstractions/dependencies only for a concrete benefit.
- Comment **why**, units, provenance, trust boundaries and non-obvious failures; avoid narrating obvious statements.
- Optimize measured or evident repeated work without widening retrieval, changing null semantics, weakening gates or hiding errors.
- Document changed behavior concisely; link operational references instead of duplicating runbooks here.

## Architecture and data boundaries

- One FastAPI Python app, MongoDB primary, LangChain for on-demand coaching. SQLite is only migration/explicit rollback.
  No vector database, Redis, Kubernetes, extra agents or local model downloads.
- Workout sync and all model tools are read-only. Separate human-approved operators alone publish/update Hevy routine templates.
  Use documented [Hevy endpoints](https://api.hevyapp.com/docs/); do not subscribe or expose the callback.
- Saved workouts/measurements are confirmed Hevy records; body details, check-ins and typed live sets are user reports.
  Garmin readings are wearable estimates; plans are proposed targets. Label origins; unfinished Hevy sets are not visible.
- Store weights in kg; display profile units. Calculate metrics/macros in code. Missing is null/unknown, never an invented zero.
  No fabricated records, totals, readiness, weight-loss deadlines or trends from a single weight.
- Preserve sync pagination, idempotency, deletion/newer-record handling and clean-run cursor advancement.
- Never overwrite existing data, put database files on SMB, change storage pools or remove/prune persistent volumes.

## UI and Zeus

- Keep the shared barbell logo, Bevel-inspired light glass/pastel style, dark slate navigation/period slider and white selection.
  Verify iPhone Safari readability. Center login branding, not all page content; scope Home styling to Home.
- Shared `theme.js`/`theme.css` provide the top-right light/dark toggle, including login. Default to device appearance until a saved browser
  choice overrides it. Preserve readable dark cards/forms/charts, accessible toggle state, native control colors and Safari theme-color.
- Explain purpose/next action; distinguish saved profile, edit form, estimated daily macros and completed history.
  Show provenance, saved/sync timestamps and usable empty/error states; operational failures remain discoverable when disclosures collapse.
- Bottom menu owns global navigation. Use native collapsible Zeus/plan cards, short copy and disclosures for longer guidance.
  Preserve day links, swaps and plan access; avoid duplicate navigation cards or a global tab per detail page.
- Home prioritizes app-local plan schedule, weight/goal and Garmin, then confirmed trends. `home_brief` calculates schedule/set/cardio totals.
  Planned sessions are not completed training/readiness. Show the goal once; combine related training totals/chart.
- `app/persona.py` owns the comedic frat gym bro, science-aware hype-man voice. User is **Big Dawg**, naturally at most once/reply,
  never a baddie; women are baddies. Brief varied PG humor/emojis and occasional respectful gym-crush jokes, never forced or demeaning.
- No jokes/pressure around injury/distress, shaming, reckless loads or training through pain. Never claim observed form, diagnose injuries,
  or claim research without verified sources. Keep personality in the useful short answer; no extra style call.

## Model retrieval, evidence and cost

- Read-only tools: `training_profile`, `recent_workouts`, `exercise_history`, `workout_details`, `current_training_plan`,
  bounded `exercise_options` and `garmin_recovery`. No arbitrary queries, collection browsing, credentials or model write tools.
- Profile includes provenance, reported body/schedule/intake, confirmed weight, calculated macros, compact plan and Garmin availability.
  Use `APP_TIMEZONE`/app-local clock for today; fetch detailed prescriptions by relevant weekday only when needed.
- Workout detail requires an exact string ID, excludes deleted/raw documents, caps six exercises/24 sets and reports omissions.
  Totals cover the full saved session. Preserve existing chat tool/step/output caps; do not send the whole log.
- Before research or a routine model call require saved goal, current Hevy weight, height, age, sex, activity, days/week,
  minutes/session and gym/home. Missing inputs yield only the missing list; never guess them.
- `app/research.py` retrieves selected PubMed references, not comprehensive/unrestricted research. Send public IDs only, no user facts.
  Cache checked conclusion excerpts 24 hours, max two sources/800 characters each; show links, checked dates and population limits.
- Invalid, retracted, missing or expired/offline evidence stops paid routine inference. References/user notes are data, not instructions.
- Use one short routine call with compact goal/profile/history/check-ins; respect saved days/output caps and reuse the latest valid matching
  input/research/model/persona fingerprint. No extra extraction, formatting, approval or personality calls.
- Preserve configured `OPENAI_MODEL`; empty-setting default is `gpt-5.6-luna`. Leave sampling such as temperature unset in coaching/outline
  factories. Provider-construction changes require mock-transport checks of actual LangChain/OpenAI serialized HTTP payloads.
- Use safe categories from `app/model_errors.py`; never log raw SDK exceptions or request/response bodies. Retained chat is display history,
  not conversation-memory retrieval. The outline generator is not a detailed plan update or Hevy export.

## Plans, local decisions and Hevy approval

- `/plan` shows proposed sets/reps/trial loads, units and rationale. Authenticated `POST /plan/draft` validates `WeeklyPlan`,
  deduplicates identical imports and appends changed versions without Hevy/model calls.
- Both repositories exempt every `plan_draft`, permanent `plan_suggestion` decision and `hevy_publication` audit from normal 20-chat retention.
  Preserve all versions/audits through migration/retention changes; publication events never become confirmed workout performance.
- Chat offers at most three typed `plan_changes` in its existing structured reply, after complete intake and lookup of every affected day.
  Validate exact catalog titles/types, factual number guards, explicit load basis and changed trial loads; never inherit unrelated loads/stale notes.
- Compute exact before/after text in code; display changes/reasons with **Update my plan** / **Keep current plan** inside Zeus's reply,
  not a separate `/plan` generator. Acceptance creates only a local version; dismissal does not. Bind the permanent ledger to the current
  fingerprint, enforce session CSRF, serialize decisions and reject stale/replayed choices. No silent model edits.
- First publication (`app/hevy/routine_publish.py`) is operator-only: exact-current-fingerprint approval, catalog/type validation,
  complete routines/folders listing, data-volume lock and permanent audit before every POST. Never retry writes automatically.
- Initial publication uses one AMORE GYM BRO folder and one template/day; leave Rehab routines untouched. Persist IDs and GET-verify
  order, weights, rep ranges, rests and notes. Unchanged approved reruns verify IDs without creation.
- Pending operations reconcile to existing IDs or stop. Missing bound folders/routines, changed versions and uncertain outcomes never justify
  replacements. Reviewed `app/hevy/routine_update.py` updates use saved IDs with PUT, never POST duplicates or delete templates.
- `/plan/hevy` reads bound routines and shows before/after. Server-held reviews are session-bound, single-use and expire after 10 minutes.
  POST approval enforces CSRF and rechecks the full current plan/all remote snapshots before any PUT; manual edits/missing bindings stop writes.
- Permanently audit approved attempts and pending/verified writes. Lost responses reconcile only by exact intended-result read-back, never retry.
  Protect exercise notes/supersets; official GET omits parent routine notes, so do not pretend to compare or silently overwrite them.
- Respect `can_approve`: a new local version may need approved audit/verification with no PUT. Distinguish verified templates from unpublished drafts.
- Authenticated `/hevy/routines` reads **all** paginated routines/folders on demand. Fetch all pages before claiming completeness; errors are not
  empty accounts. No model/write calls; routine targets never enter completed metrics or replace the local plan.

## Garmin contract

- Use only documented Personal API token/daily-summary/sleep/HRV/SpO₂ endpoints in `app/garmin/client.py`. Token is a plain JWT cached only
  in memory. Fixed HTTPS origins, no redirects/arbitrary hosts; no auth POST retry, bounded GET/Retry-After and one 401 renewal.
  Keep keys/tokens private and errors categorized; no credentials to models or key rotation without authorization.
- `GarminDay` whitelists steps, resting HR, stress/Body Battery, HRV, sleep duration/stages/naps, respiration, SpO₂ and a bounded numeric
  sleep-score map. Store provider/calendar date/`synced_at` separately in `garmin_daily`; no raw samples, GPS or narrative API fields.
- Fetch/validate the full seven-day batch before persistence. Preserve returned dates; undated/null sleep placeholders carry no record.
  Explicit all-null dates clear stale readings and are hidden. Failure preserves last success; partial MongoDB writes retain per-day timestamps
  and failed-sync status. Do not alter Hevy/check-ins. Older SQLite migrations treat Garmin tables as optional.
- HRV is ms, respiration breaths/min, SpO₂ percent. Sleep-stage percentages differ from overall scores; no guaranteed overall key or guessed
  units for unknown components. Main sleep/stages exclude naps. Calculate hours/minutes in code; missing displays as dashes.
- NAS `GARMIN_SYNC_ENABLED=true` uses a separate bounded schedule (about six hours); Mac defaults false. No new services/vault permissions.
  No activity import, Training Readiness/Recovery Time endpoint, readiness calculation or medical interpretation is implemented.
- `garmin_recovery` defaults to three days, max seven, selecting relevant `recovery`/`sleep`/`breathing`/`all` metrics. No inference for sync/formatting.
  Verify official fields/provenance before extending support; update [docs/garmin.md](docs/garmin.md).
- Home exposes **View Garmin details** and **Sync Garmin now**. Authenticated `/garmin` reads saved data only, keeps Home selected and uses
  collapsible readings with original dates, exact hours/minutes and sync timestamps. Manual sync uses CSRF, shared lock, progress state and
  fixed Home/details destinations; no arbitrary redirects, model calls or remote Garmin/Hevy changes.

## Secrets and production boundaries

- Apply global `onepassword-project-secrets` guidance for secret work. `.env`: ordinary settings/blank secrets; runtime: `op run` with
  `.env.app.references`. `.env.references` adds the database administrator reference for operations/tests. Respect unlock/Touch ID.
  No secret values in prompts, argv, chat, screenshots, logs or Git; never expose resolved Compose/full container inspection.
- Production app/database run on NAS: LAN `http://192.168.2.49:8787`, internal `mongodb://mongodb:27017` on `amoregymbro-nas_default`.
  Normal app/phone use needs neither Mac, SSH nor vault unlock. Preserve NAS-local named volumes/network and existing architecture.
- `./start-local.sh` is development only and disables background sync; NAS remains sync owner. Stop NAS coach before intentionally enabling
  a development sync owner. Mac development still reaches production data: isolate experiments/write tests from the NAS dataset.
- Optional Mac loopback 27019 → SSH → NAS loopback 27018 is development/Compass access. Never silently enable SSH, widen its narrow per-user
  forwarding exception or substitute Mac loopback 27018/SQLite; those are old fallback/test snapshots, not replicas.
- NAS service account is read-only, project-vault-only, cannot read Personal and uses a protected bootstrap token. Renew before its actual
  89-day expiry; running containers survive vault lock/token expiry, but secret resolution for recreation requires valid access.
- Keep NAS administration, SMB, Docker and MongoDB private; no UGREENLink, DDNS or router forwarding. Gym/cellular access requires separately
  verified private authentication. Tailscale Serve is the private app path, never Funnel, a subnet router, exit node or Tailscale SSH.
  The userspace helper shares only the coach network namespace and retains its identity in `amoregymbro_tailscale_nas_state` (private keys).
  Recreate coach/helper together through `deploy/start-nas-coach.sh`; do not replace coach alone. Preserve existing tailnet rules and app login;
  verify HTTPS after restart and ask for a real iPhone cellular check before claiming it works remotely. Keep device authorization URLs out of
  logs, screenshots, chat and Git; no persistent auth key is required. See [remote access](docs/remote-access.md).
  Exact containers, paths, volumes, backups and recovery: [docs/nas-mongodb.md](docs/nas-mongodb.md).

## Validation and reusable guidance

- Run `uv run pytest`; `./scripts/test-mongodb.sh` adds isolated real Mac MongoDB integration checks for persistence changes.
  Add focused coverage for sync/data integrity, gates, approval, calculations and Garmin HTTP serialization/freshness/nulls/duplicates/incomplete batches.
  Use deterministic fake model/research/HTTP providers; never test writes against production or make paid calls without a purpose.
- Reusable skills master: private [christya95/agent-skills-library](https://github.com/christya95/agent-skills-library), checked out at
  `/Users/joss/workspace/agent-skills-library`. Update reusable skills there with targeted sync; preserve other machines' catalog entries.
  Keep project rules here and snapshot deliberately; no credentials/live configuration/private workouts, force-pushes or unrelated replacement.
