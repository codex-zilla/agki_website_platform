# AGKI Appointment System — Redesign

**Covers:** `appointments/` (public booking) and `doctorDashboard/` (internal staff console)
**Status:** Draft for review — **revised after a verification pass (see §0)**
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

## 1. Why we're redesigning, not just patching

Both audits converged on the same root cause: the public booking pages and the staff dashboard are an unmodified third-party **"MediBook" doctor-booking demo template**, wired on top of a genuinely reasonable Django data model (`Appointment`, `AvailableSlot`, `AppointmentApproval`, `AppointmentNote`, `Rating`) that was actually built for AGKI. The template's decorative JavaScript was never finished being connected to that backend, and the console it feeds has no authentication wall at all.

That combination means individual patches (fix this button, guard that image) would leave the deeper issues untouched: the system currently pretends to be a general doctor-teleconsult platform (specialties, video consults, insurance, prescriptions) when AGKI is a **medical laboratory** — patients book test/sample-collection appointments, not specialist consultations. Fixing bugs without fixing that mismatch just produces a bug-free version of the wrong product.

So this redesign:
- **Keeps** the Django app structure, the core models, the `doctorDashboard` approval/notes workflow, and the existing `useraccount` auth system — all genuinely reusable.
- **Reframes** the booking domain from "find a specialist" to "book a lab visit / sample collection," which resolves most of the content-level findings as a side effect of correct scoping, not extra work.
- **Rebuilds** the template layer (server-rendered, no more fake client-side "success" flows) and **closes** the security gaps (no auth on staff console, IDOR on patient records) as non-negotiable, ship-blocking work.

---

## 2. Scope

**In scope:** `appointments/` (models, views, forms, templates, urls), `doctorDashboard/` (same), the parts of `useraccount/` that gate access to them (login, roles, `is_active`/`is_approved`).

**Out of scope:** the `ecommerce/` app's own code, the marketing site (`app/`, `cms/`), payment processing (not currently used and not recommended — see §9.4).

**Correction to the original scope statement.** This section previously said `doctorDashboard` "only renders" the ecommerce product list and that "the app itself is untouched." That was wrong: `doctorDashboard.views.delete_item` **destroys `ecommerce.Item` rows**, and does so on an unauthenticated GET. So `doctorDashboard`'s ecommerce surface is in scope — as something to lock down and largely remove (§4.4) — even though `ecommerce/`'s own views and models stay untouched.

**Also newly in scope:** `useraccount.Profile`, because the booking flow depends on it in ways this document originally didn't account for (§5.3).

**Explicitly not doing:** patient self-service accounts with passwords. AGKI's patients are walk-in/one-off, and the industry-standard pattern for this (booking reference + one verification field, no password to remember) is a better fit than forcing account creation. This is revisited in §8.4.

---

## 3. Domain reframing

| Old (MediBook) concept | New (AGKI) concept |
|---|---|
| "Doctor" search by specialty (Cardiologist, Dentist...) | Provider list scoped to actual AGKI staff who take appointments (pathologists, phlebotomists, consultants) |
| "Video Consultation" / Teleconsultation | Removed. Two real appointment modes: **In-person visit** and **Home sample collection** |
| "Digital Prescriptions", "Secure Payments", "Insurance" | Removed — not part of a lab-visit flow today. Flagged as Phase 2 if AGKI wants online payment later (§9.4) |
| Reason for visit: "Consultation", "Medication Management"... | Reason for visit: "Lab Test / Panel", "Doctor-Referred Test", "Result Collection / Follow-up", "Home Sample Collection", "Other" |
| Branding: "MediBook" | AGKI Medical Laboratory branding — now sourced from `cms.SiteSettings` rather than hardcoded, see **§3.1** |

This reframing isn't cosmetic — it's what makes the "Popular Specialties" grid, the insurance card upload, and the video-consult badges disappear as a natural consequence of scoping the product correctly, rather than as a pile of individual deletions.

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
| `doctor` | Lab provider (pathologist, phlebotomist, consultant — kept as the existing DB value to avoid a churny rename) | Own appointments, own availability, own patients' notes/approvals only |
| `receptionist` *(new, optional)* | Front-desk staff | Can view/approve appointments and manage availability on behalf of providers, cannot activate/deactivate staff accounts |

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

1. Any new configuration model introduced for these apps (booking instructions, appointment modes offered, reason-for-visit list, notification recipients) is registered in the Django admin **without** granting permissions to `Content Editors`. The existing `cms/migrations/0003_content_editors_group.py` enumerates that group's permissions explicitly, so this is enforced by *not* adding rows — but the enforcement must be asserted in a test, not left to memory, because a future `makemigrations` or a well-meaning "grant all" would silently undo it.
2. `role=doctor` and `role=receptionist` accounts get **no** access to these settings either. Providers manage their own availability and their own patients; they do not configure the system.
3. Shared *identity* fields (logo, phone, email, address) are a separate question — they are read from `cms.SiteSettings`, which Content Editors **can** edit. See §3.1 and **Decision 6**, because that is exactly the boundary this rule is drawing and it deserves an explicit answer rather than an assumption.

### 4.4 Ecommerce access (AGKI decision, binding)

**Providers must not be able to affect anything on the ecommerce side.** Today the opposite is true, and worse than "doctors can" — *anyone* can, without logging in:

- `delete_item` deletes a product on a plain **GET**, with no authentication and no CSRF protection. Reproduced against a test database: product count 1 → 0.
- `ecommerce_items` lists the full catalogue with stock and featured counts to anonymous visitors (HTTP 200).

Redesign:

1. **Remove `delete_item` from `doctorDashboard` entirely.** Product deletion belongs in the Django admin, which already provides a confirmation step, object-level permissions and a `LogEntry` audit trail — none of which a hand-rolled delete URL has. This is a deletion, not a port.
2. `ecommerce_items` becomes **read-only and superuser-only**. It stays purely as a convenience view; anything that mutates a product happens in the Django admin.
3. The sidebar link to it renders only for superusers, so a provider never sees a control they can't use.

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

class Appointment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        CONFIRMED = "confirmed", "Confirmed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No-show"

    class Mode(models.TextChoices):
        IN_PERSON = "in_person", "In-person visit"
        HOME_COLLECTION = "home_collection", "Home sample collection"

    fullName = models.CharField(max_length=100)
    birthDate = models.DateField()
    gender = models.CharField(max_length=20)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    visitType = models.CharField(max_length=20)
    reasonVisit = models.CharField(max_length=100)
    appointmentMode = models.CharField(max_length=20, choices=Mode.choices)  # now REQUIRED + actually collected
    symptoms = models.TextField(blank=True)          # CHANGED: blank=True, matches the UI which never marked it required
    slot = models.OneToOneField(AvailableSlot, on_delete=models.CASCADE)  # CHANGED: FK -> OneToOne (DB-level double-booking guard)
    bookingID = models.CharField(max_length=20, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)  # NEW
    cancelled_at = models.DateTimeField(null=True, blank=True)     # NEW
    cancellation_reason = models.CharField(max_length=255, blank=True)  # NEW
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # `age` field: DROPPED — never collected, always null, dead column.
```

**Why `OneToOneField` on `slot`:** this is the cleanest fix for the double-booking race condition (High #1 in the audit). A `ForeignKey` lets two `Appointment` rows point at the same slot if two requests race between the `is_booked` check and the save; a `OneToOneField` makes that a database-level integrity error, not just an application-level check that can be skipped. The view still does a `select_for_update()`-wrapped check-then-save for a clean user-facing error message, but the DB constraint is the real backstop.

**Why `status` instead of just `AppointmentApproval.is_approved`:** the old boolean can only express "approved" or "not yet," with no way to represent cancelled, completed, or no-show — which is why every "Cancel Appointment" button in both audits turned out to be fake (there was nowhere to persist it). `AppointmentApproval` stays as-is (it's fine, and `doctorDashboard`'s approval flow already writes to it correctly) — `status` becomes the single source of truth for the patient-facing state machine, updated by the same approve action plus new cancel/complete actions.

### 5.1 Constrain the free-text clinical fields

The model sketch above keeps `visitType`, `reasonVisit` and `gender` as bare `CharField`s. **That repeats a mistake this codebase has already paid for.** `CustomUser.role` was free text, and production accumulated doctors with `role='4491'` and `role='1'` — which silently excluded a real doctor from the public booking page, because the query filtered on an exact string. §3 prescribes a lab-appropriate reason-for-visit list; that list has to exist as `choices` on the model, not only as `<option>` tags in a template a POST can bypass:

```python
class VisitType(models.TextChoices):
    NEW_PATIENT = "new_patient", "New patient"
    FOLLOW_UP   = "follow_up",   "Follow-up"

class ReasonForVisit(models.TextChoices):
    LAB_TEST        = "lab_test",        "Lab test / panel"
    REFERRED_TEST   = "referred_test",   "Doctor-referred test"
    RESULT_FOLLOWUP = "result_followup", "Result collection / follow-up"
    HOME_COLLECTION = "home_collection", "Home sample collection"
    OTHER           = "other",           "Other"
```

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

§6 step 2 removes the rating *filters* but never decides the model's fate. It needs one — see **Decision 7**. Recommendation: **drop the model and all three read sites** unless AGKI actually wants to collect post-visit feedback, in which case that is its own small feature (a rating form on a completed appointment), not a template detail.

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
2. **Provider / date selection** (`search_results`) — server-rendered list of active AGKI staff who take appointments, each with real next-5-days slots (this logic is already correct in `views.py` and is kept). Removed: gender/rating/experience filters, "Video Consultation" and "Accepting New Patients" badges (both were hardcoded `true`, never real data), fake "show more slots" button.
3. **Booking form** (`booking_form`) — collects patient info + **required** `appointmentMode` select (In-person / Home collection) + `reasonVisit` from the lab-appropriate choice list (§3). The submit button is a plain HTML form submit — no JS intercepts it. Server-side: wrap slot claim in `select_for_update()`, re-check `is_booked`/existence before saving, return a clear "This slot was just booked by someone else, please pick another" error instead of silently creating a conflicting row.
4. **Confirmation** (`booking_success_page`) — shows the booking ID, and **sends a real confirmation email** (SMTP is already configured in `settings.py` and used elsewhere via `resend_otp`'s `send_mail` call — this reuses the same pattern, no new infrastructure). Sets a short-lived, signed session marker for this specific `bookingID` (see §8) so the patient can immediately view their own detail page without re-entering anything, without exposing a permanently guessable URL.
5. **Trace an appointment** (`booking_id_lookup`) — real server-side lookup. Input is `bookingID` **plus one verification field** (phone number or DOB) — matches the industry-standard "reference + PII" pattern and is what actually closes the IDOR (§8). Rate-limited (see §9.2).
6. **Appointment detail** (`appointment_detail`) — shows real status (from the new `status` field, not a hardcoded "Confirmed" banner), a real cancel action (writes `status=CANCELLED`, frees the slot by unsetting `is_booked` on the related `AvailableSlot`), and drops every button that had no backend (Reschedule, Contact Doctor, Live Chat) until those are actually built — a working "Cancel" beats five fake buttons.

---

## 7. Staff dashboard (redesigned)

All `doctorDashboard` views get `@login_required` + `@role_required(...)` (§4.2), the ecommerce views get the tighter superuser-only treatment from §4.4, and the chrome reads its branding from `SiteSettings` per §3.1. Beyond the auth wall:

- **`doctor_admin_dashboard`**: keep the existing server-computed stats (today/week counts, utilization) — they were correctly written, just needs the sidebar's hardcoded fake badges (`12`, `8`) replaced with the real `pending_count` the view already computes but the template ignores.
- **`available_appointments`**: add a Date column (currently missing — the page shows only time-of-day, useless for a list not scoped to "today"); fix the empty-state copy, which currently says "No appointments scheduled for today" on a page that lists *all* appointments.
- **`doctor_appointment_details`**: keep the real Approve/Add-Note forms (these already work correctly — CSRF, real POST, real persistence). Add a real **Cancel** action (writes `status=CANCELLED`, frees the slot) and a real **Mark Completed** action. Remove Reschedule/Send Message/View Medical History until backed by real views. Fix the mismatched icons/labels in the Personal Information card (wedding-ring icon on "Reason for Visiting," gender icon on "Appointment Mode" — leftovers from the source template's unrelated original fields).
- **`doctor_availability`**: keep the real "Add Availability" form. Add the missing `delete_availability` view/URL — the Delete Slot button in the current template already calls `fetch('/delete-availability/<id>/', {method: 'DELETE'})`; that endpoint just needs to exist (guard: refuse to delete a slot with `is_booked=True`). Remove the fake week prev/next/"Today" navigation buttons or wire them to real query params.
- **`doctor_schedule.html`**: currently 100% hardcoded fake data because its view passes zero context — either delete this page (its content is a duplicate of `doctor_availability`) or give it a real view. Recommendation: **delete it**, it adds no capability `doctor_availability` doesn't already cover.
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

1. **Notifications**: booking confirmation email is mandatory (reuses existing `send_mail`/SMTP config). SMS reminders are a real Tanzanian-market feature (e.g. Africa's Talking) but are **Phase 2** — flag as a decision item, not silently promised in copy the way the old templates did ("You'll receive SMS reminders" with no SMS integration anywhere).
2. **Accessibility**: form labels correctly tied to inputs, required fields marked with `required` **and** visually asterisked consistently (fixes the `symptoms` field mismatch where the backend required it but the UI never indicated that).
3. **Mobile responsiveness**: keep the existing card/grid patterns (they're already responsive), just re-skinned to AGKI branding.
4. **Observability**: none of this currently has logging beyond Django's defaults. Add structured logging on booking creation, cancellation, and staff approval actions — useful both operationally and as an audit trail for a system that handles health data.
5. **Testing**: both apps still have empty `tests.py` files. The CMS work established the pattern to follow (`cms/tests.py`, `app/tests.py`, `useraccount/tests.py` — 80 tests, with shared fixtures in `cms/test_utils.py`), including verifying the tests actually fail when the bug is reintroduced rather than trusting a green run. Minimum bar before this ships:
   - Model tests for the `OneToOneField` / `unique_together` constraints.
   - A test proving the detail page redirects to the lookup form without a valid session marker, and **does not** render PII.
   - A test **enumerating every URL in both apps** and asserting anonymous access redirects to login — enumerated from the URLconf, not a hand-written list, so a new view can't be added without a decision.
   - A test asserting `delete_item` no longer exists as a route (`NoReverseMatch`), so it can't quietly return.
   - A test asserting the `Content Editors` group holds **zero** permissions on `appointments`, `doctorDashboard` or `ecommerce` models (§4.3).
   - A test asserting a provider appears on the booking page based on `role` alone, with no `Profile` visit required (§5.3).
   - A test proving a second booking on an already-booked slot fails.

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
| 7 | **High** — Promised email/SMS never sent | §6 step 4, §9.1 — real `send_mail` on confirmation; SMS flagged Phase 2, not promised in copy until built |
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

1. **Migration 1 (additive, safe):** add `Appointment.status` (default `pending`, backfilled from existing `AppointmentApproval.is_approved` — `True` → `confirmed`, `False`/missing → `pending`), `cancelled_at`, `cancellation_reason`; add `AvailableSlot.Meta.unique_together` (run a pre-migration query to detect/report any existing duplicate slots before the constraint is added — don't let the migration fail silently on dirty data).
2. **Migration 2 (requires care):** change `Appointment.slot` from `ForeignKey` to `OneToOneField`. Safe only because the application logic already enforces one-appointment-per-slot today (just not at the DB level) — verify with a query (`AvailableSlot.objects.annotate(c=Count('appointment')).filter(c__gt=1)`) that zero rows currently violate it before applying.
3. **Migration 3:** drop `Appointment.age`; constrain `CustomUser.role` choices (existing free-text values audited first — anything outside `admin`/`doctor`/`receptionist` gets mapped or flagged).
4. **Template/view rollout:** ship the auth wall (`@login_required` on `doctorDashboard`) and the IDOR fix (§8.1) first and independently of the visual reframing — these are security fixes with no reason to wait on a design pass.
5. **Content/branding pass** (domain reframing, template rebuild) ships after the data/security layer is stable.

---

## 12. Open decisions (need AGKI sign-off before implementation)

**Settled during the review pass** (recorded here so they aren't re-litigated):

- ✅ **Who configures these apps** — superuser only. The CMS's `Content Editors` group gets nothing here. §4.3.
- ✅ **Provider access to ecommerce** — none. `delete_item` is removed outright; the product list becomes read-only and superuser-only. §4.4.

**Still open:**

| # | Decision | Why it can't be assumed | Recommendation |
|---|---|---|---|
| 1 | **Provider terminology** — `role='doctor'` stays in the DB, but should the UI say "Doctor," "Provider," "Consultant"? | Wording appears on every public booking page; a lab's staff are often not doctors | "Provider" in public copy, unless AGKI's patients expect "Doctor" |
| 2 | **Home sample collection** — is this a real AGKI service today? | Shipping it as a bookable mode promises a service that may not exist — the same mistake as the old "Video Consultation" badge | Ship **In-person only** unless confirmed; add the mode when it's real |
| 3 | **SMS reminders** — worth the integration cost now, or is email enough? | Real Tanzanian-market feature, but a paid integration and Phase 2 scope | Email only for now; don't mention SMS in any copy until built |
| 4 | **Receptionist role** — real front-desk staff, or is `admin` + `doctor` enough? | Determines whether `@role_required` needs three roles or two | Skip it until someone actually needs it |
| 5 | **`doctor_schedule.html`** — delete in favour of `doctor_availability`, or keep as a read-only calendar? | It is currently 100% hardcoded fake data with no view context | Delete; `doctor_availability` already covers it |
| **6** | **Shared branding on booking pages** — should these pages read logo/phone/email/address from `cms.SiteSettings`, which **Content Editors can edit**? | This is the exact edge of the §4.3 rule you set. Reading from `SiteSettings` means a Content Editor updating the company phone number also changes the booking-page footer. The alternative is hardcoding, which guarantees the two drift apart the first time anyone edits the admin | **Read from `SiteSettings`.** It is shared company identity, not booking configuration, and one phone number should live in one place. Booking-specific settings stay superuser-only regardless |
| **7** | **`Rating`** — drop the model, or build feedback collection? | Three views compute averages from a table **nothing ever writes to**, so every rating display is permanently "No ratings yet." Leaving it is the worst option: it looks like a feature and is scaffolding | **Drop it.** Revisit as a real post-visit feedback feature if AGKI wants patient ratings |
| **8** | **Booking notifications to staff** — should a new booking email AGKI, not just the patient? | §6 step 4 only sends the patient a confirmation; staff would have to poll the dashboard to notice a booking exists | Send to the `SiteSettings` address, mirroring how the contact form already notifies |
| **9** | **Cancellation window** — may a patient cancel at any time, or is there a cut-off? | `status=CANCELLED` frees the slot for rebooking; with no cut-off someone can cancel minutes before | Allow cancellation up to a configurable number of hours before the slot |
