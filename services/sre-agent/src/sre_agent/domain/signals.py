"""The few facts the rule-based fallback and the guardrails read, distilled from the evidence by the
evidence collector (never by the model). The model sees the full evidence; these are its summary.

Telling errors apart (owner's direction, 2026-11-24): a 5xx is a bug (`INTERNAL_ERROR`), overload
(`OVERLOADED`, the 50-request limit), a broken dependency (`DEPENDENCY_UNAVAILABLE`, an open
breaker) or, at the gateway, an upstream's answer passed on; and a revision is either new (its
pod template first ran in the last 15 minutes) or known-good (it already ran long before, e.g. the
one the agent just rolled back to).
"""

from dataclasses import dataclass, field

NEW_REVISION_MINUTES = 15
FAR_ABOVE_NORMAL = 2.0  # request rate / its normal rate
# "Normal" is the previous hour's median request rate (before the incident's 15 minutes). It
# exists only once more than half of that hour had real traffic, at least the alerts' own traffic
# guard of 0.5 requests a second: right after a rebuild, or when the bots were off, the median is a
# quiet laptop's 0 (staging, 2026-11-28: "normal 0.0/s" on every incident), and any traffic would
# look like a huge multiple of it. No decision may lean on such a baseline.
MIN_BASELINE_RATE = 0.5
NO_BASELINE = "no baseline yet"

CPU_SATURATED = 0.8  # of the CPU limit


def baseline(median: float | None) -> float | None:
    """The previous hour's median request rate, or None while there is none to compare with."""
    return median if median is not None and median >= MIN_BASELINE_RATE else None


@dataclass(frozen=True)
class Signals:
    alert: str
    service: str
    errors_by_code: dict[str, float] = field(default_factory=dict)  # 5xx per second, by code
    request_rate: float = 0.0  # per second, now
    normal_rate: float | None = None  # per second, the previous hour's median; None: no baseline
    cpu_of_limit: float = 0.0
    memory_of_limit: float = 0.0
    memory_rising: bool = False
    revision_minutes: float | None = None  # how long ago the running revision was deployed
    revision_known_good: bool = False
    replicas: int = 0
    breaker_open: bool = False
    database_slow: bool = False  # portfolio-service's Postgres transactions far above normal
    # The gateway's 5xx it only passed on (an upstream answered them), as a share of all its 5xx,
    # and the upstream with the most 5xx of its own. The gateway counts its own errors by code; a
    # passed-on error is the upstream's, so acting on the gateway would not help.
    passed_on_share: float = 0.0
    upstream: str | None = None

    @property
    def main_error(self) -> str | None:
        return max(self.errors_by_code, key=self.errors_by_code.__getitem__, default=None)

    @property
    def new_revision(self) -> bool:
        """Deployed recently and not a return to a revision that already ran well."""
        recent = self.revision_minutes is not None and self.revision_minutes < NEW_REVISION_MINUTES
        return recent and not self.revision_known_good

    @property
    def load_far_above_normal(self) -> bool:
        if self.normal_rate is None:  # no baseline: only a saturated CPU says the load is too high
            return self.request_rate > 0 and self.cpu_of_limit >= CPU_SATURATED
        return self.request_rate / self.normal_rate >= FAR_ABOVE_NORMAL
