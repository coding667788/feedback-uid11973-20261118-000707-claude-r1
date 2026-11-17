"""Injectable trusted clock.

The business clock is never read from the client request and never from
the wall clock inside tests: every write operation samples ``clock.now()``
*inside* the database write transaction, after the write lock has been
acquired.  Tests drive time by injecting :class:`ManualClock`.
"""


class ManualClock:
    """A clock whose value is set explicitly by the caller."""

    def __init__(self, value=0):
        self._value = value

    def set(self, value):
        self._value = value

    def now(self):
        return self._value
