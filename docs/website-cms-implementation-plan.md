# AGKI Website CMS — Implementation Plan & Tracker

**Design reference:** [website-cms-design.md](website-cms-design.md)
**Status legend:** 🔲 Not started · 🟡 In progress · ✅ Done · ⛔ Blocked · ⏭️ Skipped (see notes)

How to use this doc: update the **Status** cell as work happens, and always leave a line in **Notes / Remarks** when a step is closed — even a one-liner ("done, no issues"). A blank notes cell on a ✅ step is a smell; a step marked ⛔ must have a note explaining what's blocking it. Treat this file as the log a reviewer (or future-you) reads to understand *why* something was done a certain way, not just *that* it was done.

---

## 0. Pre-flight / safety checklist

This runs on a live site with a real database and real uploaded media (`passenger_wsgi.py` confirms shared-host production deployment) — nothing in Phase 1+ starts until this is clean.

| #   | Task                                                                                                                                     | Status | Notes / Remarks                                                                                                                                                                                                                                                                                                                           |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.1 | Confirm which environment this work happens against first (local`db.sqlite3` vs. a staging copy vs. prod)                              | ✅     | Confirmed by AGKI: this is local dev only. The local`db.sqlite3` contains a copy of real-looking data but is **not** the deployed instance — a separate copy is already live in production. Lowers the stakes of local migrate/seed mistakes, but production still gets its own backup + careful rollout at Phase 10 regardless. |
| 0.2 | Create a feature branch off`main` (e.g. `feature/website-cms`) — no direct commits to `main` for this body of work                | ✅     | Branch`feature/website-cms` created off `main` after the dead-code cleanup + docs were committed to `main` directly (that cleanup was standalone and predates this feature). All Phase 1+ work happens on this branch.                                                                                                              |
| 0.3 | Back up`db.sqlite3` and `media/` before any migration runs, even locally — cheap insurance                                          | ✅     | Both copied to a local scratch backup directory (`db.sqlite3` 444K, `media/` 6.4M — trivial size). **Caveat:** that scratch directory is session-temporary, not durable storage — treat this as a same-session safety net only, not a real backup. Production backup at Phase 10 (10.2) needs an actual durable location.     |
| 0.4 | Confirm`pip install` is allowed in this environment / who approves new dependencies (`django-ckeditor-5`, `django-admin-sortable2`) | ✅     | Approved by AGKI — both packages added to`requirements.txt` at Phase 3 (not installed yet; no need to pull them in before the app that uses them exists).                                                                                                                                                                              |
| 0.5 | Confirm the dead-code cleanup (previous commit) is merged/stable before layering CMS work on top                                         | ✅     | Committed to`main` as `17c4bf7` (code) and `42ced9e` (docs) before branching. `app.Appointment` model, `book_appointment`/`appointment_success`/`signin` views+templates removed, migration `0002_delete_appointment` applied locally.                                                                                    |

---

## Phase 1 — Scaffold the `cms` app

| #   | Task                                                                                                      | Status | Notes / Remarks                                                       |
| --- | --------------------------------------------------------------------------------------------------------- | ------ | --------------------------------------------------------------------- |
| 1.1 | `python manage.py startapp cms`                                                                         | ✅     | Scaffolded on`feature/website-cms`.                                 |
| 1.2 | Add`'cms'` to `INSTALLED_APPS` in `project/settings.py`                                             | ✅     | Added after`'ecommerce'`, before the `allauth` block.             |
| 1.3 | Set`verbose_name` in `cms/apps.py` (e.g. `"Website Content"`) so it reads well in the admin sidebar | ✅     | Set to`"Website Content"`.                                          |
| 1.4 | Empty`models.py`/`admin.py` committed as a clean starting point                                       | ✅     | `startapp` defaults left untouched — nothing to add until Phase 2. |
| 1.5 | `python manage.py check` passes with the new app registered and empty                                   | ✅     | "System check identified no issues (0 silenced)."                     |

**Phase gate:** app exists, is registered, project still boots clean before any model is written. ✅ **Met.**

---

## Phase 2 — Data models & migrations

One row per model from the design doc §4. Building and reviewing each individually (rather than one giant migration) makes the eventual `git log` legible and any mistake easy to isolate.

| #    | Task                                                                                                                    | Status | Notes / Remarks |
| ---- | ----------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 2.1  | `SingletonModel` abstract base (`save()` pins `pk=1`, `load()` classmethod)                                     | ✅     | Also overrode `delete()` as a no-op — a deleted singleton row leaves nothing for `home` to render and `.load()` can't resurrect it mid-request, so deletion from the admin is disabled by design rather than by convention. |
| 2.2  | `SiteSettings`                                                                                                        | ✅     | Checkpoint A. |
| 2.3  | `HeroContent` + `HeroImpactStat`                                                                                    | ✅     | Checkpoint A. |
| 2.4  | `AboutContent` + `CoreValue`                                                                                        | ✅     | Checkpoint A. |
| 2.5  | `ProblemSectionContent` + `ProblemCard` + `ProblemStat`                                                           | ✅     | Checkpoint A. |
| 2.6  | `FeatureRow` (shared by Solutions / Why Us via `category`)                                                          | ✅     | Checkpoint B. |
| 2.7  | `AwardLogo`                                                                                                           | ✅     | Checkpoint B. |
| 2.8  | `Testimonial`                                                                                                         | ✅     | Checkpoint B. **Deviation from design doc**, also corrected there: `youtube_url` changed to `blank=True` (was incorrectly required) with a `clean()` method requiring at least one of `youtube_url`/`video_file` — the design as originally written would have blocked the video-file-upload escape hatch it was supposed to enable. |
| 2.9  | `CommunityPhoto`                                                                                                      | ✅     | Checkpoint B. |
| 2.10 | `NewsPost`                                                                                                            | ✅     | Checkpoint C. |
| 2.11 | `FAQItem`                                                                                                             | ✅     | Checkpoint C. |
| 2.12 | `TeamMember`                                                                                                          | ✅     | Checkpoint C. |
| 2.13 | `ContactMessage`                                                                                                      | ✅     | Checkpoint C. Written to by the public contact form (Phase 7), not staff-authored like the rest — lives in the same app/admin per design, flagged so its different lifecycle isn't mistaken for an oversight. |
| 2.14 | Upload validators (`FileExtensionValidator`, custom max-size validator) applied to every `ImageField`/`FileField` | ✅     | New `cms/validators.py`: extension allowlists (images: jpg/jpeg/png/webp, video: mp4/webm) + max-size validators (images 5MB, video 50MB). Applied to all 8 image/video fields. Deliberately *not* doing content/magic-byte sniffing — documented in the module docstring as an accepted tradeoff since uploads only come from trusted staff accounts, not the public. |
| 2.15 | `makemigrations cms` — read the generated migration file before applying, don't trust it blindly                     | ✅     | `cms/migrations/0001_initial.py` — 16 `CreateModel` ops (13 content models + `HeroImpactStat`/`CoreValue` FK tables). Read in full: FKs, `CASCADE`, `related_name`, and validators all serialized correctly. No destructive operations. |
| 2.16 | `migrate` against local/dev DB, confirm tables via `manage.py dbshell` or shell query                               | ✅     | Applied clean. Followed by a shell smoke test: every model created successfully, singleton `.load()` confirmed stable under a second call (no duplicate row), `Testimonial.clean()` confirmed it rejects a row with neither `youtube_url` nor `video_file`. Test rows deleted afterward — singleton rows (`SiteSettings`/`HeroContent`/`AboutContent`/`ProblemSectionContent`) left in place with default field values, ready for Phase 5 seeding. |

**Phase gate:** `manage.py shell` can `.objects.create(...)` and `.load()` every model above without error. ✅ **Met** — see 2.16.

---

## Phase 3 — Admin UX

| #   | Task                                                                                                                                                    | Status | Notes / Remarks |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 3.1 | `pip install django-ckeditor-5`, add to `INSTALLED_APPS`, wire its URLs, run its migrations                                                         | ✅     | Installed `django-ckeditor-5==0.2.20`. No models/migrations of its own (`CKEditor5Field.get_internal_type()` is `"TextField"` — pure widget, not a new DB concept). Wired `path('ckeditor5/', include('django_ckeditor_5.urls'))` for its image-upload endpoint (defaults to staff-only per its own `permissions.py`). Applied `CKEDITOR_5_CONFIGS["default"]` with a small toolbar (heading/bold/italic/link/lists/quote — matches what About copy, News body, and FAQ answers actually need). Swapped those 3 fields from `TextField` to `CKEditor5Field` in `cms/models.py`, migration `0002_alter_...` reviewed (3 `AlterField` ops, non-destructive) and applied. |
| 3.2 | `pip install django-admin-sortable2`, add to `INSTALLED_APPS`                                                                                        | ✅     | **Correction**: the design doc had the wrong PyPI name (`django-adminsortable2`, no hyphen before "sortable2") — install failed with "no matching distribution." Real package is `django-admin-sortable2==2.3.1`; import path is still `adminsortable2`. Fixed in both docs and `requirements.txt`. |
| 3.3 | `ModelAdmin` for each singleton — hide add/delete, single "change" entry point                                                                       | ✅     | `SingletonModelAdmin` base class: `has_add_permission`/`has_delete_permission` return `False`, `changelist_view` redirects straight to the one row's change page via `Model.load()`. Applied to `SiteSettings`, `HeroContent`, `AboutContent`, `ProblemSectionContent`. |
| 3.4 | `ModelAdmin` for each list model — `SortableAdminMixin`, `list_display`, thumbnail preview via `format_html`, `search_fields` where relevant | ✅     | All 10 list models registered. Shared `image_thumbnail(field_name)` helper builds the preview column once, reused everywhere — and guards `FieldFile.url` (the exact unguarded-`.image.url` crash found twice in the appointments/doctorDashboard audits) so a row with no file just shows "—" instead of erroring. **Implementation improvement over the design doc as literally written**: `HeroImpactStat` and `CoreValue` are edited as `SortableTabularInline`s on their parent singleton's page (Hero / About) rather than as separate top-level admin entries — they only ever exist attached to that one parent row, so a standalone changelist for them would be one more click for no benefit. Required making the parent singleton admins also inherit `adminsortable2`'s `SortableAdminBase` (not the full `SortableAdminMixin`) — caught by `manage.py check`, which asserts on this. `FeatureRow` (shared by two sections) gets `list_filter = ("category",)` plus an explicit admin-level `ordering = ("order",)` — needed because adminsortable2 reads `ordering[0]` to pick its drag field, and the model's own `Meta.ordering` is `("category", "order")` for correct queryset behavior elsewhere; without the override it would have tried to drag-sort by category. |
| 3.5 | `ContactMessage` admin — view/list only for content editors, delete restricted to superusers (per design §5)                                        | ✅     | `has_add_permission` always `False` (only the public form creates these), all fields `readonly_fields` except `is_read`, `has_delete_permission` gated on `request.user.is_superuser`. |
| 3.6 | Admin branding:`admin.site.site_header`, `site_title`, `index_title`                                                                              | ✅     | "AGKI Content Manager" — set at the bottom of `cms/admin.py` after all registrations. |
| 3.7 | Manual smoke test as superuser: create one real row in every model, upload one real image, confirm thumbnail preview renders                            | ✅     | Done as a scripted Django-test-client walkthrough (logged in via `force_login` against an existing real superuser account, no password needed/touched) rather than a literal human click-through: admin index + branding, singleton redirect + blocked add on all 4, changelist + add-form 200s across all 10 list models, `ContactMessage` add blocked, both sortable inlines confirmed present on their parent pages. Then a full round-trip: POSTed a real in-memory PNG through the `TeamMember` add form, confirmed it saved to `cms/team/`, and confirmed the changelist thumbnail rendered via `.url` correctly (not the unguarded-crash pattern from the earlier audits) — test row + file cleaned up after. **Recommend AGKI still does one real human pass in the browser when convenient** — this proves the mechanics work, not that the UI feels good to a non-technical editor. |

**Phase gate:** a superuser can fully author one complete "test" version of every section from the admin alone, no shell/fixtures needed. ✅ **Met** — see 3.7.

---

## Phase 4 — Permissions

| #   | Task                                                                                                                            | Status | Notes / Remarks |
| --- | ------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 4.1 | Create`Content Editors` Django group                                                                                          | ✅     | Built as a **data migration** (`cms/migrations/0003_content_editors_group.py`), not a one-off shell/admin action — so it exists automatically in every environment this ships to, prod included, without anyone having to remember to click it together by hand. Reversible (`RunPython` has a matching reverse function that deletes the group). |
| 4.2 | Assign add/change/delete/view on every`cms` model to that group (view-only + no-delete on `ContactMessage`, per design §5) | ✅     | 54 permissions total: full add/change/delete/view on the 11 "authored content" models; view+change only on the 4 singletons (add/delete are pointless to grant — `SingletonModelAdmin` blocks them unconditionally regardless of the permission bit) and on `ContactMessage` (add always blocked, delete superuser-only, per `ContactMessageAdmin`). Verified via shell query, matches design exactly. **Implementation subtlety worth flagging**: used `get_or_create()` for the `ContentType`/`Permission` lookups instead of `.get()`/`.filter()`. Both are normally created by Django's `post_migrate` signal, which only fires after *all* migrations in a `migrate` run finish — not incrementally per app. On a brand-new environment where this migration runs in the same invocation as the ones that create the `cms` models, a plain `.get()` would've found nothing yet and silently produced an **empty** permission set (no error, just a group with zero permissions) — a real hazard for the Phase 10 production rollout, where this exact scenario happens. `get_or_create()` sidesteps it entirely; full reasoning is in the migration file's module docstring. |
| 4.3 | Create one real test user in that group,`is_staff=True`, **not** superuser                                              | ✅     | `content_editor_dev` / `dev-only-not-for-prod` created locally for verification — **explicitly a throwaway dev-only account**, not the real AGKI staff account (that's Phase 10.5, against production, with real credentials handled properly then). |
| 4.4 | Verify: test user can log into`/admin/`, sees only `cms` app models                                                         | ✅     | Scripted `Client`-based walkthrough logged in as `content_editor_dev`: admin index loads, all 8 non-singleton `cms` changelists return 200, singleton redirect still works, `ContactMessage` changelist viewable but add correctly 403s. |
| 4.5 | Verify: test user gets 403/hidden on`appointments`, `doctorDashboard`, `useraccount` admin sections                       | ✅     | `appointments/appointment`, `appointments/availableslot`, `useraccount/customuser`, `useraccount/profile` changelists all correctly return 403 for the content editor. Admin index HTML also confirmed to not even render links to those models (not just blocked-if-guessed). `doctorDashboard` has no models registered in Django admin at all (its `admin.py` is empty) — nothing to test there, it's inaccessible by not existing. |

**Phase gate:** least-privilege confirmed by actually logging in as the scoped account, not by reading the permission list. ✅ **Met** — see 4.4/4.5.

---

## Phase 5 — Seed data (import today's live content)

| #   | Task                                                                                                                             | Status | Notes / Remarks |
| --- | -------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 5.1 | Write`cms/management/commands/seed_home_content.py` using the copy extracted from the current `home.html` (design doc §7.2) | 🔲     |                 |
| 5.2 | Command copies referenced files from`app/static/app/*` into `media/cms/...` and stores the resulting path on each row        | 🔲     |                 |
| 5.3 | Make it idempotent — safe to re-run against a wiped dev DB                                                                      | 🔲     |                 |
| 5.4 | Dry run against local DB, spot-check a handful of rows in the admin against the live site's actual current text                  | 🔲     |                 |

**Phase gate:** every model has real AGKI content in it, sourced from the command, zero hand-typing.

---

## Phase 6 — Template rewrite (`home.html`)

Doing this section-by-section against a running dev server, comparing side-by-side with the live site, is safer than one big rewrite-and-pray.

| #    | Task                                                                                                                               | Status | Notes / Remarks |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 6.1  | `app/views.py::home` builds the full context (design §8)                                                                        | 🔲     |                 |
| 6.2  | Hero section → loop-driven,`{% if hero %}` guard                                                                                | 🔲     |                 |
| 6.3  | About / mission / vision / core values                                                                                             | 🔲     |                 |
| 6.4  | Problem section (cards + stats)                                                                                                    | 🔲     |                 |
| 6.5  | Solutions + Why Us (`FeatureRow` by category)                                                                                    | 🔲     |                 |
| 6.6  | Awards logo strip                                                                                                                  | 🔲     |                 |
| 6.7  | Testimonials                                                                                                                       | 🔲     |                 |
| 6.8  | Community photos                                                                                                                   | 🔲     |                 |
| 6.9  | News cards                                                                                                                         | 🔲     |                 |
| 6.10 | FAQ accordion                                                                                                                      | 🔲     |                 |
| 6.11 | Team carousel                                                                                                                      | 🔲     |                 |
| 6.12 | Footer /`SiteSettings` (contact info, socials, copyright)                                                                        | 🔲     |                 |
| 6.13 | Visual diff pass: rendered page vs. current live page, section by section — flag any drift                                        | 🔲     |                 |
| 6.14 | Empty-state pass: temporarily clear one table at a time locally, confirm the page still renders (no 500s, section just disappears) | 🔲     |                 |

**Phase gate:** rendered output is visually indistinguishable from the current live page, and survives every table being emptied.

---

## Phase 7 — Contact form

| #   | Task                                                                                                                           | Status | Notes / Remarks |
| --- | ------------------------------------------------------------------------------------------------------------------------------ | ------ | --------------- |
| 7.1 | `ContactForm` (ModelForm on `ContactMessage`)                                                                              | 🔲     |                 |
| 7.2 | Wire real POST handling in`home` (or split into `contact_submit`, see Phase 8)                                             | 🔲     |                 |
| 7.3 | `send_mail` notification reusing existing SMTP config                                                                        | 🔲     |                 |
| 7.4 | Remove the old JS`e.preventDefault()` / fake-`alert()` handler from `home.html`                                          | 🔲     |                 |
| 7.5 | Add`{% csrf_token %}`, confirm real submission end-to-end (row created + email sent, checked against console backend in dev) | 🔲     |                 |

**Phase gate:** a real test submission produces a real `ContactMessage` row and a real outbound email.

---

## Phase 8 — Performance

| #   | Task                                                                                                                                    | Status | Notes / Remarks |
| --- | --------------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 8.1 | Split`home` (cacheable GET) from `contact_submit` (POST, uncached) if not already separated in Phase 7                              | 🔲     |                 |
| 8.2 | `@cache_page(60 * 10)` on `home`                                                                                                    | 🔲     |                 |
| 8.3 | Confirm 10-minute staleness on edits is acceptable to AGKI, or revisit (design §9 flags signal-based invalidation as a Phase 2 option) | 🔲     |                 |

**Phase gate:** conscious decision made and recorded, not a default left unexamined.

---

## Phase 9 — Testing

| #   | Task                                                                                     | Status | Notes / Remarks |
| --- | ---------------------------------------------------------------------------------------- | ------ | --------------- |
| 9.1 | `SingletonModel` — concurrent `.load()` calls never create a second row             | 🔲     |                 |
| 9.2 | `home` view renders 200 with every `cms` table empty                                 | 🔲     |                 |
| 9.3 | `home` view renders 200 with seeded data, spot-checks key strings in the response      | 🔲     |                 |
| 9.4 | Content-editor group: reaches`cms` admin, 403 on the other three apps' admin pages     | 🔲     |                 |
| 9.5 | Contact form: valid POST creates a`ContactMessage` and triggers `send_mail` (mocked) | 🔲     |                 |
| 9.6 | Full test suite run, not just the new tests — confirm nothing else regressed            | 🔲     |                 |

**Phase gate:** green test suite, not just "it looked right in the browser."

---

## Phase 10 — Rollout & handoff

| #    | Task                                                                                                                                                                                         | Status | Notes / Remarks |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------- |
| 10.1 | Deploy branch to staging (if one exists) — otherwise this*is* the careful path to prod                                                                                                    | 🔲     |                 |
| 10.2 | Fresh backup of production`db.sqlite3` + `media/` immediately before running migrations there                                                                                            | 🔲     |                 |
| 10.3 | Run`migrate` + `seed_home_content` on production                                                                                                                                         | 🔲     |                 |
| 10.4 | Confirm production media persists correctly across the app's deploy process (Passenger/cPanel-style hosting — verify`media/` isn't inside a directory that gets wiped/replaced on deploy) | 🔲     |                 |
| 10.5 | Create the first real AGKI staff "Content Editor" account                                                                                                                                    | 🔲     |                 |
| 10.6 | Walk one AGKI staff member through: publish a news post, reorder the team carousel, edit the hero lede — acceptance test by the actual end user, not by us                                  | 🔲     |                 |
| 10.7 | Once signed off, merge feature branch to`main`                                                                                                                                             | 🔲     |                 |
| 10.8 | Separate follow-up ticket: set up a recurring backup job for`media/` (design §7.4 — content images no longer live in git after this)                                                     | 🔲     |                 |

**Phase gate:** an AGKI staff member has independently made a real content change on the live site with no developer involved — that's the actual definition of "done" for this project.

---

## Decisions log

Running record of anything decided along the way that isn't already in the design doc — keeps this from becoming tribal knowledge.

| Date       | Decision                                                                                 | Made by | Why                                                                                                                                                                                                                            |
| ---------- | ---------------------------------------------------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 2026-08-05 | Local`db.sqlite3`/`media/` are a disposable dev copy, not the production data source | AGKI    | Production runs a separate, already-deployed copy — de-risks local migration/seed experimentation, but does not change the Phase 10 production rollout care (backup, staged migrate) since that still touches the real thing. |
| 2026-08-05 | Approved adding`django-ckeditor-5` and `django-admin-sortable2` as new dependencies   | AGKI    | Small, actively maintained, narrowly-scoped admin add-ons; avoids hand-building rich text/reordering widgets from scratch.                                                                                                     |
| 2026-08-05 | Corrected package name typo: `django-adminsortable2` doesn't exist on PyPI, the real package is `django-admin-sortable2` | Claude | `pip install django-adminsortable2` failed with no matching distribution during Phase 3; caught before it silently blocked the whole install command (pip resolves all requirements before installing any, so the ckeditor install failed alongside it on the first attempt). |

---

## Open risks / carry-forward notes

Free-form space for anything that comes up mid-implementation that doesn't fit a checklist row — e.g. a package version conflict, a hosting quirk discovered in Phase 10, a piece of scope that got cut.
