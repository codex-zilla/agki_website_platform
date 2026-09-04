from django import template

register = template.Library()


@register.filter
def split(value, arg):
    return value.split(arg)


# `get_slot_by_hour` was removed with the availability rebuild (plan 5.5).
#
# It backed an hour matrix hardcoded to 08:00-17:00 and returned **the first
# slot in that hour**, so a doctor publishing 08:00 and 08:30 saw one of them
# and a doctor publishing anything at 19:00 saw neither. The timetable is a
# day grid now: every slot in a day is rendered, whatever hour it falls in.
