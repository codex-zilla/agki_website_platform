# AGKI Appointment System — Implementation Plan & Tracker

**Design reference:** [appointment-system-redesign.md](appointment-system-redesign.md)
**Covers:** `appointments/` (public booking) and `doctorDashboard/` (staff console)
**Status legend:** 🔲 Not started · 🟡 In progress · ✅ Done · ⛔ Blocked · ⏭️ Skipped (see notes)

How to use this doc: update the **Status** cell as work happens, and always leave a line in **Notes / Remarks** when a step is closed — even a one-liner ("done, no issues"). A blank notes cell on a ✅ step is a smell; a step marked ⛔ must have a note explaining what's blocking it. Treat this file as the log a reviewer (or future-you) reads to understand *why* something was done a certain way, not just *that* it was done.

**This project is different from the CMS one in a way that shapes every phase below:** the CMS built something new alongside a working site. This one touches a system that is **live, holds patient data, and is currently exploitable by anonymous visitors**. Phase 1 exists to close those holes before anything else, and it deliberately depends on nothing else in this plan.

---

## 0. Pre-flight / safety checklist

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 0.1 | Decide the branch point — this work depends on the CMS branch | 🔲 | **Needs a decision before any code.** `feature/website-cms` is **20 commits ahead of `main` and not merged**. This project needs `cms.SiteSettings` (§3.1 branding) and the `Content Editors` group (§4.3), both of which only exist on that branch. Options: (a) merge CMS to `main` first — cleanest, but that means finishing CMS Phase 10 rollout; (b) branch `feature/appointments-redesign` **off `feature/website-cms`** and accept a long-lived stacked branch; (c) do Phase 1 (security only) off `main`, since it needs nothing from CMS. **Recommendation: (c) then (a) then the rest** — the security fixes shouldn't wait on a CMS rollout. |
| 0.2 | Back up production `db.sqlite3` + `media/` before any migration | 🔲 | Non-negotiable here in a way it wasn't for the CMS: Phase 2 changes `Appointment.slot` to `OneToOneField` and drops `AppointmentApproval`. Those are not trivially reversible on real bookings. |
| 0.3 | Audit production data for the three known migration blockers | 🔲 | All three are **confirmed present in local dev** and must be checked against prod before Phase 2: (1) duplicate `AvailableSlot` rows for the same doctor/date/time — blocks `unique_together`; (2) more than one `Appointment` per slot — blocks the `OneToOneField` (the current code allows this, see design §10 #6); (3) free-text values in `visitType`/`reasonVisit`/`gender` that don't map to the new choice lists. Write the audit as a management command so it can be re-run on prod, not a one-off shell session. |
| 0.4 | Confirm `Profile` backfill scope | 🔲 | **5 of 10 local accounts have no `Profile` row.** Design §5.3 — a doctor with no Profile is invisible to booking. Check the same ratio on prod. |
| 0.5 | Decide the rate-limiting approach (§8.2) | 🔲 | Nothing in `requirements.txt` provides it today. Either add `django-ratelimit` (new dependency, needs AGKI approval like the CMS packages did) or hand-roll with Django's cache framework. **Note the cache is currently per-process `LocMemCache`** (see CMS plan Phase 8) — a per-process rate limit is weaker than it looks under multiple Passenger workers. Worth deciding alongside a shared cache backend. |
| 0.6 | Confirm cron is available on the production host | 🔲 | **Phase 6 (expiry) does not work without it.** cPanel normally provides cron, but this must be confirmed and the entry actually created — a missing cron job is silent, and without it every unanswered booking holds its slot forever. |
| 0.7 | Confirm the two settings values with AGKI in writing | 🔲 | `MINIMUM_BOOKING_NOTICE = 2 hours`, `CONFIRMATION_CUTOFF = 30 minutes` (design decisions C/D). Both go on `AppointmentSettings`, so they are changeable later — but the invariant (notice > cutoff) is enforced in `clean()`. |
| 0.8 | Freeze `ecommerce/` | 🔲 | Per decision 1: its own redesign comes later. This project touches it in exactly one way — deleting `doctorDashboard.views.delete_item`, which points *at* it. Nothing inside `ecommerce/` changes. |

**Phase gate:** the branch question (0.1) is answered, production data is audited for the three blockers, and backups exist.

---

## Phase 1 — Security hardening (ships first, alone, depends on nothing)

Design §11 step 0. Every item here is currently exploitable by an **anonymous** visitor, verified by reproduction against a throwaway database. None of it needs the state machine, the notification layer, or a design sign-off. **Do not let this phase wait on the rest of the plan.**

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 1.1 | Guard every unguarded `.image.url` in both apps | 🔲 | Do this **first** — it is a prerequisite for testing anything else. `doctorDashboard/doctor_base.html` (lines 764, 843) renders `{{ user.image.url }}` unguarded, and 5 of 10 accounts have no image, so those accounts get a **500 on every dashboard page**. `doctors.html` already does this correctly (`{% if doctor.image %}`) — that pattern gets applied everywhere. Fixes 5 crash sites across both apps. |
| 1.2 | `@login_required` on every `doctorDashboard` view | 🔲 | Design §4.2 point 1. Note the current failure mode is *not* a 403: four URLs (`/doctors/`, `/ecommerce_items/`, `/schedule/`, `/base/`) return a clean **200** to anonymous users, and the rest return **500** because `AnonymousUser` hits a `TypeError` in the queryset filter. That crash is incidental, not protection. |
| 1.3 | `@role_required` decorator + per-view application | 🔲 | §4.2 point 2. `doctors()` / `activate_deactivate_doctor()` are `admin`-only. |
| 1.4 | **Delete `delete_item`** (view, URL, template button) | 🔲 | §4.4. Reproduced: an **anonymous GET** deletes an `ecommerce.Item` — no login, no POST, no CSRF. Deleted rather than ported; product deletion moves to the Django admin, which has confirmation, permissions and a `LogEntry` trail. A test asserts the route no longer reverses. |
| 1.5 | `ecommerce_items` → `is_superuser`-only, read-only | 🔲 | §4.4. Stricter than any role. Sidebar link renders only for superusers. |
| 1.6 | Fix the IDOR on patient records | 🔲 | §8.1. Reproduced: a stranger reads full patient PII (name, DOB, phone, email, symptoms) at `/appointments/detail/1/`. URL keys off `bookingID`, direct access additionally requires a signed appointment-scoped session marker, bare GET redirects to the lookup form. |
| 1.7 | `booking_id_lookup` requires a second verification field | 🔲 | §8.1. `bookingID` alone is not a secret (100k combinations/year). Add phone-or-DOB, with a generic error that doesn't reveal which field was wrong. |
| 1.8 | Rate-limit the lookup endpoint | 🔲 | §8.2, approach per 0.5. |
| 1.9 | Fix `post_login_redirect` for `role=admin` | 🔲 | §4.1.2 — a live mismatch: `kulwa_cherehani` (`role='admin'`, `is_staff`, not superuser) currently lands on `/admin/`, never on the console she administers. Route both operational roles to the dashboard before the `is_staff` check. Add a `role=admin` row to the existing `PostLoginRedirectTests` matrix. |
| 1.10 | Enforce `is_approved` at login | 🔲 | §4.2 point 4 / cross-cutting #17. Today `signin()` only checks `is_active`, so the admin "approval" toggle gates nothing. **Check prod first**: any existing staff account with `is_approved=False` will be locked out the moment this ships. |
| 1.11 | Tests for every item above | 🔲 | Enumerate URLs from the URLconf rather than a hand-written list, so a new view can't be added without a decision. |

**Phase gate:** an anonymous visitor can reach nothing they shouldn't, no destructive endpoint responds to a GET, and patient PII is not readable by guessing a number. Verified by test, not inspection.

---

## Phase 2 — Data model & migrations

Design §5 and §11 steps 1-6. **Run 0.3's audit before starting.**

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 2.1 | `AppointmentSettings` singleton | 🔲 | §4.3. `MINIMUM_BOOKING_NOTICE`, `CONFIRMATION_CUTOFF`, cancellation cut-off. Reuse the CMS's `SingletonModel` pattern. `clean()` enforces notice > cutoff — the combination that would otherwise make every booking expire on creation. **Registered superuser-only**, not granted to `Content Editors`. |
| 2.2 | `Specialty` + `Profile.specialties` (M2M) | 🔲 | §5.5. A model rather than `TextChoices` so AGKI can add one without a deploy. Superuser-managed. |
| 2.3 | Migrate `expertise` → `specialties` | 🔲 | **Report unmappable values, don't guess.** The one live value is `"Cardiograph"` — an ECG machine, not a specialty. A human decides it means Cardiology. Keep `expertise` until the mapping is confirmed, then drop it in a later migration. |
| 2.4 | `Appointment.status` + `Mode` + the new timestamp/URL fields | 🔲 | §5. `status` defaults to `PENDING_TIME`; `appointmentMode` is **blank until round 2** (§0.1) and is not collected at booking. Adds `meeting_url`, `time_confirmed_at`, `mode_confirmed_at`, `expired_at`, `cancelled_at`, `cancellation_reason`. |
| 2.5 | `AppointmentTransition` audit model | 🔲 | §5. Append-only. `actor=None` means the system (expiry) or the patient. This is what makes "which doctor let this expire" a query. |
| 2.6 | Backfill `status` from `AppointmentApproval`, seed transitions | 🔲 | §11 step 2. `is_approved=True` → `CONFIRMED`, else `PENDING_TIME`. Each existing approval becomes a seed transition so history survives. **Backfill is lossy in one direction and that's fine** — historic appointments have no mode; do not invent one. |
| 2.7 | Retire `AppointmentApproval` (later migration) | 🔲 | Keep the table read-only for **one release** after 2.6 before dropping — cheap insurance against an unrecoverable backfill mistake. |
| 2.8 | `AvailableSlot.unique_together` | 🔲 | ⚠️ **Duplicates confirmed in local dev.** Dedupe first, guided by 0.3's audit. Do not let the constraint fail on dirty data. |
| 2.9 | `Appointment.slot` → `OneToOneField`, `on_delete=PROTECT` | 🔲 | ⚠️ **Riskiest migration in the project.** Two appointments can already share a slot (design §10 #6), so prod may violate this constraint *today* and need manual resolution first. `PROTECT` also fixes a separate latent bug: `CASCADE` meant deleting an availability slot silently deleted the patient's appointment. |
| 2.10 | `choices` on `visitType` / `reasonVisit` / `gender` | 🔲 | §5.1. Audit and map existing free-text values first — the `role='4491'` precedent says assume dirty data. Note `home_collection` moves **out** of reason-for-visit (it's a mode now, §0.1). |
| 2.11 | Drop `Appointment.age` | 🔲 | Dead column, never collected. |
| 2.12 | Reshape `Rating` | 🔲 | §5.4. Add `appointment` OneToOne, rename `doctor` → `provider`. Built now even though nothing reads it, so the table is ready when the feature is (decision 7). |
| 2.13 | `Profile` auto-creation signal + backfill | 🔲 | §5.3. 5 of 10 local accounts affected. Doctor visibility must follow from `role`, never from whether someone once opened a page. |
| 2.14 | `NotificationLog` | 🔲 | §9.1. |

**Phase gate:** every migration applied against a **copy of production data**, not just a clean dev DB. The `OneToOneField` and `unique_together` steps in particular are meaningless as tests on empty tables.

---

## Phase 3 — Notification layer

Design §9.1. Built before the flow, because the flow is unusable without it.

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 3.1 | `notify(event, appointment, to=...)` event API | 🔲 | Call sites emit **events, never messages**. That coupling is exactly what would force a rewrite when SMS arrives (decision 3). |
| 3.2 | `EmailChannel` | 🔲 | Reuses existing SMTP config, as the CMS contact form already does. |
| 3.3 | `SmsChannel` stub | 🔲 | Raises `NotImplementedError`. Present so the seam is **real and tested** rather than imagined. Recipient data already exists (`Appointment.phone`, `CustomUser.phone`), so SMS needs an integration, not new data capture. |
| 3.4 | `NotificationLog` writes | 🔲 | "Did the patient ever get told?" is an operational question; "check the mail server logs" is not an answer. |
| 3.5 | Message templates for all 8 events | 🔲 | `booked`, `time_confirmed`, `time_declined`, `mode_proposed`, `mode_confirmed`, `cancelled`, `expired`, `completed`. **Every patient-facing one carries a link back to their appointment** — that's what makes the two-round flow work without patient accounts (§0.1). |
| 3.6 | Copy discipline check | 🔲 | No template mentions SMS until `SmsChannel` is real. The old templates promised SMS reminders with no integration anywhere. |

**Phase gate:** every event sends a real email in dev (console backend), is logged, and no call site imports `send_mail` directly.

---

## Phase 4 — Public booking flow

Design §6.

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 4.1 | `transition_to()` on `Appointment` | 🔲 | Validates the move is legal, writes the transition, frees the slot where relevant, emits the event. **Views never assign `status` directly** — that's what keeps "confirm" meaning the same thing everywhere. |
| 4.2 | `search_results`: specialty filter + browse grid | 🔲 | Decision E, §5.5. Real server-side filtering. A specialty with no bookable doctors is never a live link. |
| 4.3 | `search_results`: widen the horizon, drop the `Profile` dependency | 🔲 | §6 step 2. Currently hardcoded to 5 days; widen to the published week/month. List by `role` + availability, not by whether a `Profile` exists. |
| 4.4 | `booking_form`: real slot claim | 🔲 | `select_for_update()` **and an actual `is_booked` check** — today nothing reads it, so this is not a race fix, it's a missing check (§10 #6). |
| 4.5 | `booking_form`: 2-hour lead time | 🔲 | Decision D. Refuse slots inside the window **and stop offering them** in 4.2/4.3 — a slot the system won't accept shouldn't be presented as bookable. |
| 4.6 | `booking_success_page` | 🔲 | Sets the session marker; email explains *what happens next* rather than promising instant confirmation the flow can't deliver. |
| 4.7 | `appointment_choose_mode` (round 2) | 🔲 | Only reachable at `PENDING_MODE`, same session-marker gate. Three modes per §5. |
| 4.8 | `appointment_detail`: real status + next action | 🔲 | Shows what the patient must do when it's their turn. Remove Reschedule / Contact Doctor / Live Chat until backed by real views. |
| 4.9 | Cancel action | 🔲 | Writes `CANCELLED`, frees the slot, subject to the decision-9 cut-off. |

**Phase gate:** a booking can be made, confirmed, given a mode, confirmed again and cancelled — end to end, by test, with the right email at each step.

---

## Phase 5 — Doctor dashboard

Design §7.

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 5.1 | Confirm / decline the **time** (round 1) | 🔲 | |
| 5.2 | Confirm / reject the **mode** (round 2) | 🔲 | Rejection returns to `PENDING_MODE` with a reason so the patient can pick again, rather than the booking dying. |
| 5.3 | `meeting_url` required when confirming an `ONLINE` mode | 🔲 | Decision B. Validation error, not a silently empty field — an "online appointment" with no link is worse than an unconfirmed one, because both sides think it's settled. |
| 5.4 | Mark completed / no-show | 🔲 | |
| 5.5 | `doctor_availability` → real week/month timetable | 🔲 | Decision 5. **Prerequisite for 5.7.** The fake prev/next/"Today" buttons become real query-param navigation. AGKI needs this horizon for booking to be realistic. |
| 5.6 | `delete_availability` endpoint | 🔲 | The button already calls it; the endpoint has never existed. Refuse any slot with an appointment (2.9's `PROTECT` backs this at the DB level). |
| 5.7 | Delete `doctor_schedule.html` + view + URL | 🔲 | Decision 5, **conditional on 5.5 landing first**. 100% hardcoded fake data with a view that passes zero context. |
| 5.8 | Delete `appointment_details.html` (orphan) | 🔲 | Not referenced by any view; its field refs (`slot.start_time`) don't exist on the model. A trap for the next reader. |
| 5.9 | Delete `/doctorDashboard/base/` route + `doctor_base` view | 🔲 | New finding #19 — renders a base template standalone. Base templates aren't pages. |
| 5.10 | `doctors.html`: scope the query, fix the stat cards | 🔲 | Currently `CustomUser.objects.all()` in a table titled "Registered Doctors". Three stat cards are bound to context the view never provides. |
| 5.11 | Split the Activate/Deactivate control | 🔲 | §4.2 point 5 — the button shows `is_active` but flips `is_approved`. Two explicit toggles. |
| 5.12 | Verify the "Add New Doctor" modal against the real signup view | 🔲 | Its field names were never cross-checked. |
| 5.13 | Real sidebar badges | 🔲 | Bind to the `pending_count` the view already computes; the template ignores it and shows hardcoded `12` / `8`. |
| 5.14 | `available_appointments`: add the Date column, fix empty-state copy | 🔲 | Says "No appointments scheduled for today" on a page listing *all* appointments. |
| 5.15 | Fix the mismatched icons/labels | 🔲 | Wedding-ring icon on "Reason for Visiting", gender icon on "Appointment Mode" — leftovers from the source template's unrelated fields. |

**Phase gate:** a doctor can run both confirmation rounds and manage a real timetable; an admin can see across all doctors; neither can touch ecommerce.

---

## Phase 6 — Expiry & scheduling

Design §6.1. **Ships with Phase 4/5, not after** — without it every unanswered booking holds its slot forever.

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 6.1 | `expire_stale_appointments` management command | 🔲 | Idempotent, safe to re-run, also the thing to run manually after an outage. |
| 6.2 | Distinguish who was waited on | 🔲 | Three states can be waiting; only two are the doctor's. `PENDING_MODE` is the **patient**. The transition records which — otherwise the audit blames doctors for patients' inaction, which is worse than not auditing. |
| 6.3 | Cutoff guards in the confirm views | 🔲 | A doctor cannot confirm past the cutoff **even if the cron hasn't run**. Belt and braces: a silently-stopped cron is a classic failure. |
| 6.4 | Cron entry on the production host | 🔲 | ⚠️ Explicit rollout step, per 0.6. Every ~5 minutes. Easy to forget, silent when missed. |
| 6.5 | Expiry notifications | 🔲 | Both parties. The patient is told plainly that the doctor didn't confirm and the slot was released — never silent. |

**Phase gate:** an appointment 29 minutes out expires and frees its slot; one 31 minutes out doesn't; a doctor can't confirm past the cutoff; and the log names who was waited on.

---

## Phase 7 — Branding, CMS integration & template rebuild

Design §3, §3.1. Ships **last**, once data and security are stable.

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 7.1 | Context processor for `SiteSettings` | 🔲 | §3.1. Every page in both apps, so a context processor rather than per-view. |
| 7.2 | Reuse the homepage's cache, don't add a second one | 🔲 | §3.1 point 3 — a second differently-timed cache of the same data means an edit appears on one page and not the other. |
| 7.3 | Remove ~100 `MediBook` strings | 🔲 | In `<title>`, footers, nav brands, and a JS share-sheet payload. |
| 7.4 | Rebuild the templates to AGKI branding | 🔲 | Keep the existing responsive card/grid patterns; re-skin only. |
| 7.5 | Remove the dead decorative UI | 🔲 | Insurance upload, prescriptions, specialty *grid* (replaced by the real one from 4.2), "Accepting New Patients" badge, fake "show more slots". |

**Phase gate:** editing the phone number in the CMS admin changes it on the booking pages, and no "MediBook" string survives.

---

## Phase 8 — Testing

Design §9.5. The CMS work established the pattern (80 tests, shared fixtures in `cms/test_utils.py`, and **verifying each test fails when its bug is reintroduced** rather than trusting a green run).

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 8.1 | Auth: every URL in both apps, enumerated **from the URLconf** | 🔲 | Not a hand-written list — a new view must not be addable without a decision. |
| 8.2 | IDOR: detail page renders no PII without a valid marker | 🔲 | |
| 8.3 | `delete_item` no longer reverses (`NoReverseMatch`) | 🔲 | So it can't quietly return. |
| 8.4 | `Content Editors` holds **zero** permissions on these three apps | 🔲 | §4.3 — enforced by test, not by memory. A future "grant all" would otherwise silently undo it. |
| 8.5 | Every state transition, **including the illegal ones** | 🔲 | e.g. `PENDING_TIME` straight to `CONFIRMED`, skipping both gates, must be refused. |
| 8.6 | Expiry: 29-min expires, 31-min doesn't, slot freed, fault recorded | 🔲 | |
| 8.7 | Booking: second booking on a taken slot fails; <2h slot refused and not offered | 🔲 | |
| 8.8 | Settings model rejects `notice <= cutoff` | 🔲 | The combination that makes every booking expire on creation. |
| 8.9 | Doctor visible by `role` alone, no `Profile` visit required | 🔲 | |
| 8.10 | `ONLINE` confirmation without `meeting_url` fails validation | 🔲 | |
| 8.11 | Mutation-check the suite | 🔲 | Reintroduce each bug, confirm the right tests — and only those — fail. |
| 8.12 | Full suite, all apps | 🔲 | Must not regress the CMS's 80 tests. |

**Phase gate:** green suite that has been proven to have teeth.

---

## Phase 9 — Rollout & handoff

| #   | Task | Status | Notes / Remarks |
| --- | --- | --- | --- |
| 9.1 | Fresh production backup immediately before migrating | 🔲 | |
| 9.2 | Run 0.3's data audit against production, resolve blockers | 🔲 | Especially duplicate slots and multi-appointment slots — both block Phase 2 migrations. |
| 9.3 | Ship Phase 1 (security) independently and first | 🔲 | It depends on nothing else here. |
| 9.4 | Migrate, in the §11 order | 🔲 | |
| 9.5 | Create the cron entry and **verify it fires** | 🔲 | Not "add it" — watch one run. |
| 9.6 | Seed `Specialty` with AGKI's real specialties | 🔲 | And resolve the `"Cardiograph"` mapping with a human. |
| 9.7 | Set `AppointmentSettings` values | 🔲 | 2 hours / 30 minutes, per 0.7. |
| 9.8 | Walk one doctor through both confirmation rounds | 🔲 | Acceptance by the actual user, not by us. |
| 9.9 | Walk the lab administrator through the dashboard | 🔲 | Specifically confirm 1.9 — she lands on the console she administers. |
| 9.10 | Merge to `main` | 🔲 | |

**Phase gate:** a real patient booking has completed both rounds on production, with a real doctor confirming, and the emails arrived.

---

## Decisions log

| Date | Decision | Made by | Why |
| --- | --- | --- | --- |
| 2026-08 | Booking is a **two-round negotiation**, not a single form | AGKI | Matches how AGKI actually works: doctor confirms the time, *then* the patient picks a mode, *then* the doctor confirms that too. Reshaped the whole state machine — see design §0.1. |
| 2026-08 | **Video consultation is a real service**, one of three modes | AGKI | The design doc had removed it as MediBook cruft. Wrong: the problem was that the template *asserted* it decoratively, not that AGKI doesn't offer it. |
| 2026-08 | **Specialty search stays** and gets a real taxonomy | AGKI | Same class of correction as video consultation — the third instance of removing a concept because its implementation was fake. |
| 2026-08 | Appointments/dashboard are configured by **superuser only** | AGKI | The CMS's Content Editors group must not reach booking or clinical data. |
| 2026-08 | `SiteSettings` **stays editable by the CMS manager** | AGKI | Shared company identity, drives no booking behaviour. Boundary is *identity vs. configuration*, not which app renders it. |
| 2026-08 | **Ecommerce is frozen**, superuser-only | AGKI | Needs its own redesign; this project only removes the foreign delete endpoint pointing at it. |
| 2026-08 | Notifications go to **the doctor being booked** | AGKI | The platform hosts doctors beyond AGKI, who can't be expected to watch someone else's dashboard. |
| 2026-08 | Email now, **architected for SMS** | AGKI | Events + channel interface so adding SMS is a registration, not a rewrite. |
| 2026-08 | Expiry **30 min before the slot**; minimum booking notice **2 hours** | AGKI | Leaves a 90-minute window for the doctor to answer. The two values have an invariant: notice must exceed cutoff. |
| 2026-08 | `Rating` dropped now, **architecture kept** | AGKI | Nothing writes to it today, so every rating shown is permanently "No ratings yet". Reshaped so building it later is additive. |
| 2026-08 | **No receptionist role** | AGKI | Doesn't exist today. Enum value stays; nothing assumes it. |

---

## Open risks / carry-forward notes

- **The branch question (0.1) blocks everything except Phase 1.** Twenty unmerged CMS commits sit between this work and `main`.
- **Two Phase 2 migrations can fail on real data** (duplicate slots; multiple appointments per slot). Both are *confirmed possible* in the current code, so prod may already violate them. 0.3's audit is not optional.
- **`is_approved` enforcement (1.10) can lock out existing staff** the moment it ships. Check prod for `is_approved=False` accounts first.
- **Expiry silently does nothing without cron** (0.6, 6.4, 9.5). Verify a real run, don't just add the entry.
- **Per-process cache weakens rate limiting** (0.5). Shared cache backend is a settings-only change and worth doing alongside.
- **Still open from the design:** whether anyone bookable is *not* a doctor (proceeding with "Doctor" as the public term). Only revisit if nurses or technicians become directly bookable.
- **`useraccount` debris** noted during the CMS cleanup and still present: `/useraccount/` serves a 13-line developer scratch page ("QR Code Generator" / "McColn Basement"), and `useraccount/urls.py` registers `name='profile'` twice so one of the two views is unreachable by name. Neither blocks this project; both are adjacent enough to fold in cheaply.
