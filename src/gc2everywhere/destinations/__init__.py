"""Add a destination module and register it here; the synchronization engine stays unchanged."""

from ..common import SyncError
from .effortsignal import EffortSignal
from .intervals import Intervals

DESTINATIONS = (Intervals, EffortSignal)


def configured_destinations(settings, transport, selected=None):
    if selected and set(selected) - {adapter.name for adapter in DESTINATIONS}:
        raise SyncError("unknown destination; choose intervals or effortsignal")
    result = []
    for adapter in DESTINATIONS:
        if selected and adapter.name not in selected:
            continue
        key = settings.get(adapter.key_variable, "").strip()
        if key:
            result.append(adapter(key, transport, settings))
        elif selected:
            raise SyncError(f"{adapter.key_variable} is required for {adapter.name}")
    return result
