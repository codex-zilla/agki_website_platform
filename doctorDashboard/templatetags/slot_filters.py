from django import template

register = template.Library()

@register.filter
def split(value, arg):
    return value.split(arg)

@register.filter
def get_slot_by_hour(slots, hour):
    """Get slot for specific hour"""
    if not slots:
        return None
    
    target_hour = int(hour)
    for slot in slots:
        slot_hour = slot.time.hour
        if slot_hour == target_hour:
            return slot
    return None