"""Shared conservative disk budget for a new full local backup and restore."""
import math


def budget(source_bytes, usage, minimum_free_bytes, first_restore=False):
    floor = max(minimum_free_bytes, int(usage.total * .10))
    copies = 4.5 if first_restore else 2.3
    required = math.ceil(source_bytes * copies + floor)
    return {
        'estimated_source_bytes': source_bytes,
        'available_bytes': usage.free,
        'required_free_bytes': required,
        'reserve_bytes': floor,
        'shortfall_bytes': max(0, required - usage.free),
        'sufficient': usage.free >= required,
    }
