"""Bounded, advisory cache for verified standalone library tracks."""

from collections import OrderedDict
from threading import Lock
from time import monotonic

from trip_api.db import VaultActivity


class ReusePreviewCache:
    """Remember standalone validation per source version within one API process."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: OrderedDict[tuple[object, ...], float] = OrderedDict()

    @staticmethod
    def _key(activity: VaultActivity) -> tuple[object, ...]:
        return (
            activity.user_id,
            activity.id,
            activity.updated_at,
            activity.disconnected_at,
            activity.source_provider,
            activity.strava_activity_id,
            activity.external_activity_id,
            activity.bronze_object_key,
            activity.silver_object_key,
            activity.source_sha256,
        )

    def is_ready(self, activity: VaultActivity) -> bool:
        """Return a recent verification of the current connected source."""
        if not activity.has_gps or activity.disconnected_at is not None:
            return False
        key = self._key(activity)
        with self._lock:
            expires = self._entries.pop(key, 0)
            if expires <= monotonic():
                return False
            self._entries[key] = expires
            return True

    def mark_ready(self, activity: VaultActivity) -> None:
        """Cache successful standalone validation, never authorization to copy."""
        key = self._key(activity)
        with self._lock:
            self._entries[key] = monotonic() + 60
            self._entries.move_to_end(key)
            # Bound memory without treating out-of-band object changes as permanent readiness.
            while len(self._entries) > 4096:
                self._entries.popitem(last=False)
