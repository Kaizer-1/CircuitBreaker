"""The concurrency limit: at most `limit` requests in a pod at once; beyond it 503 OVERLOADED.

Without a limit an overloaded pod queues every request: all of them get slow, then time out. With
it the pod keeps the requests it can serve fast and turns the rest away in microseconds, so more
pods mean more capacity (chaos scenarios section 3.3).

It is checked after routing (a route class on the API's router), so a turned-away request is
counted under its route (`/v1/orders`) like the ones that got in. Probes and scrapes are on other
routers and are never turned away: a busy pod must not look dead to Kubernetes.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute

from order_service.runtime.problem import problem_response


class ConcurrencyLimit:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0


class LimitedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            limit: ConcurrencyLimit = request.app.state.concurrency_limit
            if limit.in_flight >= limit.limit:
                response = problem_response(
                    503,
                    "OVERLOADED",
                    "Overloaded",
                    detail=f"order-service is handling {limit.limit} requests; retry shortly.",
                    instance=request.url.path,
                )
                response.headers["Retry-After"] = "1"
                return response
            limit.in_flight += 1
            try:
                return await handler(request)
            finally:
                limit.in_flight -= 1

        return limited
