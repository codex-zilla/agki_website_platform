"""Week and month periods for a navigable calendar.

The doctor console's availability page has had these since plan 11.1, as
private helpers inside `doctorDashboard.views`. The patient-facing doctor page
needs the same arithmetic — AGKI asked for the same layout, and a Week/Month
switch that stepped differently from the console's would be the same page
behaving two ways.

They live here rather than being imported from the console because
`doctorDashboard` already imports from `appointments`, and importing back the
other way is a circular import waiting to happen. The console keeps its own
copy for now: AGKI asked that its availability page not be touched, and
changing an import is still touching it. Consolidating is a one-line change
whenever that page is next worked on.
"""

import calendar
from datetime import date as date_cls, timedelta

from django.utils import timezone

WEEK = "week"
MONTH = "month"


def mode_from(request, default=MONTH):
    """`week` or `month`, from `?view=`.

    The default is the caller's, and the two callers want different ones: a
    doctor publishing times works a week at a time, while a patient choosing
    among the next thirty days wants to see them all.
    """
    asked = request.GET.get("view", "")
    if asked in (WEEK, MONTH):
        return asked
    return default


def anchor_date(request):
    """The day the period is drawn around, from `?start=`.

    Anything unparseable falls back to today rather than raising: the value
    arrives from history and from bookmarks, not only from links this page
    drew.
    """
    try:
        return date_cls.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        return timezone.localdate()


def period(anchor, mode):
    """First and last day shown, and the label above the navigation."""
    if mode == MONTH:
        start = anchor.replace(day=1)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        return start, end, start.strftime("%B %Y")
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=6)
    return start, end, "%s - %s" % (start.strftime("%d %b"), end.strftime("%d %b %Y"))


def step(anchor, mode, direction):
    """The anchor date one period earlier or later."""
    if mode == MONTH:
        first = anchor.replace(day=1)
        if direction < 0:
            return (first - timedelta(days=1)).replace(day=1)
        return first.replace(
            day=calendar.monthrange(first.year, first.month)[1]
        ) + timedelta(days=1)
    return anchor + timedelta(days=7 * direction)


def weeks_between(start, end, build):
    """The period as rows of seven, padded to whole Monday-Sunday weeks.

    `build(day)` returns whatever each cell should carry. Padding is what makes
    a month render as a month rather than as a ragged list, and it is why the
    grid can carry days outside the period at all — they exist because a week
    has seven days, not because they are offers.
    """
    grid_start = start - timedelta(days=start.weekday())
    grid_end = end + timedelta(days=6 - end.weekday())

    rows, row, day = [], [], grid_start
    while day <= grid_end:
        row.append(build(day))
        if len(row) == 7:
            rows.append(row)
            row = []
        day += timedelta(days=1)
    return rows
