"""What a failed or paused sign-in looks like, whichever way the person signs in.

Directory sign-in (``auth.ldap``) and local users (``auth.users``) share these
so the console and the API answer the same way: one message for every reason
that is the person's doing, and a pause after repeated failures.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

#: The same answer for every reason a sign-in fails on the person's side.
REFUSED = "Wrong username or password"


class SignInRefused(Exception):
    """The sign-in did not succeed; the message is safe to show the person."""


class TooManyFailures(SignInRefused):
    """Sign-in is paused for this user or address for a while."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many failed sign-ins; try again in {retry_after} seconds")
        self.retry_after = retry_after


class LoginThrottle:
    """Counts failures per key in a sliding window and pauses a key that has too many."""

    def __init__(
        self, max_failures: int, lockout_seconds: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Pause a key after ``max_failures`` failures within ``lockout_seconds``."""
        self.max_failures = max_failures
        self.lockout = lockout_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        window = self._failures[key]
        while window and now - window[0] > self.lockout:
            window.popleft()
        if not window:
            self._failures.pop(key, None)
            return deque()
        return window

    def check(self, *keys: str) -> None:
        """Raise :class:`TooManyFailures` when any key is paused."""
        now = self._clock()
        with self._lock:
            for key in keys:
                window = self._recent(key, now)
                if len(window) >= self.max_failures:
                    raise TooManyFailures(max(1, int(window[0] + self.lockout - now) + 1))

    #: Past this many keys being tracked, expired ones are dropped on the next failure.
    MAX_TRACKED = 10_000

    def failed(self, *keys: str) -> None:
        """Count one more failure for each key."""
        now = self._clock()
        with self._lock:
            if len(self._failures) > self.MAX_TRACKED:
                for stale in [k for k in self._failures if not self._recent(k, now)]:
                    self._failures.pop(stale, None)
            for key in keys:
                self._recent(key, now)
                self._failures[key].append(now)

    def succeeded(self, *keys: str) -> None:
        """Forget the failures of each key."""
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
