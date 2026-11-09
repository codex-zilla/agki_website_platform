# Deploying to cPanel — runbook

**Target:** `agkimedicallaboratory.co.tz`, cPanel + Passenger, Python 3.12
**Deploying:** `main` — the CMS and website work. **Not** the appointment redesign; see §7.
**Database:** SQLite for now — the host's MySQL is too old for Django 4.2. See **§3.1/§3.2**.
**Status:** **executed successfully on 2026-08-19.** Corrected afterwards against what actually
happened — every step below has now been run on the real server, not just rehearsed.

> **The stack is LiteSpeed + LSAPI, not Apache + Passenger.** cPanel on CloudLinux wires the
> Python app in the LiteSpeed vhost config, so there are no Passenger directives in
> `.htaccess` to find or edit. `touch tmp/restart.txt` still works — LiteSpeed uses the same
> restart convention — and LiteSpeed honours `.htaccess` the way Apache does. The application
> root is `/home/agkimed1/agki_medical_lab_website`; the document root is
> `/home/agkimed1/public_html`.

> **Read this first — which working tree you upload is the whole decision.**
> There is no git integration on the server, so deployment is a manual upload of whatever is
> checked out on your machine. That makes it easy to ship the wrong thing without noticing.
>
> **Check out `chore/cpanel-deployment` first — before Part A, not just before the upload.**
> It is `main` plus the MySQL support, and nothing else. The branch you have been working on,
> `feature/appointments-redesign`, additionally contains the whole appointment redesign
> (Phases 0–3).
>
> It matters earlier than you might expect, because **Part A migrates a copy of the production
> database using whatever code is checked out**. On the wrong branch that copy goes through the
> appointment redesign's migrations too — and then stops partway with a `RuntimeError` about
> orphan `Rating` rows, which is a baffling thing to meet during a CMS deployment. Step A0 is
> the check.
>
> Two ways to confirm: `showmigrations appointments` should end at `0016` (A0), and the bundle
> script should report **17 files under `appointments/migrations/`**. 25 means the wrong branch.
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
`settings.py` decides what "production" means from that one variable.

> **This happened, and the symptom is misleading.** `check --deploy` came back with six
> warnings — `DEBUG` on, all three secure-cookie settings off, an insecure `SECRET_KEY` — which
> reads like six problems and is one: `IS_PRODUCTION` was false, because the `.env` had been
> uploaded to `tmp/` instead of the application root. `settings.py` reads exactly one path,
> `BASE_DIR/.env`, meaning **next to `manage.py` and nowhere else**. A file anywhere else is
> not an error; everything just silently defaults. If several security warnings appear at once,
> check the file's location before changing any setting. Without it, `IS_PRODUCTION`
> is false and the site silently runs in development mode: **the email backend prints to a log
> instead of sending**, required settings stop being enforced, and `DEBUG` defaults to on. If
> password-reset or OTP emails have "not been arriving", this is the first thing to check. Note
> that `.env.prod` in the repo does **not** contain it.

**2. `.env.prod` is a template, not a config.** It carries `DATABASE_URL=`, `EMAIL_HOST_USER=`
and `EMAIL_HOST_PASSWORD=` all empty, and a `django-insecure-…` secret key that was generated
for development. **Generate a new secret key for production** (step C4). The file is gitignored,
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

## 3. The database: MySQL is the destination, SQLite is the interim

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

### 3.1 Blocked for now — the host's MySQL is too old

cPanel is offering MySQL 5.x. Django 4.2 will not talk to it. The minimums are read straight
out of `django/db/backends/*/features.py`, not from documentation:

| Backend    | Django 4.2 requires |
| ---------- | ------------------- |
| MySQL      | **8.0+**      |
| MariaDB    | **10.4+**     |
| PostgreSQL | **12+**       |
| SQLite     | 3.21+               |

Django checks the server version when it first connects and raises `NotSupportedError`. There
is no flag to override it, and no partial mode — the site simply will not start.

**Confirm the version before abandoning MySQL.** cPanel labels MariaDB as "MySQL" in several
places, and MariaDB's version numbers are not MySQL's: a panel reading "5.5" is often MariaDB
10.x underneath, which *is* supported. In phpMyAdmin, or a shell:

```sql
SELECT VERSION();
```

If that returns something like `10.6.16-MariaDB`, you are fine and §3 applies unchanged. If it
genuinely returns `5.x` and is not MariaDB 10.4+, the options are, in rough order of effort:

1. **Ask the host to move the account to a newer MySQL/MariaDB.** Usually a support ticket
   rather than a migration; many hosts run several versions and put new accounts on the newest.
2. **Check whether PostgreSQL is offered.** Django 4.2 needs 12+, `dj-database-url` already
   parses `postgres://`, and the only code change is `psycopg2-binary` in `requirements.txt`
   instead of `mysqlclient`. Everything in this runbook works the same way.
3. **A managed database elsewhere.** Works, but adds network latency to every query from a
   shared host, and outbound connections on those ports are often firewalled. Check both before
   committing.

**Until one of those lands, deploy on SQLite.** That is a real option here rather than a
compromise to feel bad about: production has been running on SQLite all along, has six accounts
and no appointments, and the CMS is a low-write workload. What follows says exactly what
changes.

### 3.2 The SQLite path

**It makes the deployment simpler, not harder.** There is no database to create, no export and
no import — the migrated database file *is* the deliverable. Concretely, against §4:

| Step                                   | On the SQLite path                                                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **A5** (`dumpdata`)            | **Skip.** Nothing needs exporting; you upload the file itself                                                           |
| **Part B** (create the database) | **Skip entirely**                                                                                                       |
| **C2** (upload)                  | Upload the**migrated `backups/rehearsal.sqlite3`** alongside the bundle, not `prod_data.json`                       |
| **C4** (`.env`)                | Leave`DATABASE_URL` **blank**, and set `DATABASE_CONN_MAX_AGE=0`, `DATABASE_CONN_HEALTH_CHECKS=false`             |
| **C6** (migrate + loaddata)      | `migrate` only, and it should report **no migrations to apply** — Part A already ran them. **No `loaddata`** |

Blank `DATABASE_URL` is not a special case in the code; `settings.py` falls back to
`BASE_DIR/db.sqlite3` when it is empty, which is the same path production uses today.

**Two things to do on the server that MySQL would have given you for free.**

Put the file in place under the right name, keeping the original next to it:

```bash
cd /home/agkimed1/agki_medical_lab_website
mv db.sqlite3 db.sqlite3.pre-deploy          # the running database, kept
mv rehearsal.sqlite3 db.sqlite3              # the one you migrated in Part A
```

**Confirm Django is actually on SQLite before the next command**, or it fails against the
database you were trying to get away from:

```bash
python manage.py shell -c "
from django.conf import settings
d = settings.DATABASES['default']
print(d['ENGINE'], d['NAME'])
"
```

Expect `django.db.backends.sqlite3` and `/home/agkimed1/agki_medical_lab_website/db.sqlite3`. A `NotSupportedError: MySQL 8 or later is required (found 5.7.44)` here means `DATABASE_URL` is still set — in `.env`, or in cPanel's
environment-variable panel, which reaches the shell through the virtualenv's activate script.
Check both: `echo $DATABASE_URL` and `grep DATABASE_URL .env`.

Then switch it to WAL journalling — once, permanently:

```bash
python manage.py shell -c "
from django.db import connection
print(connection.cursor().execute('PRAGMA journal_mode=WAL;').fetchone()[0])
"
```

It should print `wal`. This is a property of the **file**, not of a connection, so it survives
restarts and every later deploy; you never need to run it again. Under the default journalling
a writer blocks readers outright, which on a multi-worker Passenger setup is what produces
`database is locked` on an otherwise idle site. WAL lets readers carry on while a write is in
flight. The settings file separately raises the lock wait from SQLite's default 5 seconds to 20,
so a contended write waits rather than failing in front of a visitor.

**What this costs, stated plainly.**

- **Concurrent writes stay the weak point.** WAL and the longer timeout make `database is locked` unlikely at this traffic level; they do not make it impossible.
- **The appointment redesign cannot ship on this.** Phase 4 claims a slot with
  `select_for_update()`, and SQLite raises `NotSupportedError` — no timeout or journal mode
  changes that. The CMS deployment is unaffected, but **the database question has to be
  resolved before the booking work goes live**, which makes it a scheduling dependency rather
  than a preference.
- **Backups are yours to take.** No cPanel database backup covers a file in a home directory.
  Add `db.sqlite3` to whatever backup you keep of `media/`.

When the database question is resolved, §4 Part A already produces exactly what a MySQL or
PostgreSQL cutover needs — re-run A5 against the by-then-current SQLite file and follow Part B
onward. Nothing done here has to be undone.

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

### Part A — prepare the data (on your machine, using the new code)

**Nothing in this part touches the server.** It all runs on your laptop: the **new** code out
of this repository, pointed at a **copy** of the production database you downloaded in Part 0.

> Commands in Part A run in **your local shell** — PowerShell, most likely. Where the syntax
> genuinely differs, both forms are given. From Part C onward, anything marked *on the server*
> runs in cPanel's Terminal, which is bash, so those blocks are bash and only bash.
> The server carries on serving the old code from its own database the whole time. It is not
> touched until C2, and if you stop after Part A nothing anywhere has changed.

That pairing — new code, old data, local machine — is the entire point of Part A. Some of the
migrations are data cleanups that have to run against production's actual rows: `useraccount 0007` is what turns `Kyla`'s `role='1'` and `Dr. Magembe`'s `role='4491'` back into
`'doctor'`, and it finds them **by username**. Those migrations only exist in the new code. So:

- Run them here, against a copy, and the export you upload is already correct.
- Run them on the server later and you are migrating the live database in place, with no
  rehearsal and no undo.
- Create an empty MySQL database, migrate *that*, then import the old data, and the cleanup
  runs against empty tables — the garbage values come straight back in with the import.

**A0. Check out the branch you are deploying, before anything else.** Part A migrates a copy
of production with whatever code is checked out right now, so this is not a formality:

```bash
git checkout chore/cpanel-deployment
git rev-parse --abbrev-ref HEAD          # must print chore/cpanel-deployment
python manage.py showmigrations appointments
```

The last line of that list must be `[X] 0016_appointmentapproval_approved_by`.

If that last line shows `0024_notification_sending_state`, you are on
`feature/appointments-redesign` and Part A would migrate the copy through the whole appointment
redesign. It does not fail quietly, at least — it stops partway with a `RuntimeError` about
orphan `Rating` rows, which is a baffling thing to meet in the middle of a CMS deployment.

**A1. Put the downloaded database somewhere predictable.** It is wherever your browser saved
it — most likely `Downloads`, still called `db.sqlite3`. Copy it into the repository under a
name you will recognise in a week:

```bash
mkdir -p backups
cp ~/Downloads/db.sqlite3 backups/prod-2026-08-18.sqlite3     # adjust both sides to reality
```

```powershell
# PowerShell
New-Item -ItemType Directory -Force backups
Copy-Item "$HOME\Downloads\db.sqlite3" backups\prod-2026-08-18.sqlite3
```

**A2. Work on a copy of that copy.** If Part A goes wrong you want to restart from something
untouched without downloading again.

```bash
cp backups/prod-2026-08-18.sqlite3 backups/rehearsal.sqlite3
```

Now point Django at it. **The path form matters on Windows** — `$(pwd)` gives Git Bash's
`/c/Users/...`, which SQLite cannot open, and the error you get is a bare
`unable to open database file`:

```bash
# Git Bash on Windows - note `pwd -W`, not `pwd`
export DATABASE_URL="sqlite:///$(pwd -W)/backups/rehearsal.sqlite3"
```

```powershell
# PowerShell
$env:DATABASE_URL = "sqlite:///$((Get-Location).Path -replace '\\','/')/backups/rehearsal.sqlite3"
```

```bash
# macOS/Linux
export DATABASE_URL="sqlite:///$(pwd)/backups/rehearsal.sqlite3"
```

Check it took, before migrating anything:

```bash
python manage.py shell -c "from django.db import connection; print(connection.settings_dict['NAME'])"
```

It must print the path to `backups/rehearsal.sqlite3`. If it prints `db.sqlite3`, the variable
did not reach Django and the next step would migrate **your development database** instead.

**A3. Migrate the copy.**

```bash
python manage.py migrate
```

Expect, among the rest:

```
  Applying useraccount.0007_role_qualifications_split...
    useraccount 0007: set role='doctor' for username='Dr. Magembe'
    useraccount 0007: set role='doctor' for username='Kyla'
```

If it says `WARNING - username='Kyla' not found, skipped`, you are pointed at the wrong
database — that message means the migration could not find the accounts it exists to fix.

**A4. Confirm the cleanup landed and the CMS tables exist.**

```bash
python manage.py shell -c "
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

**Two known data problems are deliberately left alone**, because nothing being deployed here
touches them: the duplicate slot pair, and the two orphan `Rating` rows. They matter only to
the appointment work, whose migrations refuse to run until somebody resolves them. Leaving them
is the right call — do not tidy production data this deployment does not need.

**A5. Export.** — **skipped on the SQLite path (§3.2):** the migrated
`backups/rehearsal.sqlite3` is itself what you upload, so there is nothing to export. Stop
after A4 and go to C1.

The export and the import have both been run end to end against this data: 164
objects out, 164 objects in, accounts intact.

**One line, deliberately.** A backslash continues a line in bash but is just an argument in
PowerShell, where the continuation character is a backtick — split this across lines there and
it fails with `unrecognized arguments: \ \ \`. One line works in every shell:

```
python manage.py dumpdata --natural-foreign --natural-primary --exclude contenttypes --exclude auth.permission --exclude auth.group --exclude sessions.session --indent 2 -o backups/prod_data.json
```

Why each exclusion:

| Excluded                              | Because                                                                                                                                                                                                          |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contenttypes`, `auth.permission` | Recreated by`migrate` on the new database. Importing them collides on ids. `--natural-foreign` is what lets everything else still point at them correctly                                                    |
| `auth.group`                        | `migrate` creates the **Content Editors** group itself (`cms` migration 0003, 54 permissions). Importing a second copy collides on the unique name. Production has no other groups, so nothing is lost |
| `sessions.session`                  | 110 rows of expiring cookies. Everyone signs in again; that is fine                                                                                                                                              |

`prod_data.json` is about 60 KB. **It has to be uploaded to the server** (C2) — step C6 loads
it there, because that is where the MySQL connection lives.

**A6. Unset the variable**, or every later command in this shell still points at the rehearsal
copy:

```bash
unset DATABASE_URL
```

```powershell
$env:DATABASE_URL = $null
```

### 4.1 What goes up, and what stays behind

`scripts/make_upload_bundle.py` is the authority; this is what it decides and why.

**Uploaded** — the seven Django apps (including their `static/` *source* directories),
`templates/`, `manage.py`, `passenger_wsgi.py`, `requirements.txt`, `.env.example`, and two
media directories.

**Not uploaded:**

| Left out                                                   | Why                                                                                                                                                                           |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.git/`                                                  | 72 MB of history the server has no use for                                                                                                                                    |
| `.venv/`                                                 | Platform-specific. The cPanel Python App builds its own                                                                                                                       |
| `static/`                                                | **63 MB of `collectstatic` output.** Regenerate it on the server (C7) — the sources travel inside each app, so uploading this would be shipping the same files twice |
| `design/`, `docs/`                                     | Mockups and reference material; never imported at runtime                                                                                                                     |
| `backups/`, `db.sqlite3`, `deployed_copy_db.sqlite3` | Development and production database copies. The bundle script refuses to build if any appears                                                                                 |
| `.env`, `.env.prod`                                    | Development secrets. You create the real`.env` on the server (C4)                                                                                                           |
| `__pycache__/`, `*.pyc`                                | Compiled for a different Python build than the server's                                                                                                                       |

**Media — audited, not guessed.** Of the 33 MB in `media/`, the bundle carries **27 MB**:

| Path                                                                                                             | Ships?        | Why                                                                                                                                                                                                                                                                                            |
| ---------------------------------------------------------------------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `media/cms/**` (32 files)                                                                                      | **Yes** | Every CMS content image. These are what the redesigned homepage renders                                                                                                                                                                                                                        |
| `media/problem/*.jpg` (3 files)                                                                                | **Yes** | `seed_home_content` reads these as *sources*. Byte-identical copies already sit in `media/cms/problem/`, but the command still fails without them: `_resolve_source()` raises `CommandError` **before** the "already installed" check ever runs. 185 KB to avoid a failed seed |
| 5 account photos +`productImages/47441.jpg`                                                                    | **No**  | Referenced by rows in the database you are importing — and**already on the server**, since that is where they were uploaded. Re-sending them would overwrite identical bytes                                                                                                            |
| 2 screenshots,`AGNES_PHOTO_FUZE-removebg.png`, `police_hAaDVrF.jpg`, `wikedzi.jpg`, 2 spare product images | **No**  | Referenced by**nothing**: no database row, no template, no seed spec. Verified by extracting every file path stored in the production database and grepping the templates for hardcoded `/media/` URLs. Leftovers                                                                      |

**This is why you extract over the existing directory rather than replacing it.** Six files the
imported database points at are on the server and not in the bundle. Delete `media/` first and
five staff photos and a product image turn into broken images — with nothing in any log to say
so.

### Part B — create the database (in cPanel)

> **Skip this whole part on the SQLite path (§3.2).** There is no database to create.

1. **MySQL® Databases** → create a database, e.g. `agkimed1_medical_db`.
2. Create a user, e.g. `agkimed1_admin`, with a long generated password. **Do not use `@ : / #`
   in it** — they have to be percent-encoded inside a URL, and that is a needless trap.
3. Add the user to the database with **ALL PRIVILEGES**.
4. Note the final names: cPanel prefixes both with your account name.

The resulting URL. **The order is user, then password, then database** — and they are easy to
transpose, because cPanel prefixes all three with the same account name and shows them on the
same screen:

```
DATABASE_URL=mysql://USER:PASSWORD@localhost:3306/DBNAME
                     └──┬─┘ └───┬──┘           └──┬──┘
                        │       │                 └── the database, e.g. agkimed1_medical_db
                        │       └── the password you generated for that user
                        └── the user, e.g. agkimed1_admin
```

So for a database `agkimed1_medical_db` owned by user `agkimed1_admin`:

```
DATABASE_URL=mysql://agkimed1_admin:ThePasswordYouGenerated@localhost:3306/agkimed1_medical_db
```

> A name ending in `_db` in the **user** position, or a name ending in `_admin` in the
> **password** position, means the fields have been transposed. The error you get is
> `Access denied for user`, which reads like a permissions problem and sends you back to
> cPanel to re-grant privileges that were never the issue.

Check it before going further — this is the one line most likely to be wrong, and C5 is the
cheapest place to find out:

```bash
python manage.py shell -c "
from django.db import connection
connection.ensure_connection()
print('connected to', connection.settings_dict['NAME'], 'as', connection.settings_dict['USER'])
"
```

### Part C — upload, configure, migrate

Every step says where it runs. That seam is where deployments go wrong: half of these are on
your laptop and half are on the server, and the commands look identical.

---

**C1 · on your machine — build the bundle.**

```bash
git rev-parse --abbrev-ref HEAD               # still chore/cpanel-deployment, from A0
python scripts/make_upload_bundle.py          # writes dist/agki-upload-<date>.zip
```

263 files, 79 MB, 76 MB compressed. Check the printed manifest says **17 files under
`appointments/migrations/`**; 25 means you are on the wrong branch. The script **refuses to
build** if anything in `FORBIDDEN` would end up inside, because a hand-assembled zip that
contains `.env` looks exactly like one that does not. What it leaves out, and why, is §4.1.

---

**C2 · in cPanel File Manager — upload two things.**

1. **`dist/agki-upload-<date>.zip`** → into the application directory → **Extract**.
   **Do not delete the existing directory first.** Six files the imported database points at
   live in the server's `media/` and are deliberately not in the bundle (§4.1); extracting
   *over* the directory keeps them.
2. **`backups/prod_data.json`** → into the application directory too. C6 loads it from there.
   **On the SQLite path (§3.2):** upload **`backups/rehearsal.sqlite3`** instead — that file is
   the database, and §3.2 says what to rename it to once it is up there.

Extraction **merges**, it does not mirror: files deleted in this version still linger on the
server afterwards. Harmless here — nothing imports them once `urls.py` and the templates are
replaced — but worth knowing before you go looking for a file that should be gone.

---

**C3 · on the server — enter the virtualenv, then install dependencies.**

Open **cPanel → Terminal** (or SSH in). Everything from here to C8 runs there, and every one
of those commands needs the virtualenv active first — outside it, `python` is the system
interpreter with none of this project's packages.

**cPanel writes the exact command for you.** Go to **Setup Python App**, open the application,
and the top of its card shows a line beginning `source /home/…/bin/activate`, with a copy
button. Paste that into Terminal. It looks like:

```bash
source /home/agkimed1/virtualenv/agki_medical_lab_website/3.12/bin/activate && cd /home/agkimed1/agki_medical_lab_website
```

The three variables are your cPanel username, the **Application root** you set when the app was
created, and the Python version. Copying it from the UI avoids guessing all three.

Confirm it took before doing anything else:

```bash
which python          # must be under /home/.../virtualenv/..., not /usr/bin
python --version      # 3.12.x
```

If the prompt gains a `(agki:3.12)` prefix, you are in. Then:

```bash
pip install -r requirements.txt
```

> **If Terminal is disabled** on this hosting plan, Setup Python App can still install
> dependencies on its own: add `requirements.txt` under **Configuration files** and use **Run
> Pip Install**. That covers C3 — but *only* C3. Steps C5 through C8 and D1 are management
> commands with no UI equivalent, so without Terminal or SSH this deployment cannot be
> completed. Worth checking you have one **before** starting Part A.

If `mysqlclient` fails to build — it needs MySQL development headers, which not every shared
host has — install the pure-Python driver instead. Nothing else changes; `project/__init__.py`
already makes either one work:

```bash
pip install PyMySQL
```

---

**C4 · on the server — create `.env`** in the application root, next to `manage.py`.

**Use the file, not cPanel's environment-variable panel.** This is not a style preference. The
panel injects variables into the *application process Passenger starts* — it does not put them
in your Terminal session. But C5 through C8 and D1 are all commands you run from Terminal, and
`settings.py` reads `.env` on every import regardless of how the process was started:

```python
load_dotenv(BASE_DIR / ".env", override=False)
```

So with the config only in the panel, `python manage.py migrate` in Terminal would see **no**
`DATABASE_URL`, fall back to the SQLite default, cheerfully build every table in a
`db.sqlite3` beside `manage.py`, and report success — while the website talks to MySQL and
finds nothing there. Nothing errors. You would notice at C6 or later, by which point the
obvious explanation is the wrong one.

`override=False` means real environment variables still win, so the panel is safe to use *as
well* — it just cannot be used *instead*.

Generate the secret key first (either machine):

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

```ini
# ─── Required. The site refuses to start without these in production ────────
DJANGO_ENVIRONMENT=production
DJANGO_SECRET_KEY=<the key you just generated>
DJANGO_ALLOWED_HOSTS=agkimedicallaboratory.co.tz,www.agkimedicallaboratory.co.tz
SITE_URL=https://agkimedicallaboratory.co.tz
DATABASE_URL=mysql://DBUSER:DBPASSWORD@localhost:3306/DBNAME
# On the SQLite path (§3.2), leave DATABASE_URL empty instead - settings.py then
# falls back to BASE_DIR/db.sqlite3, which is where production already is:
#   DATABASE_URL=
#   DATABASE_CONN_MAX_AGE=0
#   DATABASE_CONN_HEALTH_CHECKS=false

# ─── Strongly recommended, even though the defaults are already right ───────
# Explicit because these are the ones you want to be able to read off the file
# during an incident rather than infer from a default.
DJANGO_DEBUG=false
DJANGO_CSRF_TRUSTED_ORIGINS=https://agkimedicallaboratory.co.tz,https://www.agkimedicallaboratory.co.tz
DATABASE_CONN_MAX_AGE=60
DATABASE_CONN_HEALTH_CHECKS=true

# ─── Email. All four are enforced in production ─────────────────────────────
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=mail.agkimedicallaboratory.co.tz
EMAIL_PORT=465
EMAIL_USE_SSL=true
EMAIL_USE_TLS=false
EMAIL_HOST_USER=noreply@agkimedicallaboratory.co.tz
EMAIL_HOST_PASSWORD=<the mailbox password>
DEFAULT_FROM_EMAIL=noreply@agkimedicallaboratory.co.tz

# ─── HTTPS and cookies ──────────────────────────────────────────────────────
DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_TRUST_X_FORWARDED_PROTO=true
# HSTS stays OFF for the first deploy - see the note below. Turn it on later.
DJANGO_SECURE_HSTS_SECONDS=0
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=false
DJANGO_SECURE_HSTS_PRELOAD=false

# ─── Auth redirects ─────────────────────────────────────────────────────────
DJANGO_LOGIN_URL=signin
DJANGO_LOGIN_REDIRECT_URL=post_login_redirect
DJANGO_LOGOUT_REDIRECT_URL=admin:login
ACCOUNT_LOGOUT_REDIRECT_URL=signin

# ─── Locale ─────────────────────────────────────────────────────────────────
DJANGO_TIME_ZONE=Africa/Dar_es_Salaam
DJANGO_LANGUAGE_CODE=en-us
DJANGO_USE_TZ=true
DJANGO_USE_I18N=true

# ─── OAuth. Blank = the social buttons do not work. See below ───────────────
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_CLIENT_KEY=
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
GITHUB_CLIENT_KEY=
```

**`DJANGO_LOGOUT_REDIRECT_URL` is not a copy of the dev file.** `.env.example` carries
`logout`, which names the logout view itself — logging out would redirect to logging out, in a
loop. The value above matches the code default and is the one to use.

**Leave HSTS at 0 for the first deploy.** Once a browser has seen an HSTS header it refuses
plain HTTP for that domain for the full duration, cache and all, and you cannot take it back by
removing the header. Turn it on — start at `3600`, then raise — once you have confirmed
every page, image and redirect works over HTTPS. `check --deploy` will warn about it being
unset in the meantime; that warning is expected here, not a failure.

**The OAuth keys are deliberately blank**, because Google and GitHub sign-in have never been
set up: production has **zero** rows in `socialaccount_socialapp`, `socialaccount_socialaccount`
and `account_emailaddress`. Nothing is lost by leaving them empty — but any "Sign in with
Google" button on the site is inert and will send visitors to a Google error page. Either
configure the credentials or remove the buttons; do not leave it looking like a working option.

**Deliberately not in the file above.** Every one of these is read by `settings.py`, and every
one already defaults to the right production value — so setting them adds a line to get wrong
without changing behaviour:

| Variable                                      | Default                                 | Why that is fine                                                                                                                                          |
| --------------------------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DJANGO_SITE_ID`                            | `1`                                   | Production has exactly one`django_site` row, id 1. But see D3 — its *domain* is wrong                                                                |
| `DJANGO_STATIC_URL`, `DJANGO_MEDIA_URL`   | `/static/`, `/media/`               | Match the`.htaccess` rules in C9                                                                                                                        |
| `DJANGO_STATIC_ROOT`, `DJANGO_MEDIA_ROOT` | `BASE_DIR/static`, `BASE_DIR/media` | Correct while the app root is the project root. Set`DJANGO_MEDIA_ROOT` only if you ever move `media/` outside the directory a deploy replaces (§2.4) |
| `SOCIALACCOUNT_*`, `ACCOUNT_UNIQUE_EMAIL` | as in`.env.example`                   | Only reachable through OAuth, which is not configured                                                                                                     |

And one that is in the **dev `.env`** but is **not a setting at all**: a stray
`UB_CLIENT_SECRET=`, a truncated leftover of `GITHUB_CLIENT_SECRET`. `settings.py` never reads
it, so it does nothing anywhere — worth deleting from the dev file so the next person reading
it does not go looking for what it configures. (`.env.example` is clean; this one only exists
in the working copy.)

---

**C5 · on the server — check the configuration before touching the database.**

```bash
python manage.py check --deploy
```

**Expect exactly one warning: `security.W004`**, about `SECURE_HSTS_SECONDS`. That one is
deliberate (see C4). *Anything else is a failure*, not something to note and move past.

In particular, seeing `W008`, `W012`, `W016` and `W018` together is not four problems — it is
`IS_PRODUCTION` being false, and §2.1 says where to look. `W009` alongside them confirms it:
`SECRET_KEY` can only fall back to its insecure default when `IS_PRODUCTION` is false, because
`env_value()` raises instead of falling back in production.

Note this command does **not** open a database connection, so it passing says nothing about
`DATABASE_URL`. C6 is where that gets tested.

---

**C6 · on the server — schema, then data.**

```bash
python manage.py migrate
python manage.py loaddata prod_data.json      # the file you uploaded in C2
```

`migrate` runs against the empty MySQL database and builds every table. `loaddata` then puts
production's 164 rows into it. Expect "Installed 164 object(s) from 1 fixture(s)".

> **On the SQLite path (§3.2):** run `migrate` only — **no `loaddata`**. It should report *no
> migrations to apply*, because Part A already applied them to the file you just uploaded. If it
> starts applying migrations instead, you are pointed at the wrong file: most likely the old
> `db.sqlite3` is still in place and the rename in §3.2 has not happened.

---

**C7 · on the server — static files.**

```bash
python manage.py collectstatic --noinput
```

This is why `static/` is not in the bundle: it is generated from sources that are.
`createcachetable` is **not** needed for this deployment — the rate limiter that wants it
arrives with the appointment work (§7).

---

**C8 · on the server — restart Passenger.**

```bash
touch tmp/restart.txt
```

---

**C9 · on the server — let the web server serve `/static/` and `/media/`** (§2.3 — Django
will not, once `DEBUG=False`).

**Symlink them into the document root.** This is the approach that works, and it needs no
configuration file at all — LiteSpeed serves any file that exists in the document root before
the request reaches Django:

```bash
ln -s /home/agkimed1/agki_medical_lab_website/static /home/agkimed1/public_html/static
ln -s /home/agkimed1/agki_medical_lab_website/media  /home/agkimed1/public_html/media
ls -la /home/agkimed1/public_html/ | grep -E "static|media"
```

> **Not rewrite rules.** An earlier draft of this step gave `RewriteRule` directives to paste
> into `.htaccess`. Two things were wrong with it. They are **Apache configuration, not shell
> commands** — pasted into a terminal they produce `bash: RewriteEngine: command not found`.
> And they would not have worked from `.htaccess` anyway: a substitution beginning with `/` is
> treated as a URL path, and mapping one to a directory *outside* the document root needs an
> `Alias` directive, which shared hosting does not give you. Symlinks sidestep both problems.

> **Leave `/home/agkimed1/public_html/.htaccess` alone.** On this stack it contains only
> CloudLinux's environment-variable block, marked `DO NOT REMOVE OR MODIFY` — and it is what
> carries the app's environment. There are no Passenger directives in it to order things around.

**Verify with real files, not by reading config.** A directory returns 404 whether or not
routing works, so test actual files:

```bash
curl -sI https://agkimedicallaboratory.co.tz/static/admin/css/base.css | head -1
curl -sI https://agkimedicallaboratory.co.tz/media/cms/awards/AFD.png | head -1
```

Both `200` is the pass. `403` means symlink following is disabled; `404` on static means the
web server is routing everything to Django; `404` on media only means the media upload did not
land, which is a different problem with a different fix — which is why they are tested
separately.

### Part D — after the first successful load

**D1 · on the server — import the homepage content into the CMS.**

```bash
python manage.py seed_home_content
```

Run once, deliberately. Re-running **overwrites** the rows it manages, which is what an import
command should mean but is not something to wire into a deploy script.

It reads its images from two places, both of which the bundle carries: `static:app/…` through
the staticfiles finders, and `media:problem/…` from `media/problem/` — the three files §4.1
explains. If it stops with `CommandError: Media asset not found`, that directory did not make
it up.

**D2 · in the browser — create the first Content Editor account.** `/admin/` → Users → Add
user, `is_staff` on, group **Content Editors**, **not** superuser.

**D3 · on the server — fix the site domain.** Production's one `django_site` row still says
`example.com`, the value Django ships with, and the import carries it across unchanged:

```bash
python manage.py shell -c "
from django.contrib.sites.models import Site
s = Site.objects.get(pk=1)
s.domain = 'agkimedicallaboratory.co.tz'
s.name = 'AGKI Medical Laboratory'
s.save()
print('site is now', s.domain)
"
```

Not cosmetic. `django.contrib.sites` is installed, so Django's password-reset email builds its
link from this row — `useraccount/templates/useraccount/password_reset_email.html` renders
`{{ protocol }}://{{ domain }}…`, and `domain` comes from here. Left alone, every password
reset email sends staff to `example.com`. It fails silently, at the least convenient moment,
and to the one person who cannot get in to report it.

---

## 5. Verifying it actually worked

Check the things that fail silently, not the ones that fail loudly.

| # | Check                                                                                                            | Why this one                                                                           |
| - | ---------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 1 | `python manage.py check --deploy` reports no ERRORs                                                            | Catches a`DEBUG` or `ALLOWED_HOSTS` left wrong                                     |
| 2 | The homepage loads over HTTPS with**images visible**                                                       | Proves §2.3 and §2.4 — the two most common silent breakages                         |
| 3 | `/admin/` loads and shows the **Website Content** section                                                | Proves the cms migrations ran                                                          |
| 4 | Sign in as the Content Editor → lands on`/admin/`, sees only Website Content                                  | Proves the group and the sign-in routing                                               |
| 5 | Contact form → a real email**arrives in the inbox**                                                       | Proves`DJANGO_ENVIRONMENT` and SMTP together. A log line is not proof.               |
| 6 | Public doctor list shows**all five doctors**, Kyla and Dr. Magembe included                                | Proves the`role` cleanup landed. This is the one thing you can see from the outside. |
| 7 | Row counts match the export                                                                                      | `CustomUser` 6, `Profile` 4, `AvailableSlot` 25, `LogEntry` 115, `Item` 1    |
| 8 | **Every account still has `is_active=True`** and no new `OtpToken` rows appeared                       | §2.5. The one check that catches a silent import doing damage                         |
| 9 | Request a password reset → the emailed link points at**agkimedicallaboratory.co.tz**, not `example.com` | Proves D3. This is only visible in the email body, so nothing else catches it          |

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
3. The MySQL database can sit there untouched while you work out what happened; nothing writes
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
