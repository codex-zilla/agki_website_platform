# AGKI Website CMS — Implementation Plan & Tracker

**Design reference:** [website-cms-design.md](website-cms-design.md)
**Status legend:** 🔲 Not started · 🟡 In progress · ✅ Done · ⛔ Blocked · ⏭️ Skipped (see notes)

How to use this doc: update the **Status** cell as work happens, and always leave a line in **Notes / Remarks** when a step is closed — even a one-liner ("done, no issues"). A blank notes cell on a ✅ step is a smell; a step marked ⛔ must have a note explaining what's blocking it. Treat this file as the log a reviewer (or future-you) reads to understand *why* something was done a certain way, not just *that* it was done.

---

## 0. Pre-flight / safety checklist

This runs on a live site with a real database and real uploaded media (`passenger_wsgi.py` confirms shared-host production deployment) — nothing in Phase 1+ starts until this is clean.

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 0.1 | Confirm which environment this work happens against first (local `db.sqlite3` vs. a staging copy vs. prod) | 🔲 | |
| 0.2 | Create a feature branch off `main` (e.g. `feature/website-cms`) — no direct commits to `main` for this body of work | 🔲 | |
| 0.3 | Back up `db.sqlite3` and `media/` before any migration runs, even locally — cheap insurance | 🔲 | |
| 0.4 | Confirm `pip install` is allowed in this environment / who approves new dependencies (`django-ckeditor-5`, `django-adminsortable2`) | 🔲 | |
| 0.5 | Confirm the dead-code cleanup (previous commit) is merged/stable before layering CMS work on top | ✅ | Cleanup staged this session: `app.Appointment` model, `book_appointment`/`appointment_success`/`signin` views+templates removed, migration `0002_delete_appointment` applied locally. |

---

## Phase 1 — Scaffold the `cms` app

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 1.1 | `python manage.py startapp cms` | 🔲 | |
| 1.2 | Add `'cms'` to `INSTALLED_APPS` in `project/settings.py` | 🔲 | |
| 1.3 | Set `verbose_name` in `cms/apps.py` (e.g. `"Website Content"`) so it reads well in the admin sidebar | 🔲 | |
| 1.4 | Empty `models.py`/`admin.py` committed as a clean starting point | 🔲 | |
| 1.5 | `python manage.py check` passes with the new app registered and empty | 🔲 | |

**Phase gate:** app exists, is registered, project still boots clean before any model is written.

---

## Phase 2 — Data models & migrations

One row per model from the design doc §4. Building and reviewing each individually (rather than one giant migration) makes the eventual `git log` legible and any mistake easy to isolate.

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 2.1 | `SingletonModel` abstract base (`save()` pins `pk=1`, `load()` classmethod) | 🔲 | |
| 2.2 | `SiteSettings` | 🔲 | |
| 2.3 | `HeroContent` + `HeroImpactStat` | 🔲 | |
| 2.4 | `AboutContent` + `CoreValue` | 🔲 | |
| 2.5 | `ProblemSectionContent` + `ProblemCard` + `ProblemStat` | 🔲 | |
| 2.6 | `FeatureRow` (shared by Solutions / Why Us via `category`) | 🔲 | |
| 2.7 | `AwardLogo` | 🔲 | |
| 2.8 | `Testimonial` | 🔲 | |
| 2.9 | `CommunityPhoto` | 🔲 | |
| 2.10 | `NewsPost` | 🔲 | |
| 2.11 | `FAQItem` | 🔲 | |
| 2.12 | `TeamMember` | 🔲 | |
| 2.13 | `ContactMessage` | 🔲 | |
| 2.14 | Upload validators (`FileExtensionValidator`, custom max-size validator) applied to every `ImageField`/`FileField` | 🔲 | |
| 2.15 | `makemigrations cms` — read the generated migration file before applying, don't trust it blindly | 🔲 | |
| 2.16 | `migrate` against local/dev DB, confirm tables via `manage.py dbshell` or shell query | 🔲 | |

**Phase gate:** `manage.py shell` can `.objects.create(...)` and `.load()` every model above without error.

---

## Phase 3 — Admin UX

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 3.1 | `pip install django-ckeditor-5`, add to `INSTALLED_APPS`, wire its URLs, run its migrations | 🔲 | |
| 3.2 | `pip install django-adminsortable2`, add to `INSTALLED_APPS` | 🔲 | |
| 3.3 | `ModelAdmin` for each singleton — hide add/delete, single "change" entry point | 🔲 | |
| 3.4 | `ModelAdmin` for each list model — `SortableAdminMixin`, `list_display`, thumbnail preview via `format_html`, `search_fields` where relevant | 🔲 | |
| 3.5 | `ContactMessage` admin — view/list only for content editors, delete restricted to superusers (per design §5) | 🔲 | |
| 3.6 | Admin branding: `admin.site.site_header`, `site_title`, `index_title` | 🔲 | |
| 3.7 | Manual smoke test as superuser: create one real row in every model, upload one real image, confirm thumbnail preview renders | 🔲 | |

**Phase gate:** a superuser can fully author one complete "test" version of every section from the admin alone, no shell/fixtures needed.

---

## Phase 4 — Permissions

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 4.1 | Create `Content Editors` Django group | 🔲 | |
| 4.2 | Assign add/change/delete/view on every `cms` model to that group (view-only + no-delete on `ContactMessage`, per design §5) | 🔲 | |
| 4.3 | Create one real test user in that group, `is_staff=True`, **not** superuser | 🔲 | |
| 4.4 | Verify: test user can log into `/admin/`, sees only `cms` app models | 🔲 | |
| 4.5 | Verify: test user gets 403/hidden on `appointments`, `doctorDashboard`, `useraccount` admin sections | 🔲 | |

**Phase gate:** least-privilege confirmed by actually logging in as the scoped account, not by reading the permission list.

---

## Phase 5 — Seed data (import today's live content)

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 5.1 | Write `cms/management/commands/seed_home_content.py` using the copy extracted from the current `home.html` (design doc §7.2) | 🔲 | |
| 5.2 | Command copies referenced files from `app/static/app/*` into `media/cms/...` and stores the resulting path on each row | 🔲 | |
| 5.3 | Make it idempotent — safe to re-run against a wiped dev DB | 🔲 | |
| 5.4 | Dry run against local DB, spot-check a handful of rows in the admin against the live site's actual current text | 🔲 | |

**Phase gate:** every model has real AGKI content in it, sourced from the command, zero hand-typing.

---

## Phase 6 — Template rewrite (`home.html`)

Doing this section-by-section against a running dev server, comparing side-by-side with the live site, is safer than one big rewrite-and-pray.

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 6.1 | `app/views.py::home` builds the full context (design §8) | 🔲 | |
| 6.2 | Hero section → loop-driven, `{% if hero %}` guard | 🔲 | |
| 6.3 | About / mission / vision / core values | 🔲 | |
| 6.4 | Problem section (cards + stats) | 🔲 | |
| 6.5 | Solutions + Why Us (`FeatureRow` by category) | 🔲 | |
| 6.6 | Awards logo strip | 🔲 | |
| 6.7 | Testimonials | 🔲 | |
| 6.8 | Community photos | 🔲 | |
| 6.9 | News cards | 🔲 | |
| 6.10 | FAQ accordion | 🔲 | |
| 6.11 | Team carousel | 🔲 | |
| 6.12 | Footer / `SiteSettings` (contact info, socials, copyright) | 🔲 | |
| 6.13 | Visual diff pass: rendered page vs. current live page, section by section — flag any drift | 🔲 | |
| 6.14 | Empty-state pass: temporarily clear one table at a time locally, confirm the page still renders (no 500s, section just disappears) | 🔲 | |

**Phase gate:** rendered output is visually indistinguishable from the current live page, and survives every table being emptied.

---

## Phase 7 — Contact form

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 7.1 | `ContactForm` (ModelForm on `ContactMessage`) | 🔲 | |
| 7.2 | Wire real POST handling in `home` (or split into `contact_submit`, see Phase 8) | 🔲 | |
| 7.3 | `send_mail` notification reusing existing SMTP config | 🔲 | |
| 7.4 | Remove the old JS `e.preventDefault()` / fake-`alert()` handler from `home.html` | 🔲 | |
| 7.5 | Add `{% csrf_token %}`, confirm real submission end-to-end (row created + email sent, checked against console backend in dev) | 🔲 | |

**Phase gate:** a real test submission produces a real `ContactMessage` row and a real outbound email.

---

## Phase 8 — Performance

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 8.1 | Split `home` (cacheable GET) from `contact_submit` (POST, uncached) if not already separated in Phase 7 | 🔲 | |
| 8.2 | `@cache_page(60 * 10)` on `home` | 🔲 | |
| 8.3 | Confirm 10-minute staleness on edits is acceptable to AGKI, or revisit (design §9 flags signal-based invalidation as a Phase 2 option) | 🔲 | |

**Phase gate:** conscious decision made and recorded, not a default left unexamined.

---

## Phase 9 — Testing

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 9.1 | `SingletonModel` — concurrent `.load()` calls never create a second row | 🔲 | |
| 9.2 | `home` view renders 200 with every `cms` table empty | 🔲 | |
| 9.3 | `home` view renders 200 with seeded data, spot-checks key strings in the response | 🔲 | |
| 9.4 | Content-editor group: reaches `cms` admin, 403 on the other three apps' admin pages | 🔲 | |
| 9.5 | Contact form: valid POST creates a `ContactMessage` and triggers `send_mail` (mocked) | 🔲 | |
| 9.6 | Full test suite run, not just the new tests — confirm nothing else regressed | 🔲 | |

**Phase gate:** green test suite, not just "it looked right in the browser."

---

## Phase 10 — Rollout & handoff

| # | Task | Status | Notes / Remarks |
|---|---|---|---|
| 10.1 | Deploy branch to staging (if one exists) — otherwise this *is* the careful path to prod | 🔲 | |
| 10.2 | Fresh backup of production `db.sqlite3` + `media/` immediately before running migrations there | 🔲 | |
| 10.3 | Run `migrate` + `seed_home_content` on production | 🔲 | |
| 10.4 | Confirm production media persists correctly across the app's deploy process (Passenger/cPanel-style hosting — verify `media/` isn't inside a directory that gets wiped/replaced on deploy) | 🔲 | |
| 10.5 | Create the first real AGKI staff "Content Editor" account | 🔲 | |
| 10.6 | Walk one AGKI staff member through: publish a news post, reorder the team carousel, edit the hero lede — acceptance test by the actual end user, not by us | 🔲 | |
| 10.7 | Once signed off, merge feature branch to `main` | 🔲 | |
| 10.8 | Separate follow-up ticket: set up a recurring backup job for `media/` (design §7.4 — content images no longer live in git after this) | 🔲 | |

**Phase gate:** an AGKI staff member has independently made a real content change on the live site with no developer involved — that's the actual definition of "done" for this project.

---

## Decisions log

Running record of anything decided along the way that isn't already in the design doc — keeps this from becoming tribal knowledge.

| Date | Decision | Made by | Why |
|---|---|---|---|
| | | | |

---

## Open risks / carry-forward notes

Free-form space for anything that comes up mid-implementation that doesn't fit a checklist row — e.g. a package version conflict, a hosting quirk discovered in Phase 10, a piece of scope that got cut.

-
