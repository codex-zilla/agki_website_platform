# AGKI Appointment System — Redesign

**Covers:** `appointments/` (public booking) and `doctorDashboard/` (internal staff console)
**Status:** Revised — verification pass (§0) + AGKI decisions applied (§0.1). Ready to implement.
**Author:** Engineering (design doc)
**Related:** Builds directly on the two audit passes performed on this codebase (see "Findings → Fix Mapping" for the full list of issues this design resolves). Also now depends on the completed CMS work — see [website-cms-implementation-plan.md](website-cms-implementation-plan.md).

---

## 0. Revision note — verification pass

This document was written before the website CMS work shipped. It has since been re-checked **against the running code**, not just re-read: each claim below was reproduced against a throwaway database rather than inferred. That pass produced three kinds of change.

**Already fixed elsewhere — struck from scope:**

| Was | Now |
| --- | --- |
| Cross-cutting #18 — `signin()` hardcodes `redirect('doctor_admin_dashboard')` for every role | Fixed. All sign-in routes funnel through `useraccount.views.post_login_redirect`, which routes by role. |
| §5 — constrain `CustomUser.role` with `choices` | Done, plus a correction this doc didn't anticipate: `role` was doubling as a free-text "Qualifications" field on the profile-edit form, so it was split into `role` (choices) + `qualifications` (free text). Production had doctors with `role='4491'`, which silently excluded them from the booking page. |

**Severity corrections** (§10 tables updated):

- **Double-booking is not a race condition.** `booking_form` never reads `is_booked` at all, so two *sequential* bookings on the same slot both succeed — reproduced, 2 appointments on 1 slot. The prescribed fix (`OneToOneField` + `select_for_update()`) is still right; the original framing understated how easily it happens.
- **Four dashboard URLs return 500, not 403, to anonymous users** — `AnonymousUser` hits a `TypeError` inside the queryset filter. That is incidental, not protection: `/doctors/`, `/ecommerce_items/`, `/schedule/` and `/base/` all return a clean **200** to anyone.

**New findings** — nine items the original pass missed, added to §10 as appointments #18-22, dashboard #17-19 and cross-cutting #19-21. The most serious is that **`delete_item` destroys an ecommerce product on an anonymous GET** (reproduced: item count 1 → 0, no login, no POST, no CSRF).

**New requirement:** these two apps must now read shared branding from the CMS rather than hardcoding it — with a permission boundary that did not exist when this was written. See §3.1 and §4.3.

---

## 0.1 Revision note — AGKI decisions, and a corrected domain model

All nine open decisions are now answered (§12). One answer changes this design more than the other eight combined, so it is recorded here rather than buried in a table.

### The booking flow is a two-round negotiation, not a single form

AGKI described how it actually works:

> The doctor publishes the time they will be available for the day. Patients book an appointment for that day according to the doctor's availability. The doctor then confirms whether that time is still feasible. If confirmed, the patient chooses the mode of meeting — online (video call), doctor travels to the patient, or patient travels to the doctor — and then the doctor confirms the mode.

That is **two provider confirmations with a patient choice in between**, and this document had it wrong in three ways:

| This doc said | Reality |
|---|---|
| Mode is collected on the booking form as a required field (§6 step 3) | Mode is chosen **after** the provider confirms the time — it isn't known at booking |
| Two modes: in-person visit, home sample collection | **Three**: online video call, provider travels to patient, patient travels to provider |
| "Video Consultation → **Removed**. Not part of a lab-visit flow" (§3) | **Video consultation is a real service.** Deleting it as MediBook cruft would have removed a feature AGKI actually offers |

The "remove the teleconsult stuff, this is a lab" instinct in §1 was over-corrected. Some of that template *was* cruft (insurance cards, specialty grids, prescriptions); video consultation was not. §3 and §5 are rewritten accordingly.

**This also means a single `status` boolean can't work.** `AppointmentApproval.is_approved` has one bit; this flow has at least two independent gates. The state machine in §5 replaces it.

### The platform is not AGKI-only

From decision 8: *"this app consists of doctors not only from AGKI."* Two consequences:

- **New-booking notifications go to the provider being booked**, not to a central AGKI inbox — the opposite of what §12 originally recommended, and correct: an external provider can't watch an AGKI dashboard for work that belongs to them.
- §3's "provider list scoped to actual AGKI staff" is too narrow. The scoping rule is *providers who take appointments on this platform*, which is `role=doctor` plus published availability, regardless of employer.

### Second-order consequence worth surfacing now

A two-round flow means the patient must **return to the appointment at least twice** — once to choose a mode after the time is confirmed, and again to see the mode confirmed. §2 still says no patient accounts, and that remains the right call, but it only works if every transition emails the patient a link back to their own appointment. The notification design in §9.1 is therefore load-bearing for the booking flow, not just a courtesy.

---

## 1. Why we're redesigning, not just patching

Both audits converged on the same root cause: the public booking pages and the staff dashboard are an unmodified third-party **"MediBook" doctor-booking demo template**, wired on top of a genuinely reasonable Django data model (`Appointment`, `AvailableSlot`, `AppointmentApproval`, `AppointmentNote`, `Rating`) that was actually built for AGKI. The template's decorative JavaScript was never finished being connected to that backend, and the console it feeds has no authentication wall at all.

That combination means individual patches (fix this button, guard that image) would leave the deeper issues untouched. The template asserts a pile of things nothing backs — specialty search, insurance cards, digital prescriptions, per-provider "Video Consultation" and "Accepting New Patients" badges hardcoded to `true` — while the flow AGKI actually runs (§0.1) isn't implemented at all. Fixing bugs without fixing that mismatch just produces a bug-free version of the wrong product.

> **Corrected after AGKI review.** An earlier version of this paragraph said the system "pretends to be a general doctor-teleconsult platform when AGKI is a medical laboratory — patients book test/sample-collection appointments, not specialist consultations." That over-corrected: the platform does host consultations, including **online video calls**, and hosts providers beyond AGKI. The problem was never that teleconsultation appeared — it was that the template *asserted* it decoratively instead of implementing it. See §0.1.

So this redesign:
- **Keeps** the Django app structure, the core models, the `doctorDashboard` notes workflow, and the existing `useraccount` auth system — all genuinely reusable.
- **Reframes** the booking domain from "find a specialist by rating" to the real two-round flow: publish availability → patient books → provider confirms the time → patient chooses a mode → provider confirms the mode (§6). Most content-level findings disappear as a side effect of modelling that correctly.
- **Rebuilds** the template layer (server-rendered, no more fake client-side "success" flows) and **closes** the security gaps (no auth on staff console, IDOR on patient records, an anonymous product-delete endpoint) as non-negotiable, ship-blocking work.

---

## 2. Scope

**In scope:** `appointments/` (models, views, forms, templates, urls), `doctorDashboard/` (same), the parts of `useraccount/` that gate access to them (login, roles, `is_active`/`is_approved`).

**Out of scope:** the `ecommerce/` app's own code, the marketing site (`app/`, `cms/`), payment processing (not currently used and not recommended — see §8.4).

**Correction to the original scope statement.** This section previously said `doctorDashboard` "only renders" the ecommerce product list and that "the app itself is untouched." That was wrong: `doctorDashboard.views.delete_item` **destroys `ecommerce.Item` rows**, and does so on an unauthenticated GET. So `doctorDashboard`'s ecommerce surface is in scope — as something to lock down and largely remove (§4.4) — even though `ecommerce/`'s own views and models stay untouched.

**Also newly in scope:** `useraccount.Profile`, because the booking flow depends on it in ways this document originally didn't account for (§5.3).

**Explicitly not doing:** patient self-service accounts with passwords. AGKI's patients are walk-in/one-off, and the industry-standard pattern for this (booking reference + one verification field, no password to remember) is a better fit than forcing account creation. This is revisited in §8.4.

---

## 3. Domain reframing

**Corrected after AGKI review — see §0.1.** The original version of this table deleted video consultation as MediBook cruft. It isn't; it's a service AGKI offers. The reframing below keeps what is real and removes only what was never backed by anything.

| Old (MediBook) concept | New (AGKI) concept |
|---|---|
| "Doctor" search by specialty (Cardiologist, Dentist...) | Provider list scoped to **providers who take appointments on this platform** — `role=doctor` with published availability. Not AGKI staff only: the platform hosts external providers too (§0.1) |
| "Video Consultation" as a hardcoded badge on every card | **Kept as a real appointment mode**, but as one of three the *patient chooses after the provider confirms the time* (§6) — not a decorative badge asserting every provider offers it |
| "Digital Prescriptions", "Secure Payments", "Insurance" | Removed — nothing backs any of them. Online payment is a dedicated project if wanted later (§8.4) |
| Reason for visit: "Consultation", "Medication Management"... | A closed choice list covering both lab work and consultation (§5.1) — the platform does both |
| Gender / rating / experience / "Accepting New Patients" filters | Removed — every one was hardcoded `true` or computed from a table nothing writes to (§5.4) |
| Branding: "MediBook" | AGKI branding — sourced from `cms.SiteSettings` rather than hardcoded, see **§3.1** |

The reframing still isn't cosmetic: it removes the "Popular Specialties" grid, the insurance upload and the fake filters as a consequence of correct scoping. It just no longer throws out teleconsultation with them.

---

### 3.1 Branding source: the CMS, not hardcoded values

This document originally said to reuse "the nav/footer/colors already built for the redesigned main site (`design/` folder, recent commits)." **That instruction is now out of date.** The marketing site is no longer hardcoded — its identity lives in `cms.SiteSettings` (logo, favicon, tagline, phone, email, address, socials, copyright), editable in the admin without a deploy.

If these templates are rebuilt with AGKI's details typed in, the result is two sources of truth: staff change the phone number in the admin, the marketing site updates, and the booking pages keep showing the old one. That drift is guaranteed, just delayed.

**Design:**

1. Both apps' base templates read shared identity from `SiteSettings` — the same fields the homepage footer uses. A context processor is the right shape here, since it is every page in both apps rather than a handful of views.
2. `SiteSettings` is loaded via `.load()`, which creates the row if absent, so a fresh install renders rather than raising.
3. The homepage caches its content for five minutes (`app.views._home_content`). These pages should use the **same cache key or none at all** — what they must not do is introduce a second, differently-timed cache of the same data, so an edit appears on one page and not the other.
4. All ~100 `MediBook` strings across these templates are removed as part of this pass — they are currently in `<title>`, footers, nav brands and even a JS share-sheet payload.

**The permission boundary (per §4.3).** Reading `SiteSettings` here means a Content Editor changing the site phone number *does* change the booking page footer. That is the intended behaviour for **shared company identity** — one phone number, one place. It is *not* a licence for Content Editors to configure booking itself: appointment modes, reason-for-visit options, booking instructions and notification recipients live in a separate superuser-only model and are never exposed to that group. If AGKI wants even shared identity frozen for these pages, that is **Decision 6** and changes the above to "copy the values at build time," with the drift cost accepted explicitly.

---

## 4. Roles & permissions

Current state: `CustomUser.role` is a free-text field; `is_active` gates login; `is_approved` exists on the model but **the live `signin()` view never checks it** — so admin "approval" of a staff account is currently decorative. There is no `@login_required` anywhere in `doctorDashboard`.

### 4.1 Roles (formalized, minimal change)

Keep `role` as a `CharField`, but constrain it with `choices` and use it consistently:

| Role value | Who | Access |
|---|---|---|
| `admin` | Lab administrator | Full `doctorDashboard`: all appointments, all providers, staff activation, availability for anyone |
| `doctor` | Provider taking appointments — AGKI staff *or* external (§0.1). Kept as the existing DB value to avoid a churny rename | Own appointments, own availability, own patients' notes/confirmations only |
| ~~`receptionist`~~ | — | **Dropped (decision 4).** AGKI has no front-desk role today. The value stays in `Role.choices` since it already shipped and costs nothing, but no UI, permission or code path assumes it exists. Adding it later is a permission change, not a redesign |

### 4.2 Enforcement (concrete)

1. **Every `doctorDashboard` view gets `@login_required`.** This alone closes Critical #6 from the audit.
2. Add one small decorator, `@role_required("admin", "doctor")`, applied per-view, so `doctors()` / `activate_deactivate_doctor()` / `ecommerce_items()` are admin-only, while `doctor_availability()` / `available_appointments()` etc. work for both `doctor` and `admin`.
3. Object-level scoping stays as-is where it already exists (`Appointment.objects.filter(slot__user=doctor)` in `doctor_appointment_details`) — it was correctly written, it just had no auth wall in front of it. Add an `admin`-role bypass so admins can see everything, not just their own slot's appointments.
4. **Fix the login gate to actually mean something:** `signin()` currently only checks `is_active`. Change it to also require `is_approved` (matching the model's own comment: *"added so as to add admin approval to login"*), with a clear message ("Your account is pending admin approval") when it fails. This makes the Activate/Deactivate control in `doctors.html` finally control something real.
5. **Fix the Activate/Deactivate control itself:** today the button's label/icon/badge reflect `is_active` but the POST only flips `is_approved` (Critical #9 from the audit) — two different fields, one of them unreachable from the UI. Redesign: a single "Account status" control per staff row with two independent, clearly-labeled toggles — **Approved for login** (`is_approved`, what gates `signin()` per point 4 above) and **Active** (Django's `is_active`, an admin kill-switch that also works with `login_required`'s built-in behavior). Stop conflating them in one button.
6. ~~**Role-based post-login redirect**~~ — **done, see §0.** `post_login_redirect` now owns this decision for every sign-in route. It sends `role=doctor` to the dashboard, any other `is_staff` account to `/admin/`, and everyone else to the patient dashboard. Nothing further needed here.

### 4.3 Who may edit this system's content (AGKI decision, binding)

The CMS shipped a **Content Editors** group scoped to the `cms` app so non-technical staff can update the marketing site. **That group must not gain any control over booking or the clinical dashboard.** Per AGKI: these two apps are configured by the **superuser only**.

Concretely:

1. Any new configuration model introduced for these apps (booking instructions, appointment modes offered, reason-for-visit list, notification templates) is registered in the Django admin **without** granting permissions to `Content Editors`. The existing `cms/migrations/0003_content_editors_group.py` enumerates that group's permissions explicitly, so this is enforced by *not* adding rows — but the enforcement must be asserted in a test, not left to memory, because a future `makemigrations` or a well-meaning "grant all" would silently undo it.
2. `role=doctor` accounts get **no** access to these settings either. Providers manage their own availability and their own patients; they do not configure the system.
3. **`cms.SiteSettings` stays editable by Content Editors** — AGKI's explicit call. It carries shared company identity (logo, tagline, phone, email, address, socials), which is exactly the kind of thing the CMS exists to let staff maintain, and it drives no booking or clinical behaviour. A Content Editor updating the phone number changes it on the booking pages too, and that is the point: one number, one place. The boundary is **identity vs. configuration** — not "which app renders it."
4. Nothing in `appointments`, `doctorDashboard` or `ecommerce` is ever added to that group. §9.5 makes this a test.

### 4.4 Ecommerce access (AGKI decision, binding)

**Providers must not be able to affect anything on the ecommerce side.** Today the opposite is true, and worse than "doctors can" — *anyone* can, without logging in:

- `delete_item` deletes a product on a plain **GET**, with no authentication and no CSRF protection. Reproduced against a test database: product count 1 → 0.
- `ecommerce_items` lists the full catalogue with stock and featured counts to anonymous visitors (HTTP 200).

**AGKI's decision (1):** the ecommerce site needs its own redesign later, so **nothing about it is rebuilt now** — it is frozen, and only a superuser may reach or configure it.

Redesign:

1. **Remove `delete_item` from `doctorDashboard` entirely.** Product deletion belongs in the Django admin, which already provides a confirmation step, object-level permissions and a `LogEntry` audit trail — none of which a hand-rolled delete URL has. This is a deletion, not a port. A test asserts the route no longer reverses, so it cannot quietly return.
2. `ecommerce_items` becomes **read-only and superuser-only** — not `role=admin`, *superuser*. It stays only as a convenience view; anything that mutates a product happens in the Django admin.
3. The sidebar link renders only for superusers, so a provider never sees a control they can't use.
4. `ecommerce/`'s own views, models and templates are **not touched** by this project. Its redesign is a separate piece of work, and this one should leave it exactly as found apart from removing the foreign delete endpoint pointed at it.

---

## 5. Data model changes

Additive migrations on the existing models — no rip-and-replace. Each change is tied to a specific audit finding (cross-referenced in §10).

### `appointments/models.py`

```python
class AvailableSlot(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.TimeField()
    is_booked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "date", "time")]   # NEW — stops duplicate-slot creation

```python
class Appointment(models.Model):
    class Status(models.TextChoices):
        # --- round 1: the provider confirms the time ---
        PENDING_TIME  = "pending_time",  "Awaiting provider's confirmation of the time"
        TIME_DECLINED = "time_declined", "Time declined by provider"
        # --- round 2: the patient picks a mode, the provider confirms it ---
        PENDING_MODE  = "pending_mode",  "Time confirmed - awaiting patient's choice of mode"
        MODE_PROPOSED = "mode_proposed", "Awaiting provider's confirmation of the mode"
        # --- settled ---
        CONFIRMED     = "confirmed",     "Confirmed"
        COMPLETED     = "completed",     "Completed"
        CANCELLED     = "cancelled",     "Cancelled"
        NO_SHOW       = "no_show",       "No-show"

    class Mode(models.TextChoices):
        ONLINE           = "online",           "Online video call"
        PROVIDER_TRAVELS = "provider_travels", "Provider travels to the patient"
        PATIENT_TRAVELS  = "patient_travels",  "Patient travels to the provider"

    fullName = models.CharField(max_length=100)
    birthDate = models.DateField()
    gender = models.CharField(max_length=20, choices=Gender.choices)      # CHANGED: closed set (§5.1)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    visitType = models.CharField(max_length=20, choices=VisitType.choices)          # CHANGED: closed set
    reasonVisit = models.CharField(max_length=30, choices=ReasonForVisit.choices)   # CHANGED: closed set
    symptoms = models.TextField(blank=True)          # CHANGED: blank=True, matches the UI which never marked it required
    slot = models.OneToOneField(AvailableSlot, on_delete=models.PROTECT)  # CHANGED: FK -> OneToOne; PROTECT, see below
    bookingID = models.CharField(max_length=20, unique=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_TIME)

    # Mode is NOT collected at booking - it is chosen in round 2, after the
    # provider confirms the time. Null until then, which is why it stays
    # nullable despite being mandatory before an appointment is CONFIRMED
    # (enforced in clean(), not by the column).
    appointmentMode = models.CharField(max_length=20, choices=Mode.choices, blank=True)

    time_confirmed_at = models.DateTimeField(null=True, blank=True)
    mode_confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # `age`: DROPPED - never collected, always null, dead column.


class AppointmentTransition(models.Model):
    """Append-only audit trail: who moved this appointment to which state, when.

    Health-adjacent data with a multi-party approval flow needs to be able to
    answer "who confirmed this and when" months later. `status` holds the
    current state; this holds the history. Also satisfies the audit-logging
    requirement in §9.4 without a second mechanism.
    """
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    actor = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)  # null = the patient
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

### The state machine

```
                      ┌─ provider declines ──> TIME_DECLINED ──> (slot freed)
   booking ──> PENDING_TIME
                      └─ provider confirms ──> PENDING_MODE
                                                   │  patient chooses mode
                                                   v
                                              MODE_PROPOSED
                                                   │  provider confirms mode
              provider rejects mode <──────────────┤
              (back to PENDING_MODE)               v
                                               CONFIRMED ──> COMPLETED
                                                          └─> NO_SHOW

   CANCELLED is reachable from any non-terminal state (§12 decision 9 sets the cut-off).
```

Transitions live in one place — a `transition_to(new_status, actor)` method that validates the move is legal, writes the `AppointmentTransition` row, frees the slot where relevant, and emits the notification event (§9.1). Views never assign `status` directly. That is what keeps "confirm" meaning the same thing in the dashboard, the admin, and any future API.

**Why `AppointmentApproval` goes away.** It carries a single `is_approved` boolean, which cannot express a flow with two independent provider gates — approving the *time* and approving the *mode* are different acts. Keeping it alongside `status` would create two disagreeing sources of truth. Migration: `is_approved=True` → `status=CONFIRMED`, `False`/absent → `PENDING_TIME`, and each existing row becomes a seed `AppointmentTransition` so the history isn't lost. Its `approved_by`/`approved_at` map onto the transition's `actor`/`created_at`.

**Why `on_delete=PROTECT` on `slot`.** The original sketch kept `CASCADE`, which means deleting an availability slot silently deletes the patient's appointment and their record of it. A slot with a live appointment should refuse to be deleted (the doc already requires this guard on the delete-slot endpoint in §7); `PROTECT` makes the database enforce what the view is supposed to check.

**Why `OneToOneField` on `slot`:** this is the cleanest fix for the double-booking race condition (High #1 in the audit). A `ForeignKey` lets two `Appointment` rows point at the same slot if two requests race between the `is_booked` check and the save; a `OneToOneField` makes that a database-level integrity error, not just an application-level check that can be skipped. The view still does a `select_for_update()`-wrapped check-then-save for a clean user-facing error message, but the DB constraint is the real backstop.

**Why `status` at all:** the old boolean can only express "approved" or "not yet," with no way to represent cancelled, completed, or no-show — which is why every "Cancel Appointment" button in both audits turned out to be fake, since there was nowhere to persist it. The two-round flow (§0.1) then makes a boolean untenable outright.

### 5.1 Constrain the free-text clinical fields

The original model sketch keeps `visitType`, `reasonVisit` and `gender` as bare `CharField`s. **That repeats a mistake this codebase has already paid for.** `CustomUser.role` was free text, and production accumulated doctors with `role='4491'` and `role='1'` — which silently excluded a real doctor from the public booking page, because the query filtered on an exact string. A choice list has to exist as `choices` on the model, not only as `<option>` tags a POST can bypass:

```python
class VisitType(models.TextChoices):
    NEW_PATIENT = "new_patient", "New patient"
    FOLLOW_UP   = "follow_up",   "Follow-up"

class ReasonForVisit(models.TextChoices):
    LAB_TEST        = "lab_test",        "Lab test / panel"
    REFERRED_TEST   = "referred_test",   "Doctor-referred test"
    RESULT_FOLLOWUP = "result_followup", "Result collection / follow-up"
    CONSULTATION    = "consultation",    "Consultation"
    OTHER           = "other",           "Other"
```

**Note what is deliberately *not* in that list:** "home sample collection." The original draft had it as a reason for visit, which conflates two different things. After §0.1 it is a **meeting mode** (`PROVIDER_TRAVELS`) — *how* the appointment happens, chosen in round 2 — not *why* the patient is booking. Someone can book a lab test and then agree the provider will travel to them; those are independent answers. `CONSULTATION` is added because the platform does consultations too, not just lab work (§0.1).

`gender` gets the same treatment, with a free-text "prefer to self-describe" option if AGKI wants one — the point is that the *set* is closed and validated server-side, not that it is short.

### 5.2 Reject impossible dates

Neither `AvailableSlotForm` nor `AppointmentForm` has any validation today, so a provider can publish availability in the past and a patient can book it by URL (the 5-day window is a display filter in `search_results`, not a constraint). Add `clean()` on both: a slot's `date`/`time` must be in the future when created, and `booking_form` must refuse a slot whose datetime has passed even if it is still `is_booked=False`.

### 5.3 `Profile` — the booking flow's hidden dependency

Not mentioned anywhere in the original draft, and it breaks booking in two ways:

- `Profile` rows are created **lazily** — `get_or_create` in the `profile()` view, the first time a user opens their own profile page. Nothing creates one at account creation.
- `search_results` iterates `Profile.objects.filter(user__role=DOCTOR)`, so **a provider with no `Profile` row is invisible to the public booking page entirely.** This is not hypothetical: after `role` was corrected, `Dr. Magembe` and `Kyla` both had `role='doctor'` and still didn't appear, because neither had ever opened their profile page.
- `booking_success_page` and `appointment_detail` then call `Profile.objects.get(user=...)` unguarded, which raises `DoesNotExist` for exactly those providers.

Fix: create the `Profile` in a `post_save` signal on `CustomUser` (with a data migration to backfill the accounts missing one), and change the two `.get()` calls to `get_or_create` or a guarded lookup. Provider visibility should follow from `role`, never from whether someone happened to visit a page once.

### 5.4 `Rating` — decide, don't inherit

`Rating` is read by `search_results`, `booking_form` and `appointment_detail`, which compute averages and "recommended %" from it. **Nothing in the entire codebase ever creates a `Rating`.** It is permanently empty scaffolding from the MediBook template, so every one of those computed values is a fixed "No ratings yet."

**AGKI's decision (7): drop it for now, but leave the architecture ready** so building it later is an addition, not a retrofit. Concretely:

1. **Remove all three read sites now.** `search_results`, `booking_form` and `appointment_detail` stop computing averages from an empty table and stop rendering "No ratings yet" as though it were data.
2. **Keep the model, but fix its shape first.** As written it is `Rating(doctor, rating_value, feedback, recommended)` with **no link to an appointment** — so there is nothing to stop one person rating a provider a hundred times, and no way to tell which visit a rating refers to. The integration point that makes it easy later is:
   ```python
   class Rating(models.Model):
       appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)  # NEW - one rating per visit
       provider = models.ForeignKey(CustomUser, on_delete=models.CASCADE)          # renamed from `doctor`
       rating_value = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
       feedback = models.TextField(blank=True)
       recommended = models.BooleanField(default=True)
       created_at = models.DateTimeField(auto_now_add=True)
   ```
   The `OneToOneField` is the whole trick: it makes "one rating per completed appointment" a database rule, and gives the future feedback form a natural, already-authenticated entry point — the patient reaches it through the same signed session marker that already protects their appointment detail page.
3. **Reserve the trigger, don't build it.** The `COMPLETED` transition is where a "how did it go?" notification would be emitted. §9.1's event list includes `appointment.completed`; wiring a feedback email to it later is a new handler, not a change to the booking flow.

So the work when AGKI wants ratings is: one form, one view, one notification handler. No migration of live appointment data, no changes to booking.

### 5.5 `useraccount/models.py`

```python
class CustomUser(AbstractUser):
    ...
    role = models.CharField(max_length=20, choices=Role.choices)   # DONE - see §0
    qualifications = models.CharField(max_length=255, blank=True)   # DONE - split out of role
```
Already shipped. Everything else (`is_active`, `is_approved`, `image`) is reused as-is.

---

## 6. Public booking flow (redesigned)

Every step below is a **real Django `POST` to a real view** — no `e.preventDefault()` decorative flows, no client-side-only "success" alerts.

1. **Home** (`appointment_home`) — AGKI-branded, states plainly this is for booking a lab visit or sample collection, one CTA: "Book an appointment."
2. **Provider / date selection** (`search_results`) — server-rendered list of providers with published availability, each showing their real slots. **Widened per decision 5:** the current hard-coded 5-day window becomes the same week/month horizon the provider publishes against (§7), so a patient can see and book further out than the day after tomorrow. Providers are listed by `role` and published availability, **not** by whether a `Profile` row happens to exist (§5.3). Removed: gender/rating/experience filters, the "Accepting New Patients" badge (hardcoded `true`), and the fake "show more slots" button. The "Video Consultation" badge also goes — not because video isn't real (§0.1), but because asserting it per-provider on a card was never backed by data; mode is negotiated per appointment.
3. **Booking form** (`booking_form`) — collects patient info and `reasonVisit` from the closed choice list (§5.1). **It does not ask for the meeting mode**: per §0.1 that is chosen later, once the provider has confirmed the time. The submit button is a plain HTML form submit — no JS intercepts it. Server-side: wrap the slot claim in `select_for_update()`, **actually check `is_booked`** (today nothing does), and return a clear "This slot was just taken, please pick another" error instead of silently creating a conflicting row. Result: `status=PENDING_TIME`, and a `booking.created` notification to **the provider being booked** (§9.1).
4. **Confirmation** (`booking_success_page`) — shows the booking ID and sets the signed, appointment-scoped session marker (§8.1). Emails the patient their reference **and what happens next**: that the provider will confirm the time, and that they'll be emailed when it's their turn to choose a meeting mode. The old template promised an instant confirmation the flow can't deliver.
5. **Round 1 — provider confirms the time** (`doctorDashboard`, §7). On confirm → `PENDING_MODE` and the patient is emailed a link back. On decline → `TIME_DECLINED`, the slot is freed for someone else, and the patient is told (with the provider's reason, if given).
6. **Round 2 — patient chooses the mode** (`appointment_choose_mode`) — reachable only when `status=PENDING_MODE`, gated by the same session marker as the detail page. Presents the three modes from `Appointment.Mode`. Submitting sets `appointmentMode` and moves to `MODE_PROPOSED`, notifying the provider.
7. **Round 2 — provider confirms the mode** (`doctorDashboard`). Confirm → `CONFIRMED` (patient emailed the final details, including the video link if the mode is online). Reject → back to `PENDING_MODE` with a reason, so the patient can pick a different mode rather than the booking dying.
8. **Trace an appointment** (`booking_id_lookup`) — real server-side lookup. Input is `bookingID` **plus one verification field** (phone number or DOB) — the industry-standard "reference + PII" pattern, and what actually closes the IDOR (§8). Rate-limited (§8.2).
9. **Appointment detail** (`appointment_detail`) — shows the real state from `status`, not a hardcoded "Confirmed" banner, and shows *what the patient must do next* when it's their turn. A real cancel action (writes `CANCELLED`, frees the slot, subject to the §12-decision-9 cut-off). Every button with no backend (Reschedule, Contact Doctor, Live Chat) is removed until built — a working "Cancel" beats five fake buttons.

**Online mode.** If the confirmed mode is `ONLINE`, something has to carry the video link. This design does **not** build video calling — it stores a `meeting_url` the provider supplies on confirmation, and includes it in the confirmation email and on the detail page. Whether that is Google Meet, Zoom or something else is AGKI's operational choice, and needs no code change.

---

## 7. Staff dashboard (redesigned)

All `doctorDashboard` views get `@login_required` + `@role_required(...)` (§4.2), the ecommerce views get the tighter superuser-only treatment from §4.4, and the chrome reads its branding from `SiteSettings` per §3.1. Beyond the auth wall:

- **`doctor_admin_dashboard`**: keep the existing server-computed stats (today/week counts, utilization) — they were correctly written, just needs the sidebar's hardcoded fake badges (`12`, `8`) replaced with the real `pending_count` the view already computes but the template ignores.
- **`available_appointments`**: add a Date column (currently missing — the page shows only time-of-day, useless for a list not scoped to "today"); fix the empty-state copy, which currently says "No appointments scheduled for today" on a page that lists *all* appointments.
- **`doctor_appointment_details`**: keep the real Approve/Add-Note forms (these already work correctly — CSRF, real POST, real persistence). Add a real **Cancel** action (writes `status=CANCELLED`, frees the slot) and a real **Mark Completed** action. Remove Reschedule/Send Message/View Medical History until backed by real views. Fix the mismatched icons/labels in the Personal Information card (wedding-ring icon on "Reason for Visiting," gender icon on "Appointment Mode" — leftovers from the source template's unrelated original fields).
- **`doctor_availability`**: keep the real "Add Availability" form. Add the missing `delete_availability` view/URL — the Delete Slot button already calls `fetch('/delete-availability/<id>/', {method: 'DELETE'})`; that endpoint just needs to exist, refusing any slot that has an appointment (and `on_delete=PROTECT` from §5 backs that at the database level).
  **Expanded per decision 5:** this page must be a real **timetable**, not just the current week. AGKI needs to see a provider's availability across a **week or a month** for booking to be realistic — so the fake prev/next/"Today" buttons become real query-param navigation (`?week=` / `?month=`), with a month grid alongside the week view. This is now a requirement, not the "or wire them to real query params" afterthought the original draft made it.
- **`doctor_schedule.html`**: **delete it (decision 5).** It is 100% hardcoded fake data with a view that passes zero context, and every capability it appears to offer is covered by `doctor_availability` once that page gains the week/month navigation above. AGKI confirmed the deletion on that condition — so the timetable work is a prerequisite for removing this page, not independent of it.
- **`doctors.html`** (admin-only): scope the query to actual provider/staff roles instead of `CustomUser.objects.all()` (which currently mixes every account type into a table titled "Registered Doctors"). Fix the three stat cards that are wired to context variables (`pending_count`, `total_patients`, `today_comparison`) the view never actually provides — either pass real values or remove the cards. Fix the Activate/Deactivate control per §4.2 point 5. Verify the "Add New Doctor" modal's field names (`firstname`, `password1`, etc.) against the real `signup` view before shipping — they were never cross-checked.
- **Delete the orphaned `appointment_details.html`** template (not referenced by any view, and its own field references — `slot.start_time`/`slot.end_time` — don't exist on `AvailableSlot`). Dead code, currently a trap for the next person who assumes it's live.
- **Image handling**: every `{{ user.image.url }}` / `{{ doctor.user.image.url }}` across both apps gets wrapped exactly like `doctors.html` already correctly does it (`{% if doctor.image %}...{% else %}<fallback icon>{% endif %}`) — that page is the one place in the whole codebase that got this right; it becomes the template for everywhere else. This alone fixes five separate crash sites identified across both audits.

---

## 8. Security design

### 8.1 Patient record access (fixes the IDOR)

Today, `/appointments/detail/<int:appointment_id>/` and the redirect target of `booking_success_page` both use the raw sequential database ID with zero ownership check — full patient PII (name, DOB, phone, email, symptoms) readable by incrementing a number in the URL.

**Redesign:**
- The URL keys off `bookingID` (`AGKI-YYYY-XXXXX`), not the integer PK — removes trivial sequential enumeration.
- `bookingID` alone is still not treated as a secret (100,000 combinations/year is guessable at scale). Direct GET access to the detail page additionally requires a **signed, appointment-scoped session marker**, set in exactly two places:
  1. `booking_success_page`, immediately after a patient creates their own appointment (so the confirmation → detail flow stays frictionless).
  2. `booking_id_lookup`, after a successful `POST` that matches **both** `bookingID` and a second field (phone number or DOB) — not `bookingID` alone.
- A bare GET to the detail URL with neither marker present redirects to the lookup form instead of rendering patient data.
- This mirrors how most real-world "track your order/appointment" portals handle anonymous access, without forcing account creation.

### 8.2 Rate limiting

`booking_id_lookup` becomes a brute-forceable endpoint once it's the only path to patient data (good — that's the point), so it needs throttling: cap attempts per IP (e.g. Django's cache framework, 5 attempts / 10 minutes, generic "check your details and try again" error that doesn't reveal which field was wrong).

### 8.3 Staff console

Covered in §4 — `@login_required` on every view, `is_approved` actually enforced at login, role-scoped querysets.

### 8.4 Payments / insurance

Not currently implemented anywhere despite the UI promising it (price breakdowns, "Insurance Coverage," Stripe-shaped copy in the old template) — correctly excluded from this redesign's scope. If AGKI wants online payment later, that's a dedicated PCI-scope project, not a template fix, and is called out explicitly so it isn't quietly assumed to already work.

---

## 9. Non-functional requirements

### 9.1 Notifications — email now, SMS later, without a rewrite

**AGKI's decision (3):** ship email, but architect so SMS drops in later rather than forcing a rewrite. And per decision 8, **a new booking notifies the provider being booked**, not a central AGKI inbox — the platform hosts external providers who cannot be expected to watch someone else's dashboard.

The way to make SMS a later addition rather than a rewrite is to make call sites emit **events**, never "send this email":

```python
# appointments/notifications.py
notify("appointment.booked", appointment, to=appointment.slot.user)      # -> the provider
notify("appointment.time_confirmed", appointment, to=appointment.email)  # -> the patient
```

Three pieces:

1. **Events, not messages.** `transition_to()` (§5) emits one event per state change: `appointment.booked`, `time_confirmed`, `time_declined`, `mode_proposed`, `mode_confirmed`, `cancelled`, `completed`, `reminder_due`. Views never call `send_mail` directly — that is exactly the coupling that forces a rewrite when a second channel appears.
2. **Channels behind one interface.** `EmailChannel` ships now, reusing the existing SMTP config. `SmsChannel` is a stub raising `NotImplementedError`, present so the seam is real and tested rather than imagined. A per-event channel map decides what goes where; adding SMS is registering a channel and filling in one method.
3. **A `NotificationLog` row per send** (event, recipient, channel, status, timestamp). For a system where "did the patient ever get told?" is a real operational question, "check the mail server logs" is not an answer. It also makes retries possible without guessing.

**Recipient data already exists** for both channels: `Appointment.phone` is collected today, and providers have `CustomUser.phone`. So SMS needs no new data capture — only a provider integration (e.g. Africa's Talking).

**Copy discipline:** no template says "you'll receive an SMS reminder" until `SmsChannel` is real. The old templates promised exactly that with no SMS integration anywhere in the codebase.

### 9.2 Accessibility

Form labels correctly tied to inputs; required fields marked with `required` **and** visually asterisked consistently (fixes the `symptoms` mismatch, where the backend required it but the UI never said so).

### 9.3 Mobile responsiveness

Keep the existing card/grid patterns — they are already responsive — re-skinned to AGKI branding.

### 9.4 Observability

Nothing here has logging beyond Django's defaults. `AppointmentTransition` (§5) provides the audit trail for state changes; add structured logging around booking creation, cancellation and provider confirmations on top of it. For a system handling health data, "who confirmed this and when" needs an answer that survives months.

### 9.5 Testing

Both apps still have empty `tests.py` files. The CMS work established the pattern to follow (`cms/tests.py`, `app/tests.py`, `useraccount/tests.py` — 80 tests, with shared fixtures in `cms/test_utils.py`), including verifying the tests actually fail when the bug is reintroduced rather than trusting a green run. Minimum bar before this ships:
- Model tests for the `OneToOneField` / `unique_together` constraints.
- A test proving the detail page redirects to the lookup form without a valid session marker, and **does not** render PII.
- A test **enumerating every URL in both apps** and asserting anonymous access redirects to login — enumerated from the URLconf, not a hand-written list, so a new view can't be added without a decision.
- A test asserting `delete_item` no longer exists as a route (`NoReverseMatch`), so it can't quietly return.
- A test asserting the `Content Editors` group holds **zero** permissions on `appointments`, `doctorDashboard` or `ecommerce` models (§4.3).
- A test asserting a provider appears on the booking page based on `role` alone, with no `Profile` visit required (§5.3).
- A test proving a second booking on an already-booked slot fails.
- **A test per state transition**, including the illegal ones — that `transition_to` refuses e.g. `PENDING_TIME` straight to `CONFIRMED`, skipping both gates.

---

## 10. Findings → Fix Mapping

### `appointments/` (public booking)

| # | Finding (severity) | Fix in this design |
|---|---|---|
| 1 | **Critical** — IDOR: sequential `appointment_id` exposes any patient's PII | §8.1 — bookingID-keyed URL + signed session marker required for direct access |
| 2 | **Critical** — "Trace an Appointment" JS is unusable (wrong ID format, missing DOM refs, unconditional `preventDefault`) | §6 step 5 — real server-side POST lookup, no client JS gatekeeping submission |
| 3 | **Critical** — Booking form's "Confirm" only works by accident (fake JS success handler, real submission happens only because unrelated JS crashes first) | §6 step 3 — plain HTML submit, no JS interception at all |
| 4 | **Critical** — Entire section is generic "MediBook" template content, wrong for a lab | §3 — domain reframing (specialties/video-consult/insurance removed, lab-appropriate copy) |
| 5 | **Critical** — `.image.url` unguarded, crashes page if a provider has no photo | §7 last bullet — `{% if doctor.image %}` guard everywhere, modeled on `doctors.html`'s existing correct pattern |
| 6 | **Critical** *(was High)* — Double-booking. **Re-verified: this is not a race condition.** `booking_form` never reads `is_booked`, so two *sequential* bookings on the same slot both succeed — reproduced, 2 appointments on 1 slot, no concurrency involved | §5 — `Appointment.slot` becomes `OneToOneField` + an actual `is_booked` check under `select_for_update()` |
| 7 | **High** — Promised email/SMS never sent | §9.1 — real notifications on every state change, provider notified of new bookings (decision 8); SMS deferred but the channel seam is built now (decision 3), and no copy promises it until it exists |
| 8 | **High** — Cancel/Reschedule/Contact/Live-Chat are decorative only | §6 step 6 — real Cancel action; other buttons removed until backed by real views |
| 9 | **High** — No `status` field, cancellation structurally impossible | §5 — `Appointment.status` state machine, slot freed on cancel |
| 10 | Medium — `symptoms` required server-side, optional-looking UI | §5 — model changed to `blank=True` to match actual UI intent (confirmed genuinely optional going forward) |
| 11 | Medium — `appointmentMode` never collected, breaks detail page title | §6 step 3 — now a required form field |
| 12 | Medium — dead `age` field | §5 — dropped |
| 13 | Medium — client-side-only pagination/filtering, duplicated inline JSON per doctor | §6 step 2 — server-rendered list, filters removed (were fake anyway — gender/rating filters on hardcoded `true` data) |
| 14 | Medium — fake "show more slots" appends non-existent times | §6 step 2 — removed |
| 15 | Medium — hardcoded countdown date / "Dr. Sarah Johnson" | §6 step 6 — real appointment data only, no hardcoded demo content survives |
| 16 | Low — dead placeholder links/contacts, "MediBook" footer | §3 — AGKI branding pass |
| 17 | Low — no tests | §9.5 |
| **18** | **Critical (new)** — `Profile` is created lazily, so a provider who has never opened their profile page is **invisible to the public booking page**, despite having `role='doctor'`. Confirmed live for two real accounts. `booking_success_page` / `appointment_detail` additionally raise `Profile.DoesNotExist` for them | §5.3 — create `Profile` on user creation + backfill migration; guard the two `.get()` calls |
| **19** | **High (new)** — no date validation anywhere: a provider can publish availability in the past and a patient can book it directly by URL (the 5-day window is only a display filter) | §5.2 — `clean()` on both forms; refuse past slots at booking time |
| **20** | **Medium (new)** — `visitType` / `reasonVisit` / `gender` remain free-text with no `choices`, the same shape as the `role` field that accumulated values like `'4491'` in production | §5.1 — `TextChoices` on all three, validated server-side |
| **21** | **Medium (new)** — `Rating` is read in three views but **nothing anywhere creates one**; every rating figure shown is permanently "No ratings yet" | §5.4 — drop it, or build feedback collection as its own feature (**Decision 7**) |
| **22** | **Medium (new)** — booking creation is an unauthenticated public write with no rate limit; §8.2 throttles only the lookup endpoint | §8.2 extended to `booking_form` |

### `doctorDashboard/` (staff console)

| # | Finding (severity) | Fix in this design |
|---|---|---|
| 1 | **Critical** — zero authentication on all views. **Verified:** `/doctors/`, `/ecommerce_items/`, `/schedule/` and `/base/` return a clean **200** to anonymous visitors. The others return **500, not 403** — `AnonymousUser` hits a `TypeError` inside the queryset filter. That crash is incidental, not a security control | §4.2 point 1 — `@login_required` everywhere |
| 2 | **Critical** — `.image.url` in shared base template crashes entire dashboard | §7 last bullet — same guard pattern applied to `doctor_base.html` |
| 3 | **Critical** — `doctors()` lists every account type; stat cards bound to undefined context | §7 `doctors.html` bullet — role-scoped query, real or removed stat cards |
| 4 | **Critical** — Activate/Deactivate button shows `is_active`, toggles `is_approved` | §4.2 point 5 — split into two explicit, correctly-labeled controls |
| 5 | High — Delete Slot button 404s (no endpoint exists) | §7 `doctor_availability` bullet — real `delete_availability` view added |
| 6 | High — orphaned `appointment_details.html` with broken field refs | §7 — deleted |
| 7 | High — no uniqueness constraint on `AvailableSlot`, duplicate slots possible | §5 — `unique_together` added |
| 8 | High — "Add New Doctor" modal posts to `signup` with unverified field names | §7 `doctors.html` bullet — cross-checked before ship |
| 9 | Medium — decorative-only buttons (Cancel, Reschedule, Message, History, week nav, Export) | §7 — removed or wired to real views per-page |
| 10 | Medium — `doctor_schedule.html` 100% hardcoded fake data | §7 — page deleted, functionality already covered by `doctor_availability` |
| 11 | Medium — fake sidebar badges (`12`, `8`) | §7 first bullet — bound to real `pending_count` |
| 12 | Medium — `available_appointments.html` missing Date column, wrong empty-state copy | §7 — fixed |
| 13 | Low — duplicate `profile`/`logout` URL name registrations | Cleanup during implementation — collapse to one registration each |
| 14 | Low — "MediBook" branding | §3 |
| 15 | Low — mismatched icons/labels (wedding-ring icon on "Reason for Visiting," etc.) | §7 `doctor_appointment_details` bullet |
| 16 | Low — no tests | §9.5 |
| **17** | **Critical (new)** — **`delete_item` destroys an `ecommerce.Item` on an anonymous GET.** No login, no POST guard, no CSRF token. Reproduced against a test database: item count 1 → 0 from a single unauthenticated URL fetch. `@login_required` alone would *not* fix this — a destructive GET stays CSRF-triggerable by any logged-in staff member who visits a crafted link | §4.4 — the endpoint is **deleted**, not ported. Product deletion moves to the Django admin, which has confirmation, permissions and a `LogEntry` trail |
| **18** | **Critical (new)** — `activate_deactivate_doctor` flips `is_approved` on **any** staff account via an unauthenticated POST. Reproduced: `False` → `True` with no session | §4.2 point 1 + §4.2 point 5 — auth wall, admin-only, and the two flags split into explicit controls |
| **19** | **Medium (new)** — `/doctorDashboard/base/` renders a base template standalone (HTTP 200). Base templates are not pages; the route and its `doctor_base` view are dead | Delete both during implementation |

### Cross-cutting (found during this design pass, not the original audits)

| # | Finding | Fix |
|---|---|---|
| 17 | `signin()` never actually checks `is_approved` — the approval flag `doctorDashboard` lets admins toggle currently gates nothing | §4.2 point 4 — **still outstanding**, re-confirmed against the current code |
| ~~18~~ | ~~`signin()` hardcodes redirect to `doctor_admin_dashboard` regardless of role~~ | ✅ **Done** — `post_login_redirect`, see §0 |
| **19** | **(new)** Both apps hardcode AGKI branding and contact details while the marketing site now serves them from `cms.SiteSettings`. Rebuilding these templates with typed-in values guarantees drift the first time staff edit the footer in the admin | §3.1 — read shared identity from `SiteSettings`; booking-specific configuration stays superuser-only per §4.3 |
| **20** | **(new)** `useraccount/urls.py` still registers `name='profile'` **twice** (lines 31 and 67, pointing at `views.profile` and `views.get_user_profile`). Django resolves the duplicate to the last one registered, so one of the two views is unreachable by name — the same collision class that made `{% url 'home' %}` resolve to the shop | Collapse to one registration; this was listed as dashboard #13 but is really a `useraccount` bug |
| **21** | **(new)** The `Content Editors` group created for the CMS must never gain permissions over booking or clinical data. Nothing enforces that today beyond the migration happening not to grant it | §4.3 — assert it in a test rather than relying on memory |

---

## 11. Migration & rollout plan

**Step 0 — ship the security fixes first, on their own.** `@login_required` + `@role_required` across `doctorDashboard`, deletion of `delete_item`, and the IDOR fix (§8.1). These need no data model change, no design sign-off and no template rebuild, and every one of them is currently exploitable by an anonymous visitor. Nothing below should delay them.

1. **Migration 1 (additive, safe):** add `Appointment.status` (default `pending_time`), `appointmentMode` (blank), `time_confirmed_at`, `mode_confirmed_at`, `cancelled_at`, `cancellation_reason`, `meeting_url`; create `AppointmentTransition` and `NotificationLog`. Add `AvailableSlot.Meta.unique_together` — but **run a pre-migration query for existing duplicates first** and report them, rather than letting the constraint fail on dirty data. (This codebase already has duplicate slots: reproduced two identical rows for the same provider/date/time.)
2. **Migration 2 — backfill `status` from `AppointmentApproval`, then retire it.** `is_approved=True` → `CONFIRMED`; `False`/absent → `PENDING_TIME`. Each existing approval also becomes a seed `AppointmentTransition` (`actor` from `approved_by`, `created_at` from `approved_at`) so the history survives. Drop `AppointmentApproval` only in a **later** migration, once the new flow has run in production for a while — keeping the old table read-only for one release is cheap insurance against a backfill mistake being unrecoverable.
   **Note the backfill is lossy in one direction and that is fine:** existing appointments have no mode, so a historically "approved" appointment lands in `CONFIRMED` with `appointmentMode` blank. Do not try to invent one.
3. **Migration 3 (requires care):** `Appointment.slot` `ForeignKey` → `OneToOneField`, and `on_delete` `CASCADE` → `PROTECT`. Verify first that nothing violates it — `AvailableSlot.objects.annotate(c=Count('appointment')).filter(c__gt=1)` — and note this **is not currently guaranteed**: §10 #6 confirms two appointments can already share a slot, so on real data this query may well return rows that need resolving by hand before the constraint can be applied.
4. **Migration 4:** drop `Appointment.age`; convert `visitType`/`reasonVisit`/`gender` to `choices` (audit existing free-text values first and map them — the `role='4491'` precedent says assume dirty data); reshape `Rating` per §5.4 (`appointment` OneToOne, `doctor` → `provider`) even though nothing reads it yet, so the table is ready when the feature is built.
5. **Migration 5:** create `Profile` rows for every account missing one, and add the `post_save` signal (§5.3), so provider visibility stops depending on whether someone once opened a page.
6. **Flow rollout:** the two-round negotiation (§6) and the notification layer (§9.1) ship together — the flow is unusable without the emails that tell each party it is their turn.
7. **Content/branding pass** (domain reframing, template rebuild, `SiteSettings` wiring) ships last, once the data and security layers are stable.

---

## 12. Decisions

**All resolved.** Recorded with their answers so they aren't re-litigated.

| # | Decision | AGKI's answer | Where it lands |
|---|---|---|---|
| 1 | Provider terminology | *(superseded — see the ecommerce answer below, which AGKI gave against this number)* | — |
| — | **Ecommerce** | **Freeze it.** It needs its own redesign later; for now only a superuser may access or configure it | §4.4 — `delete_item` removed outright, product list superuser-only, `ecommerce/` itself untouched |
| 2 | Appointment modes / home collection | **Three modes, chosen in a second round after the provider confirms the time**: online video call, provider travels to patient, patient travels to provider. Video consultation is a **real service**, not template cruft | §0.1, §3, §5 state machine, §6 steps 5-7 |
| 3 | SMS reminders | **Email now, but architect for SMS** so adding it isn't a rewrite | §9.1 — event-based notifications, channel interface, `SmsChannel` stub, `NotificationLog` |
| 4 | Receptionist role | **Does not exist today** | §4.1 — dropped from the design; the enum value stays but nothing assumes it |
| 5 | `doctor_schedule.html` | **Delete it — provided `doctor_availability` gives a real week/month timetable**, which AGKI needs for booking to be realistic | §7 — timetable navigation becomes a requirement and a prerequisite for the deletion; §6 step 2 widens the patient-facing horizon to match |
| 6 | Shared branding from `SiteSettings` | **Yes — the CMS manager keeps `SiteSettings`.** It is shared site identity and affects no booking or dashboard behaviour | §3.1, §4.3 point 3 — boundary is *identity vs. configuration* |
| 7 | `Rating` | **Drop it now, but leave the architecture ready** so it's easy to add later | §5.4 — read sites removed, model reshaped with an `appointment` OneToOne, `appointment.completed` event reserved as the trigger |
| 8 | Booking notifications | **Notify the provider being booked**, not AGKI — the platform hosts providers beyond AGKI | §9.1; also corrects §3's "AGKI staff only" framing (§0.1) |
| 9 | Cancellation window | **Recommendation accepted** — configurable cut-off | §5 / §6 step 9 — cancellation allowed up to N hours before the slot, N in the superuser-only settings model |

**Still genuinely open — carried forward, not blocking:**

| Question | Why it matters |
|---|---|
| **Provider terminology** — "Doctor," "Provider," or "Consultant" in public copy? | The original decision 1 was answered with the ecommerce ruling, so this one is still unanswered. It affects wording on every public page, and now matters more than it did: the platform hosts external providers who may not all be doctors. Defaulting to **"Provider"** unless told otherwise |
| **Who supplies the video link for `ONLINE` appointments?** | §6 assumes the provider pastes a `meeting_url` on confirmation. If AGKI wants links generated automatically, that is a calendar/meeting integration and its own piece of work |
| **What happens if a provider never responds?** | The flow has no timeout: an appointment can sit in `PENDING_TIME` indefinitely, holding a slot the patient can't use and no one else can book. Suggest an auto-expiry (e.g. release the slot if unconfirmed 24h before, or N hours after booking) — needs AGKI's operational preference |
