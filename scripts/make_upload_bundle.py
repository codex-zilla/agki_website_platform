"""Build the zip to upload to cPanel - and nothing else.

    python scripts/make_upload_bundle.py            # build dist/agki-upload-<date>.zip
    python scripts/make_upload_bundle.py --list     # show what would go in, build nothing

There is no git integration on the cPanel side, so deployment is a manual
upload. That makes "what exactly do I upload?" a real question with an
expensive wrong answer in both directions: too much and you ship a 72 MB .git
directory, a development database and a local .env holding development
secrets; too little and the site 500s on a missing module.

This script answers it once. The include list below is deliberate, and every
exclusion carries a reason rather than a hunch.

**It refuses to build a bundle containing anything sensitive** - see
`check_nothing_sensitive`. That check is most of the reason for having a script
at all: a hand-assembled zip that happens to include `.env` looks exactly like
one that does not, right up until it is unpacked on a server.
"""

import argparse
import fnmatch
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- what goes up ----------------------------------------------------------

DIRECTORIES = [
    # The Django apps. Their static/ subdirectories are the *sources*
    # collectstatic reads, so they travel with the code.
    "app",
    "appointments",
    "cms",
    "doctorDashboard",
    "ecommerce",
    "useraccount",
    "project",
    # Project-level template overrides (admin/base.html and friends).
    "templates",
    "scripts",
]

FILES = [
    "manage.py",
    "passenger_wsgi.py",
    "requirements.txt",
    ".env.example",  # a reference for filling in the real .env on the server
]

# Media is audited rather than shipped wholesale - see MEDIA_NOTE.
MEDIA_SUBDIRECTORIES = [
    "cms",      # every CMS content image; the site renders these directly
    "problem",  # seed_home_content reads these three as SOURCES and raises
                # CommandError without them - even though media/cms/problem/
                # already holds byte-identical copies, because
                # _resolve_source() runs before the "already installed" check.
]

MEDIA_NOTE = """
Everything else under media/ stays on the server, deliberately:

  * Five account photos and one product image are referenced by rows in the
    database being imported. They already exist in the server's own media/
    directory, so re-uploading them would only overwrite them with identical
    bytes. This assumes the upload ADDS to media/ rather than replacing it -
    do not delete the server's media/ directory before uploading.
  * The rest - two screenshots, a background-removed portrait, a duplicate
    photo and two spare product images - are referenced by nothing at all: no
    database row, no template, no seed spec. Leftovers from earlier uploads.
"""

# --- what must never go up -------------------------------------------------

FORBIDDEN = {
    ".env",                       # development secrets, local database URL
    ".env.prod",                  # a template holding a development secret key
    "db.sqlite3",                 # the development database
    "deployed_copy_db.sqlite3",   # a copy of production, real accounts in it
}

EXCLUDE_PATTERNS = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    "*.log",
    ".gitignore",
]

# Top-level entries deliberately absent from DIRECTORIES, recorded so the next
# reader does not have to wonder whether each one was an oversight.
NOT_SHIPPED = {
    ".git": "72 MB of history the server has no use for",
    ".venv": "platform-specific; the cPanel Python App builds its own",
    "static": "collectstatic output (63 MB). Regenerate on the server instead - "
              "the sources travel inside each app",
    "design": "HTML mockups, never imported at runtime",
    "docs": "reference material, including the deployment runbook itself",
    "backups": "database snapshots, some holding real accounts",
    "public": "empty here; the server's document root is its own thing",
    "tmp": "only ever holds restart.txt, which you touch on the server",
}


def is_excluded(relative_path):
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in relative_path.parts
        for pattern in EXCLUDE_PATTERNS
    )


def files_under(base):
    for path in sorted(base.rglob("*")):
        if path.is_file() and not is_excluded(path.relative_to(ROOT)):
            yield path, path.relative_to(ROOT).as_posix()


def collect():
    """Every (absolute path, archive name) pair the bundle should contain."""
    entries = []

    for name in DIRECTORIES:
        base = ROOT / name
        if not base.is_dir():
            raise SystemExit(f"Expected directory is missing: {name}")
        entries.extend(files_under(base))

    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"Expected file is missing: {name}")
        entries.append((path, name))

    for name in MEDIA_SUBDIRECTORIES:
        base = ROOT / "media" / name
        if not base.is_dir():
            raise SystemExit(f"Expected media directory is missing: media/{name}")
        entries.extend(files_under(base))

    return entries


def check_nothing_sensitive(entries):
    """Refuse to build, rather than warn.

    A bundle that quietly contains `.env` is indistinguishable from one that
    does not until somebody unpacks it on a server - and by then it has already
    been transmitted. Matching on the file name anywhere in the tree, not just
    at the root, because a stray copy in a subdirectory is exactly the case a
    tidy include list would miss.
    """
    offenders = sorted(
        arcname for _, arcname in entries if Path(arcname).name in FORBIDDEN
    )
    if offenders:
        raise SystemExit(
            "REFUSING TO BUILD - the bundle would contain files that must "
            "never leave this machine:\n  " + "\n  ".join(offenders)
        )


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def summarise(entries):
    totals = {}
    for path, arcname in entries:
        parts = arcname.split("/")
        top = "/".join(parts[:2]) if parts[0] == "media" else parts[0]
        count, size = totals.get(top, (0, 0))
        totals[top] = (count + 1, size + path.stat().st_size)
    return totals


def main():
    parser = argparse.ArgumentParser(description="Build the cPanel upload bundle.")
    parser.add_argument("--list", action="store_true",
                        help="Show the manifest and build nothing.")
    parser.add_argument("--output", default=None, help="Where to write the zip.")
    options = parser.parse_args()

    entries = collect()
    check_nothing_sensitive(entries)
    totals = summarise(entries)

    print("Bundle contents")
    print("-" * 56)
    for name in sorted(totals):
        count, size = totals[name]
        print(f"  {name:26s} {count:5d} files  {human(size):>9s}")
    print("-" * 56)
    grand = sum(size for _, size in totals.values())
    print(f"  {'TOTAL':26s} {len(entries):5d} files  {human(grand):>9s}")

    print("\nDeliberately not shipped")
    print("-" * 56)
    for name in sorted(NOT_SHIPPED):
        print(f"  {name:10s} {NOT_SHIPPED[name]}")
    print(MEDIA_NOTE)

    if options.list:
        return

    output = Path(options.output) if options.output else (
        ROOT / "dist" / f"agki-upload-{date.today():%Y%m%d}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in entries:
            archive.write(path, arcname)

    compressed = output.stat().st_size
    print(f"Wrote {output.relative_to(ROOT).as_posix()}  ({human(compressed)} compressed)")


if __name__ == "__main__":
    main()
