"""Test doubles for chaos-service's targets: they record what was asked, like the real ones."""

from chaos_service.ports.targets import TargetUnavailableError


class FakeOrderService:
    def __init__(self, replicas: int = 2) -> None:
        self.bad_release = False
        self.revision = 7
        self._replicas = replicas
        self.calls: list[str] = []
        self.down = False

    async def bad_release_running(self) -> bool:
        return self.bad_release

    async def deploy_bad_release(self, error_rate: float) -> int | None:
        if self.down:
            raise TargetUnavailableError("Kubernetes API: ConnectError")
        self.calls.append(f"deploy {error_rate}")
        self.bad_release, self.revision = True, self.revision + 1
        return self.revision

    async def remove_bad_release(self) -> bool:
        if not self.bad_release:
            return False
        self.calls.append("remove")
        self.bad_release, self.revision = False, self.revision + 1
        return True

    async def replicas(self) -> int:
        return self._replicas

    async def scale(self, replicas: int) -> None:
        self.calls.append(f"scale {replicas}")
        self._replicas = replicas


class FakeHooks:
    def __init__(self) -> None:
        self.on: dict[str, object] = {}
        self.calls: list[str] = []
        self.down: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.down:
            raise TargetUnavailableError(f"{name} did not answer: ConnectError")

    async def _off(self, name: str) -> bool:
        self._check(name)
        self.calls.append(f"{name} off")
        return self.on.pop(name, None) is not None

    async def memory_leak_on(self, rate_mb_per_minute: int) -> None:
        self._check("leak")
        self.on["leak"] = rate_mb_per_minute

    async def memory_leak_off(self) -> bool:
        return await self._off("leak")

    async def database_degraded_on(self, delay_ms: int) -> None:
        self._check("database")
        self.on["database"] = delay_ms

    async def database_degraded_off(self) -> bool:
        return await self._off("database")

    async def panic_sell_on(self, requests_per_second: int, duration_seconds: int) -> None:
        self._check("panic")
        self.on["panic"] = (requests_per_second, duration_seconds)

    async def panic_sell_off(self) -> bool:
        return await self._off("panic")
