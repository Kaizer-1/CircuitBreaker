"""A cap on concurrent time-travel work per replica, with room kept for the presenter.

Rebuilding a past book and streaming a replay cost CPU here and reads at matching-engine, so each
replica runs at most `capacity` of each at once. The last `reserved_for_admin` places can only be
taken by the admin role, so a crowd of traders time-travelling never locks the presenter out
during a demo. A request that finds no place is refused at once (Q-12 `503 OVERLOADED`, Q-13 close
`1013`) rather than queued: waiting would only make every replay slower.
"""


class Slots:
    def __init__(self, capacity: int, reserved_for_admin: int) -> None:
        if not 0 <= reserved_for_admin < capacity:
            raise ValueError("the reserved places must leave at least one for everyone")
        self.capacity = capacity
        self.reserved_for_admin = reserved_for_admin
        self.in_use = 0

    def try_acquire(self, *, admin: bool) -> bool:
        limit = self.capacity if admin else self.capacity - self.reserved_for_admin
        if self.in_use >= limit:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)
