"""What chaos-service acts on (ADR-0020): one Deployment through the Kubernetes API, and the fault
hooks inside three services. Nothing else; never a data store."""

from typing import Protocol


class TargetUnavailableError(Exception):
    """A target could not be reached or refused the change (the experiment does not start)."""


class OrderServiceDeployment(Protocol):
    """The `order-service` Deployment in chaos-service's own namespace (its Role names only it)."""

    async def bad_release_running(self) -> bool:
        """Does the pod template carry FAULT_PROFILE (chaos's bad release)?"""

    async def deploy_bad_release(self, error_rate: float) -> int | None:
        """Patch in FAULT_PROFILE=bad-release: a new rollout revision. Returns its number."""

    async def remove_bad_release(self) -> bool:
        """Patch it out again (the previous pod template returns). False if it was not there."""

    async def replicas(self) -> int: ...

    async def scale(self, replicas: int) -> None: ...


class FaultHooks(Protocol):
    """The targets' internal /internal/v1/faults hooks. `*_off` is False if it was not on."""

    async def memory_leak_on(self, rate_mb_per_minute: int) -> None: ...

    async def memory_leak_off(self) -> bool: ...

    async def database_degraded_on(self, delay_ms: int) -> None: ...

    async def database_degraded_off(self) -> bool: ...

    async def panic_sell_on(self, requests_per_second: int, duration_seconds: int) -> None: ...

    async def panic_sell_off(self) -> bool: ...
