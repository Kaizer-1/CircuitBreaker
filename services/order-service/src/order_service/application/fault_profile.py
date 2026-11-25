"""FAULT_PROFILE: chaos-service's bad deploy (chaos scenarios section 3.1).

chaos-service patches the Deployment with FAULT_PROFILE=bad-release, which makes a new rollout
revision like any release. Its pods then fail a share of order placements with an unhandled error
from a believable "forgot a config value" bug, before anything is reserved. The image is the same;
from Kubernetes' and the SRE agent's point of view it is a new revision whose pods misbehave, and
rolling back restores the pod template without the variable.
"""

import random
from collections.abc import Callable
from typing import Literal

FaultProfileName = Literal["", "bad-release"]


class FaultProfile:
    def __init__(
        self,
        name: FaultProfileName = "",
        error_rate: float = 0.4,
        draw: Callable[[], float] = random.random,
    ) -> None:
        self.name = name
        self.error_rate = error_rate
        self._draw = draw

    def before_place(self) -> None:
        """Raise, like the bad release's code would, for `error_rate` of placements."""
        if self.name == "bad-release" and self._draw() < self.error_rate:
            raise ValueError("price_band not configured")
