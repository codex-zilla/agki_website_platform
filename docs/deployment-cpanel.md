# Deploying to cPanel — runbook

**Target:** `agkimedicallaboratory.co.tz`, cPanel + Passenger, Python 3.12
**Deploying:** `main` — the CMS and website work. **Not** the appointment redesign; see §7.
**Status:** written 2026-08-17, after rehearsing the data migration against a copy of the live database

> **Read this first — which working tree you upload is the whole decision.**
> There is no git integration on the server, so deployment is a manual upload of whatever is
> checked out on your machine. That makes it easy to ship the wrong thing without noticing.
>
> **Check out `chore/cpanel-deployment` before building the bundle.** It is `main` plus the
> MySQL support, and nothing else. The branch you have been working on,
> `feature/appointments-redesign`, additionally contains the whole appointment redesign
> (Phases 0–3) — uploading that tree would deploy work you decided to hold back (§7).
>
> The bundle script prints enough to check this: **17 files under `appointments/migrations/`
> is correct** (up to `0016`). If you see 25, you are on the wrong branch.
>
> Merging this branch into `main` is worth doing for the repository's sake, but it is not a
> deployment step and nothing here depends on it.

Every command below has been run against `deployed_copy_db.sqlite3` — a copy of the live
database — rather than written from memory. Where something failed during that rehearsal it
is called out, because those are the parts most likely to fail again.

---

## 1. What production actually looks like today

Read out of the live database copy, not assumed:

|                      |                                                                                                                                                                                          |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Accounts             | **6.** One superuser (`mccoln`), five doctors.                                                                                                                                   |
| `role` values      | Two are**garbage**: `Kyla` has `role='1'`, `Dr. Magembe` has `role='4491'`. Both are silently excluded from the public doctor list, because it filters on an exact string. |
| Profiles             | **4 of 6.** The two without one are invisible to booking regardless of role.                                                                                                       |
| Availability slots   | 25, including**one duplicate pair** (same doctor, date and time).                                                                                                                  |
| Appointments         | **Zero.** No patient data on production at all.                                                                                                                                    |
| Ratings              | 2 rows, added by hand in the admin, attached to no visit.                                                                                                                                |
| `cms` tables       | **None. The CMS has never been deployed.**                                                                                                                                         |
| Migration state      | `useraccount` at 0006, `appointments` at 0016, `cms` absent.                                                                                                                       |
| Sessions / admin log | 110 / 115 rows.                                                                                                                                                                          |

**The single most useful fact here: there are no appointments.** Nothing on production holds
patient data yet, so this deployment is far lower-risk than it would be in three months. That
is an argument for doing it now rather than later.

---

## 2. Six things that will bite

These are the ones that cost time if you meet them for the first time at 11pm.

**1. `DJANGO_ENVIRONMENT` is probably not set, and everything hangs off it.**
`settings.py` decides what "production" means from that one variable. Without it, `IS_PRODUCTION`
is false and the site silently runs in development mode: **the email backend prints to a log
instead of sending**, required settings stop being enforced, and `DEBUG` defaults to on. If
password-reset or OTP emails have "not been arriving", this is the first thing to check. Note
that `.env.prod` in the repo does **not** contain it.

**2. `.env.prod` is a template, not a config.** It carries `DATABASE_URL=`, `EMAIL_HOST_USER=`
and `EMAIL_HOST_PASSWORD=` all empty, and a `django-insecure-…` secret key that was generated
for development. **Generate a new secret key for production** (step C3). The file is gitignored,
so nothing secret is in the repository — but a dev key deployed to production is a real one.

**3. Django serves neither `/static/` nor `/media/` when `DEBUG=False`.** Look at the end of
`project/urls.py`: `static()` returns an empty list in production, by design. Apache has to
serve both. If images vanish the moment `DEBUG` goes off, this is why — and it matters more
than usual here, because the CMS is a system for uploading images.

**4. `media/` is not in git, and part of it exists only on the server.** All CMS content
images live there, and so do six files the imported database points at that are **not** in the
upload bundle because the server already has them (§4.1). Two consequences: extract the bundle
*over* the existing directory rather than replacing it, and make sure `media/` survives every
future deploy — if your process ever replaces the application directory wholesale, move it out
with `DJANGO_MEDIA_ROOT`.

**5. Importing accounts used to deactivate them.** `useraccount/signals.py` treats every
newly saved account as a public self-signup: it forces `is_active=False` and emails a
verification code. `loaddata` saves rows, so importing the production users into the new
database tripped it — the rehearsal came out with **five of six accounts deactivated** and
four spurious OTP tokens, and with live SMTP would have emailed five real people a code they
never asked for. Fixed here by having the signal skip when Django flags the save as fixture
loading (`raw=True`), which is what that flag is for. **If you are importing with an older
checkout, check `is_active` afterwards.**

**6. A failed migration stops the run partway.** During the rehearsal, `migrate` applied four
appointment migrations, hit a data problem, and stopped — **before** it reached `useraccount`
or `cms` at all. Each migration is atomic; the run as a whole is not. That is normal and
recoverable (fix the data, re-run), but it is alarming if you are not expecting it, and it is
why Part A migrates a *copy* first. Nothing on `main` has a data check that can fail this way —
the migrations that do arrive with the appointment work — but the habit is the point.

---

## 3. The database: yes, use cPanel's MySQL

Your instinct is right, and there is a harder reason than convenience.

- **SQLite cannot do `SELECT … FOR UPDATE`.** Django's SQLite backend reports
  `has_select_for_update = False` (verified). Phase 4 of the appointment work claims an
  availability slot with `select_for_update()` so two patients cannot take the same slot — on
  SQLite that raises `NotSupportedError`. The fix for double-booking **cannot ship on SQLite.**
- **Passenger runs several worker processes against one file.** Concurrent writes to SQLite
  produce `database is locked`. The site already writes on ordinary traffic (sessions, admin
  log), and the appointment work adds more: the rate limiter writes a row per lookup attempt,
  and the notification outbox writes one per message.
- **Backups become a cPanel feature** rather than "remember to copy a file".

MySQL/MariaDB is what cPanel offers, and `dj-database-url` already reads a `DATABASE_URL`, so
the code change was small: a driver in `requirements.txt`, a fallback shim in
`project/__init__.py`, and two `OPTIONS` in `settings.py` (`utf8mb4`, and MySQL's strict mode —
without it MySQL silently truncates a value too long for its column).

**Do it during this deployment, not after.** Right now there are no appointments to move.

---

## 4. Runbook

### Part 0 — back up what is there now

Zipping the whole application directory on the server is the right instinct: it makes rollback
a single restore rather than a reconstruction. Do both halves.

1. **cPanel → File Manager**, select the application directory, **Compress → zip**. Download
   it. That captures the code, the media, *and* the current `.env` if one exists — which is
   also how you find out what environment variables the site is really running with (§2.1).
2. **Download `db.sqlite3` separately** as well. It is inside that zip, but you need it as a
   loose file for Part A, and the two backups serve different purposes: the zip is the
   rollback, the loose database is the input to the migration.

Keep both somewhere durable, off the server. They are the only thing standing between a bad
deploy and a bad afternoon.

### Part A — prepare the data (on your machine)

The order here matters, and is the one thing in this document that is genuinely easy to get
wrong.

**Migrate the SQLite copy first, then export.** Not the other way round. Two of the migrations
are data cleanups that find rows *by username* — `useraccount 0007` is what turns
`role='4491'` back into `role='doctor'`. If you create an empty MySQL database, run `migrate`
there, and then import the old data, that cleanup runs against an empty table and the garbage
values come back with the import. Migrating the SQLite copy first means the cleanup runs while
the rows it is looking for are actually there.

```bash
# A1. Take the live database and media off the server first. Keep them somewhere durable;
#     this is the rollback.
#     cPanel → File Manager, or:
#     scp user@host:~/app/db.sqlite3  ./backups/prod-$(date +%F).sqlite3
#     scp -r user@host:~/app/media    ./backups/media-$(date +%F)

# A2. Work on a copy. Never on the file you just downloaded.
cp ./backups/prod-$(date +%F).sqlite3 ./backups/rehearsal.sqlite3
export DATABASE_URL="sqlite:///$(pwd)/backups/rehearsal.sqlite3"

# A3. Migrate the copy. Expect useraccount 0007 to print two role fixes:
#       useraccount 0007: set role='doctor' for username='Dr. Magembe'
#       useraccount 0007: set role='doctor' for username='Kyla'
#     If it prints "not found, skipped" instead you are pointed at the wrong
#     database - that message means it could not find the accounts it exists to fix.
python manage.py migrate

# A4. Confirm the cleanup landed and the CMS tables exist.
python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','project.settings'); django.setup()
from useraccount.models import CustomUser, Profile
from django.contrib.auth.models import Group
print('roles   :', sorted({u.role for u in CustomUser.objects.all()}))
print('profiles:', Profile.objects.count(), 'of', CustomUser.objects.count())
print('editors :', Group.objects.get(name='Content Editors').permissions.count(), 'permissions')
"
```

Expected: `roles: ['doctor']` — no `'1'`, no `'4491'` — **profiles 4 of 6**, 54 permissions.
Those are the numbers the rehearsal actually produced. Two accounts keep no profile: the
backfill that gives everyone one arrives with the appointment work, not with this.

**Two known data problems are deliberately left alone**, because nothing on `main` touches
them: the duplicate slot pair, and the two orphan `Rating` rows. They matter only to the
appointment work, whose migrations refuse to run until somebody resolves them. Leaving them
here is the right call — do not tidy production data that this deployment does not need.

The export and import below have both been run end to end against this data: 164 objects
out, 164 objects in, accounts intact.

```bash
# A5. Export. The exclusions matter - see below.
python manage.py dumpdata \
  --natural-foreign --natural-primary \
  --exclude contenttypes --exclude auth.permission --exclude auth.group \
  --exclude sessions.session \
  --indent 2 -o ./backups/prod_data.json
```

Why each exclusion:

| Excluded                              | Because                                                                                                                                                                                                           |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contenttypes`, `auth.permission` | Recreated by`migrate` on the new database. Importing them collides on ids. `--natural-foreign` is what lets everything else still point at them correctly.                                                    |
| `auth.group`                        | `migrate` creates the **Content Editors** group itself (`cms` migration 0003, 54 permissions). Importing a second copy collides on the unique name. Production has no other groups, so nothing is lost. |
| `sessions.session`                  | 110 rows of expiring cookies. Everyone signs in again; that is fine.                                                                                                                                              |

### 4.1 What goes up, and what stays behind

`scripts/make_upload_bundle.py` is the authority; this is what it decides and why.

**Uploaded** — the seven Django apps (including their `static/` *source* directories),
`templates/`, `manage.py`, `passenger_wsgi.py`, `requirements.txt`, `.env.example`, and two
media directories.

**Not uploaded:**

| Left out | Why |
| --- | --- |
| `.git/` | 72 MB of history the server has no use for |
| `.venv/` | Platform-specific. The cPanel Python App builds its own |
| `static/` | **63 MB of `collectstatic` output.** Regenerate it on the server (C5) — the sources travel inside each app, so uploading this would be shipping the same files twice |
| `design/`, `docs/` | Mockups and reference material; never imported at runtime |
| `backups/`, `db.sqlite3`, `deployed_copy_db.sqlite3` | Development and production database copies. The bundle script refuses to build if any appears |
| `.env`, `.env.prod` | Development secrets. You create the real `.env` on the server (C2) |
| `__pycache__/`, `*.pyc` | Compiled for a different Python build than the server's |

**Media — audited, not guessed.** Of the 33 MB in `media/`, the bundle carries **27 MB**:

| Path | Ships? | Why |
| --- | --- | --- |
| `media/cms/**` (32 files) | **Yes** | Every CMS content image. These are what the redesigned homepage renders |
| `media/problem/*.jpg` (3 files) | **Yes** | `seed_home_content` reads these as *sources*. Byte-identical copies already sit in `media/cms/problem/`, but the command still fails without them: `_resolve_source()` raises `CommandError` **before** the "already installed" check ever runs. 185 KB to avoid a failed seed |
| 5 account photos + `productImages/47441.jpg` | **No** | Referenced by rows in the database you are importing — and **already on the server**, since that is where they were uploaded. Re-sending them would overwrite identical bytes |
| 2 screenshots, `AGNES_PHOTO_FUZE-removebg.png`, `police_hAaDVrF.jpg`, `wikedzi.jpg`, 2 spare product images | **No** | Referenced by **nothing**: no database row, no template, no seed spec. Verified by extracting every file path stored in the production database and grepping the templates for hardcoded `/media/` URLs. Leftovers |

**This is why you extract over the existing directory rather than replacing it.** Six files the
imported database points at are on the server and not in the bundle. Delete `media/` first and
five staff photos and a product image turn into broken images — with nothing in any log to say
so.

### Part B — create the database (in cPanel)

1. **MySQL® Databases** → create a database, e.g. `cpuser_agki`.
2. Create a user, e.g. `cpuser_agki`, with a long generated password. **Do not use `@ : / #`
   in it** — they have to be percent-encoded inside a URL, and that is a needless trap.
3. Add the user to the database with **ALL PRIVILEGES**.
4. Note the final names: cPanel prefixes both with your account name.

The resulting URL:

```
DATABASE_URL=mysql://cpuser_agki:PASSWORD@localhost:3306/cpuser_agki
```

### Part C — configure and deploy

**C1. Build the upload bundle**, on your machine, from the `chore/cpanel-deployment`
working tree:

```bash
git checkout chore/cpanel-deployment
python scripts/make_upload_bundle.py            # writes dist/agki-upload-<date>.zip
python scripts/make_upload_bundle.py --list     # or just look at what it would include
```

263 files, 79 MB, 76 MB compressed. The script exists because "what exactly do I upload?" has
an expensive wrong answer in both directions, and because a hand-assembled zip that happens to
contain `.env` looks exactly like one that does not — so it **refuses to build** if anything
in `FORBIDDEN` would end up inside. What it leaves out, and why, is §4.1.

Upload it through **cPanel → File Manager**, into the application directory, and extract it
there. **Do not delete the existing directory first** — see §4.1 on media. Then, in the cPanel
Python App's virtualenv:

```bash
pip install -r requirements.txt
```

If `mysqlclient` fails to build — it needs MySQL development headers, which not every shared
host has — install the pure-Python driver instead. Nothing else changes; `project/__init__.py`
already makes either one work:

```bash
pip install PyMySQL
```

**C2. Create `.env` in the application root** (next to `manage.py`). cPanel's "Setup Python
App" environment-variable panel works too and takes precedence — either is fine, but pick one
so there is a single place to look.

```ini
DJANGO_ENVIRONMENT=production            # ← the one from §2.1. Without it, nothing else here matters.
DJANGO_SECRET_KEY=<generate a new one, see C3>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=agkimedicallaboratory.co.tz,www.agkimedicallaboratory.co.tz
DJANGO_CSRF_TRUSTED_ORIGINS=https://agkimedicallaboratory.co.tz,https://www.agkimedicallaboratory.co.tz
SITE_URL=https://agkimedicallaboratory.co.tz

DATABASE_URL=mysql://cpuser_agki:PASSWORD@localhost:3306/cpuser_agki
DATABASE_CONN_MAX_AGE=60

EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=mail.agkimedicallaboratory.co.tz
EMAIL_PORT=465
EMAIL_USE_SSL=true
EMAIL_HOST_USER=noreply@agkimedicallaboratory.co.tz
EMAIL_HOST_PASSWORD=<the mailbox password>
DEFAULT_FROM_EMAIL=noreply@agkimedicallaboratory.co.tz

DJANGO_LOGIN_REDIRECT_URL=post_login_redirect   # ← if an old .env sets this to "dashboard",
                                                #   the sign-in fix ships inert.

DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_TRUST_X_FORWARDED_PROTO=true
```

```bash
# C3. A real secret key.
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# C4. Schema, then data.
python manage.py migrate
python manage.py loaddata ./backups/prod_data.json

# C5. Static files.
python manage.py collectstatic --noinput
#     `createcachetable` is NOT needed for this deployment - the rate limiter that
#     wants it arrives with the appointment work (§7).

# C6. Upload media/ to the application root (or wherever DJANGO_MEDIA_ROOT points).

# C7. Restart Passenger.
touch tmp/restart.txt
```

**C8. Make Apache serve `/static/` and `/media/`** (§2.3 — Django will not). In the document
root's `.htaccess`, *above* the Passenger rules:

```apache
RewriteEngine On
RewriteRule ^static/(.*)$  /home/cpuser/app/static/$1  [L]
RewriteRule ^media/(.*)$   /home/cpuser/app/media/$1   [L]
```

Exact paths depend on how the Python app was set up. If cPanel's layout makes this awkward, the
alternative is symlinks from the document root into `static/` and `media/`. **Verify by loading
an image, not by reading the config.**

### Part D — after the first successful load

```bash
# D1. Import the homepage content into the CMS. Run once, deliberately: re-running
#     overwrites the rows it manages, which is the point of an import command but not
#     something to wire into a deploy script.
python manage.py seed_home_content

# D2. Create the first Content Editor account: /admin/ → Users → Add user,
#     is_staff on, group "Content Editors", NOT superuser.
```

---

## 5. Verifying it actually worked

Check the things that fail silently, not the ones that fail loudly.

| # | Check                                                                                      | Why this one                                                                           |
| - | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| 1 | `python manage.py check --deploy` reports no ERRORs                                      | Catches a`DEBUG` or `ALLOWED_HOSTS` left wrong                                     |
| 2 | The homepage loads over HTTPS with**images visible**                                 | Proves §2.3 and §2.4 — the two most common silent breakages                         |
| 3 | `/admin/` loads and shows the **Website Content** section                          | Proves the cms migrations ran                                                          |
| 4 | Sign in as the Content Editor → lands on`/admin/`, sees only Website Content            | Proves the group and the sign-in routing                                               |
| 5 | Contact form → a real email**arrives in the inbox**                                 | Proves`DJANGO_ENVIRONMENT` and SMTP together. A log line is not proof.               |
| 6 | Public doctor list shows**all five doctors**, Kyla and Dr. Magembe included          | Proves the`role` cleanup landed. This is the one thing you can see from the outside. |
| 7 | Row counts match the export                                                                | `CustomUser` 6, `Profile` 4, `AvailableSlot` 25, `LogEntry` 115, `Item` 1    |
| 8 | **Every account still has `is_active=True`** and no new `OtpToken` rows appeared | §2.5. The one check that catches a silent import doing damage                         |

---

## 6. If it goes wrong

Because you zipped the whole application directory in Part 0 and the old database is a file,
rollback is genuinely cheap — and there are two levels of it:

1. **Data only.** Set `DATABASE_URL=` back to empty (SQLite) in `.env`, restore the backed-up
   `db.sqlite3`, `touch tmp/restart.txt`. The site is back on the old data within a minute,
   still running the new code.
2. **Everything.** Extract the Part 0 zip back over the application directory, including its
   `.env`, and restart. That returns the server to exactly the state it was in before you
   started — which is the reason for taking it as a zip rather than a file list.
2. The MySQL database can sit there untouched while you work out what happened; nothing writes
   to it once the URL is unset.

**Keep the SQLite file for at least one full week after cutover**, not one day. The failure
mode you are guarding against is somebody noticing missing content on Thursday.

---

## 7. What this deployment does *not* include

The appointment system work (Phases 0–3) is on `feature/appointments-redesign` and is **not**
part of this deployment unless you choose to include it. Worth knowing either way:

- Production currently has an **unauthenticated product-delete endpoint** —
  `/doctorDashboard/products/delete/<id>/` deletes an `ecommerce.Item` on a plain GET, with no
  login. That is live right now. Phase 1 removes it.
- The patient-record IDOR is also live, but there are **no appointments**, so there is
  currently nothing to leak through it.
- Phase 1 also starts enforcing `is_approved` at sign-in. Three production doctors have
  `is_approved=False` and **would be locked out the moment it ships** — they need approving in
  the admin first. The superuser account is already approved, so there is no risk of locking
  yourself out.

Deploying that branch as well is one migration window instead of two, and the migrations have
already been rehearsed against this data (§4 Part A is that rehearsal). Deploying `main` alone
is a smaller change with less to verify. Both are defensible; it is a scheduling decision, not
a technical one.
