"""Make whichever MySQL driver is installed satisfy Django.

Django 4.2 talks to MySQL through `MySQLdb`, which is provided by
**mysqlclient** — a C extension. On shared cPanel hosting it often builds
fine; sometimes the MySQL development headers are missing and it cannot be
installed at all. **PyMySQL** is the pure-Python fallback that works
everywhere, and this makes it answer to the same name.

Nothing here runs unless a MySQL driver is actually present, so a SQLite
install is unaffected.
"""

try:  # pragma: no cover - depends on which driver the host could install
    import MySQLdb  # noqa: F401  (mysqlclient: nothing to do)
except ImportError:
    try:
        import pymysql
    except ImportError:
        pass  # No MySQL driver at all — SQLite, or the driver isn't needed.
    else:
        # Django refuses to start against a driver reporting a version older
        # than mysqlclient 1.4.3. PyMySQL reports its own (lower) version, so
        # the check fails on a driver that is perfectly capable. Overriding
        # the tuple is the long-standing workaround; it is a lie about the
        # version, not about the capability.
        pymysql.version_info = (1, 4, 3, "final")
        pymysql.install_as_MySQLdb()
