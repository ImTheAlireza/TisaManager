# Scheduling, retries & restart safety — audit and fixes

Everything listed here is implemented. `68 passed` (`python -m pytest tests`),
up from 31; `tests/test_scheduling.py` is new and covers the flow that
previously had no tests at all.

---

## Part 1 — the 23 scheduling issues

### P0 — data loss / duplicate posts / bypass

**1. Scheduled post stored as `draft` → publishable twice.**
`_finish_schedule` now saves with `delivery_status="scheduled"`, a new state in
`DELIVERY_STATUSES`. `post_detail_keyboard` renders no "publish draft" button
for it, `handle_publish_draft` refuses when `get_active_schedule_for_post`
returns a row, and edit/duplicate/retry are blocked too.
*Tests: `test_finish_schedule_marks_post_scheduled_not_draft`,
`test_scheduled_post_detail_keyboard_has_no_publish_button`.*

**2. Scheduling bypassed approval.**
Checked twice: `handle_schedule_post` refuses to start the wizard for a user
without `approve` when approval is on, and `process_scheduled_posts` re-checks
at fire time, routing the post to `pending_approval` instead of publishing.
Authorisation is also re-verified at fire time, so a demoted author's schedule
is cancelled rather than sent.
*Tests: `test_writer_post_goes_to_approval_at_fire_time`,
`test_deauthorised_author_schedule_is_cancelled`.*

**3. No claiming → duplicate publishes on crash/overlap.**
`claim_schedule()` does `UPDATE ... WHERE id=%s AND status='scheduled'` and
returns `rowcount == 1`; only the winner publishes. `reclaim_stale_schedules()`
returns rows stranded in `processing` past `SCHEDULE_CLAIM_TIMEOUT_SECONDS`,
retrying up to `SCHEDULE_MAX_ATTEMPTS` and then failing them with a
notification. Jobs also run with `max_instances=1, coalesce=True`.
*Tests: `test_only_one_claim_succeeds`, `test_due_query_skips_claimed_rows`,
`test_publish_runs_once_across_two_overlapping_ticks` (two concurrent ticks,
asserts exactly one publish).*

**4. Missed schedules all fired at once.**
`expire_stale_schedules(SCHEDULE_GRACE_SECONDS)` (default 6h) retires anything
older than the window as `expired` and DMs the author.
*Test: `test_overdue_schedule_expires_instead_of_publishing`.*

### P1 — breaks in normal use

**5. Stale buttons crashed with `KeyError`.** Every step now carries its own
data in the callback payload (`schedule_date_2026-08-10`,
`schedule_hour_<date>_<h>`, `schedule_minute_<date>_<h>_<m>`) and
`_require_phase()` verifies the workflow phase before touching state. A stale
button gets an alert and mutates nothing.
*Tests: `test_stale_minute_button_does_not_raise_keyerror`,
`test_stale_hour_button_does_not_hijack_current_post`.*

**6. No error handler.** `app.add_error_handler(on_error)` added; it logs and
always releases the callback so the client never spins.

**7. `except ValueError` swallowed real errors.** New `SchedulePastError`
subclass; only a genuinely past time is reported as such, everything else
propagates to the error handler.
*Tests: `test_schedule_past_error_is_distinct_from_valueerror`,
`test_finish_schedule_raises_typed_error_for_past_time`.*

**8. Callback answered last.** All schedule handlers `await query.answer()`
before doing DB work.

**9. Orphaned schedules.** `delete_post()` now also clears `scheduled_posts`,
`post_deliveries` and `post_versions`; the delete handler cancels a pending
schedule first and refuses while a publish is in flight.

**10. Silent outcomes, no retry.** Every scheduled publish DMs its author on
success, partial failure, hard failure, expiry and abandonment — plus the whole
retry system below.

**11. Empty selection meant "all channels".** `_resolve_targets()` distinguishes
`[]` (no channels) from a missing column (legacy row → all).
*Tests: `test_empty_selection_does_not_broadcast`,
`test_missing_target_column_falls_back_to_all`.*

**12. Job raced DB init.** Periodic jobs are registered *inside*
`initialize_database` after `init_db()` returns, not on a 10-second guess.

**13. `idx_scheduled_due` missing on existing installs.** Explicit
`information_schema.statistics` migration added, matching `post_history`.

**14. Mixed time bases.** All `scheduled_posts` / `post_deliveries` timestamps
are naive UTC written with `UTC_TIMESTAMP()`. Conversion happens only at the
edges via `local_to_utc_naive` / `utc_naive_to_local` / `format_local`.
*Tests: `test_local_to_utc_and_back_roundtrips`,
`test_naive_input_is_treated_as_local`.*

### P2 — UX & maintainability

**15. Could not list/edit/cancel a schedule.** New `handlers/schedules.py` plus
a "🕒 پست‌های زمان‌بندی‌شده" main-menu entry: list, detail, reschedule, cancel,
publish-now. Cancel and reschedule refuse rows already in `processing`.
*Tests: `ScheduleManagementTests` (4 cases).*

**16/17/20. Date & time pickers.** Seven days instead of today/tomorrow; past
hours hidden for today; 5-minute granularity with past minutes filtered.
*Tests: `KeyboardTests` (4 cases).*

**18.** Dead `state["schedule_hour"]` — now genuinely used by the minute step.

**19.** Manual entry is documented in the prompt itself. **Jalali dates are now
implemented** — see the section below.

**21.** `Asia/Tehran` no longer hardcoded: `DISPLAY_TIMEZONE` in `config.py`,
consumed everywhere including the nightly-backup job.

**22.** The confirmation now shows the schedule id, and the detail screen is
reachable from the post.

**23.** Zero tests → `tests/test_scheduling.py`.

---

## Part 2 — the two features you asked me to check

### "Restart won't break someone's job"

**This was never implemented.** The only trace was a one-line `error_handler.md`
describing the intent: *"restart detects in-flight posts, blocks writers with
msg, saves to DB queue, resumes after."* `handle_do_restart` went straight from
"are you sudo?" to `supervisorctl restart` in a background thread. So:

- every `user_states` entry died with the process — anyone mid-compose silently
  lost their work and their next message was ignored;
- nobody was told the bot was going down;
- a restart during a broadcast could abandon it half-sent, with the
  `scheduled_posts` row still `scheduled` → re-sent later to channels that had
  already received it.

Built now:

- **`workflow_sessions` table** mirrors interactive state; `persist_state()` is
  called at every step of composing, channel selection and the schedule wizard.
- **`restore_workflow_states()`** runs after `init_db` and reloads unexpired
  sessions. `created_at` is rebased (monotonic clocks are meaningless across
  processes) and an `awaiting_media_group` state is downgraded to
  `awaiting_post`, because the album debounce job died with the old process.
- **`handle_do_restart` now drains**: flushes all states, DMs every active user
  "your work is saved, please wait", then waits up to
  `RESTART_DRAIN_TIMEOUT_SECONDS` for in-flight publishes (tracked in
  `_inflight_publishes`) before restarting — and warns if it has to give up.
- **`notify_online`** releases the users it asked to wait.
*Tests: `WorkflowPersistenceTests` (4), `InflightTrackingTests` (2).*

### "Retry in 1/3/6 hours with notification"

**Also not implemented.** No retry table, no scheduling of retries, no
`timedelta(hours=...)` anywhere. The only retry was the manual "🔁 ارسال مجدد"
button, and `get_pool`'s 3-attempt connection loop. Worse, the old failure path
recorded only *counts* (`{"telegram_failed": 2}`) — no record of *which*
channels failed, so a retry was impossible even in principle. And a Bale
response with `ok: false` was silently counted as a success.

Built now:

- **`post_deliveries`** — one row per (post, channel) with `status`, `error`,
  `attempts`, `next_retry_at`.
- **`_next_retry_at()`** implements the 1h → 3h → 6h ladder from
  `RETRY_DELAYS_HOURS` and returns `None` when exhausted.
- **`process_delivery_retries`** (every 5 min) claims due rows atomically and
  re-sends **only the failed channels**, preserving the message ids of channels
  that already succeeded. Grouped by `(post, attempt)` so channels on different
  rungs aren't merged.
- **Notifications** at every transition: partial failure with next-attempt time,
  retry success, and final give-up pointing at the manual button.
- Bale `ok: false` is now correctly treated as a failure.
- Failed channels and their next retry time are shown in the post detail view;
  `/stats` gained "🔁 در صف تلاش مجدد".
*Tests: `RetryScheduleTests` (2), `RetryTargetingTests` (2),
`RetryLadderGroupingTests`, `PartialRetryPreservationTests`.*

---

## Deployment notes

- **Migrations are automatic** on next start (`init_db`): new columns on
  `scheduled_posts`, the missing index, and three new tables. Existing rows are
  untouched.
- **Old buttons still on users' screens** (`schedule_date_today` etc.) no longer
  match any pattern. Rather than leave them spinning, a catch-all handler
  answers them with "this button is from the previous version".
- **New env knobs** (all optional, defaults in `config.py`): `DISPLAY_TIMEZONE`,
  `SCHEDULE_GRACE_SECONDS`, `SCHEDULE_CLAIM_TIMEOUT_SECONDS`,
  `SCHEDULE_MAX_ATTEMPTS`, `RETRY_DELAYS_HOURS`, `WORKFLOW_TTL_SECONDS`,
  `RESTART_DRAIN_TIMEOUT_SECONDS`.
- **Not done / worth considering later:** per-user timezones and recurring
  schedules.


---

## Part 3 — Jalali (Persian) calendar

The UI was entirely Persian but every date was Gregorian with Latin digits.
Dates are now shown on the Solar Hijri calendar by default.

**Storage is untouched.** `run_at`, `created_at` and every other column remain
Gregorian UTC, and callback payloads remain ISO Gregorian
(`schedule_date_2026-08-06`). Only labels, prompts and parsing changed — so
existing schedules keep firing at exactly the same instant, and no migration is
required for this part.

### What changed

- **`jalali.py`** — conversion, month/weekday names, digit translation and
  parsing. Written in-house because the project deliberately avoids third-party
  dependencies (`bale_client` speaks HTTP over `urllib` for the same reason);
  the algorithm is closed-form integer arithmetic with no lookup data.
- **Display** — `format_local`, plus new `format_local_date`, `format_clock`
  and `date_example` helpers, all honouring a single `USE_JALALI` switch.
  Persian-Indic digits throughout (`۱۴۰۵-۰۵-۱۲ ۱۴:۳۰`, `۱۲ مرداد ۱۴۰۵`).
- **Pickers** — quick-pick buttons are labelled `پنجشنبه ۱۵ مرداد`, and a new
  **📅 انتخاب از تقویم** opens a full Jalali month grid: Saturday-first columns,
  today marked 🔸, past days rendered as inert dots, month paging that refuses
  to go back into a fully past month.
- **Input** — `parse_user_datetime` accepts Jalali (`۱۴۰۵-۰۵-۱۵ ۱۴:۳۰`,
  `1405/5/15 14:30`) and still accepts Gregorian, disambiguated by year: `>=1700`
  can only be Gregorian, so no guessing. Persian *and* Arabic-Indic digits are
  normalised, since phone keyboards emit both. Two-digit years are rejected
  rather than assumed — silently turning `05` into `1405` would schedule to the
  wrong day.
- **Settings toggle** — «📅 تقویم نمایش تاریخ» flips between Persian and
  Gregorian at runtime, persisted in `bot_settings` and reloaded at startup.
  `USE_JALALI=0` sets the default for a fresh install.

### Verification

- Every day from **1990-01-01 to 2100-12-31** (40,542 days) converts identically
  to the `jdatetime` reference library, and round-trips back exactly. Leap years
  agree across 1300-1499; month lengths agree across 1390-1459.
- **4,382 calendar cells** across 144 months were checked to sit in the column
  matching their real weekday — an off-by-one here would silently schedule posts
  to the wrong day.
- An end-to-end walk confirms tapping «۱۵» in Mordad 1405 with 14:30 stores
  `2026-08-06 11:00` UTC (Tehran is UTC+3:30) and renders back as
  `۱۴۰۵-۰۵-۱۵ ۱۴:۳۰`.

`tests/test_jalali.py` adds 46 cases. Full suite: **115 passed**.

Note: `jdatetime` is used *only* by the test cross-check and is skipped when not
installed — it is not a runtime dependency and is not in `requirements.txt`.

---

## Part 4 — timestamp bug (reported twice)

**Report 1:** history showed `16:08` when the clock read `17:43`.
**Report 2:** history showed `11:06` when the clock read `12:37`.

### Diagnosis

The two reports together were decisive. The gaps (95 and 91 minutes) differ by
the few minutes that had elapsed since each post was made, so the *constant*
component is 90 minutes — and 90 minutes is exactly Tehran (UTC+3:30) minus
**UTC+2**. Both displayed values equal the raw wall clock of a CEST database
server, with no sign of partial correction.

### Two real bugs

**A. `TIMESTAMP` columns were read in the server's timezone.** The pool never
pinned a session timezone, so MySQL returned `created_at` as a server-local
wall clock while the code treated it as UTC. Fixed by
`init_command="SET time_zone = '+00:00'"`.

**B. `created_at` was rendered without conversion.** `handlers/history.py` and
`keyboards.py` formatted the raw value instead of calling `format_local` —
which is also why the date used Persian digits while the clock stayed Latin.
All display now routes through `format_local` / the new `format_local_short`.

### A third bug: my own "fix" was wrong and would have corrupted data

The previous round added a migration that rebased legacy rows with `DATE_ADD`.
That was based on a misreading of MySQL semantics. From the manual:

> "MySQL converts `TIMESTAMP` values from the current time zone to UTC for
> storage, and back from UTC to the current time zone for retrieval. (This does
> not occur for other types such as `DATETIME`.)"

`TIMESTAMP` is stored as a **UTC epoch** and converted on write *and* read. The
stored instant was therefore always correct — the old bug was purely in
interpretation. **Pinning the session fixes existing rows on its own**, and
subtracting two hours from an already-correct epoch is corruption, not repair.
Simulated: pin-only yields the expected `۱۲:۳۷`; the migration would have
produced `۱۰:۳۷`.

The migration has been removed, with a comment explaining why, and a test
asserts it is not reintroduced. It never ran against the database, so no data
was harmed. `DATETIME` columns are unaffected by all of this, which is why the
scheduling tables write `UTC_TIMESTAMP()` explicitly.

### Hardening

`_assert_session_is_utc()` runs at pool creation and raises if a session is not
on UTC, rather than letting a silent driver failure reintroduce the bug.

### Why it appeared unfixed after the previous push

The session pin only applies to connections opened after a restart. Until the
bot restarts on the new code, every read still returns server-local time.

10 regression tests, verified to fail when the pin is removed.
Full suite: **128 passed**.
