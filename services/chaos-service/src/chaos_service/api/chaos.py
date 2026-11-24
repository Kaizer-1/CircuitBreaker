"""Q-21, A-10, Q-22, Q-23, A-11 and A-20 (api/chaos-service.yaml). Admin only: api-gateway enforces
the admin role and sets X-Role; the service checks it again."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from chaos_service.application.lab import AlreadyRunningError, ChaosLab, NotRunningError, Reset
from chaos_service.domain.scenarios import SCENARIOS, Experiment, Scenario, ScenarioId
from chaos_service.ports.targets import TargetUnavailableError
from chaos_service.runtime.problem import FieldError, problem_response

router = APIRouter()
Role = Annotated[str | None, Header(alias="X-Role")]


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_rate: float | None = Field(default=None, ge=0.05, le=0.9)
    rate_mb_per_minute: int | None = Field(default=None, ge=1, le=200)
    requests_per_second: int | None = Field(default=None, ge=50, le=1000)
    delay_ms: int | None = Field(default=None, ge=100, le=5000)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioId
    duration_seconds: int | None = Field(default=None, ge=60, le=900)
    parameters: Parameters | None = None


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def scenario_view(s: Scenario) -> dict[str, Any]:
    return {
        "scenario": s.scenario.value,
        "name": s.name,
        "description": s.description,
        "target_service": s.target_service,
        "expected_fix": s.expected_fix,
        "default_duration_seconds": s.default_duration_seconds,
        "default_parameters": s.defaults,
    }


def experiment_view(e: Experiment) -> dict[str, Any]:
    return {
        "experiment_id": e.experiment_id,
        "scenario": e.scenario.scenario.value,
        "target_service": e.scenario.target_service,
        "status": e.status.value,
        "parameters": e.parameters,
        "started_at": _iso(e.started_at),
        "ends_at": _iso(e.ends_at),
        "ended_at": _iso(e.ended_at),
        "incident_id": e.incident_id,
        "time_to_detect_seconds": e.time_to_detect_seconds,
        "time_to_recover_seconds": e.time_to_recover_seconds,
        "agent_actions": e.agent_actions,
        "timeline": [{"at": _iso(t.at), "message": t.message} for t in e.timeline],
    }


def reset_view(r: Reset) -> dict[str, Any]:
    return {"reset_at": _iso(r.at), "undone": r.undone, "failed": r.failed}


def _lab(request: Request) -> ChaosLab:
    lab: ChaosLab = request.app.state.lab
    return lab


def _forbidden(request: Request) -> JSONResponse:
    return problem_response(
        403,
        "FORBIDDEN",
        "Forbidden",
        detail="Chaos experiments are for the admin role only.",
        instance=request.url.path,
    )


def _not_found(request: Request) -> JSONResponse:
    return problem_response(
        404, "NOT_FOUND", "Not found", detail="No such experiment.", instance=request.url.path
    )


@router.get(
    "/v1/chaos/scenarios",
    operation_id="listChaosScenarios",
    summary="List chaos scenarios",
    openapi_extra={"x-seeds-id": "Q-21"},
)
async def list_scenarios(request: Request, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    return JSONResponse({"scenarios": [scenario_view(s) for s in ChaosLab.scenarios()]})


@router.post(
    "/v1/chaos/experiments",
    operation_id="startChaosExperiment",
    summary="Start a chaos experiment",
    openapi_extra={"x-seeds-id": "A-10"},
)
async def start_experiment(request: Request, body: StartBody, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    given = body.parameters.model_dump(exclude_none=True) if body.parameters else {}
    scenario = SCENARIOS[body.scenario]
    if stray := sorted(set(given) - set(scenario.defaults)):
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail=f"{body.scenario.value} takes only {', '.join(scenario.defaults)}.",
            instance=request.url.path,
            errors=[
                FieldError(field=f"parameters.{k}", message="not for this scenario") for k in stray
            ],
        )
    try:
        experiment = await _lab(request).start(body.scenario, body.duration_seconds, given)
    except AlreadyRunningError as exc:
        other = exc.running
        shared = ", ".join(sorted(other.scenario.affects & scenario.affects))
        return problem_response(
            409,
            "EXPERIMENT_ALREADY_RUNNING",
            "Experiment already running",
            detail=(
                f"Experiment {other.experiment_id} ({other.scenario.scenario.value}) already"
                f" affects {shared}. Stop it first."
            ),
            instance=request.url.path,
        )
    except TargetUnavailableError as exc:
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Target unavailable",
            detail=f"The experiment did not start: {exc}",
            instance=request.url.path,
        )
    return JSONResponse(experiment_view(experiment), status_code=201)


@router.get(
    "/v1/chaos/experiments",
    operation_id="listChaosExperiments",
    summary="List chaos experiments",
    openapi_extra={"x-seeds-id": "Q-22"},
)
async def list_experiments(request: Request, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    lab = _lab(request)
    return JSONResponse(
        {
            "items": [experiment_view(e) for e in reversed(lab.experiments)],
            "resets": [reset_view(r) for r in reversed(lab.resets)],
        }
    )


@router.get(
    "/v1/chaos/experiments/{experiment_id}",
    operation_id="getChaosExperiment",
    summary="Get one chaos experiment",
    openapi_extra={"x-seeds-id": "Q-23"},
)
async def get_experiment(request: Request, experiment_id: str, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    experiment = _lab(request).get(experiment_id)
    if experiment is None:
        return _not_found(request)
    return JSONResponse(experiment_view(experiment))


@router.post(
    "/v1/chaos/experiments/{experiment_id}/stop",
    operation_id="stopChaosExperiment",
    summary="Stop a chaos experiment",
    openapi_extra={"x-seeds-id": "A-11"},
)
async def stop_experiment(request: Request, experiment_id: str, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    try:
        experiment = await _lab(request).stop(experiment_id)
    except NotRunningError as exc:
        return problem_response(
            409,
            "EXPERIMENT_NOT_RUNNING",
            "Experiment not running",
            detail=f"Experiment {experiment_id} already ended ({exc.experiment.status.value}).",
            instance=request.url.path,
        )
    except TargetUnavailableError as exc:
        return problem_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "Target unavailable",
            detail=f"The fault could not be undone: {exc}. Try again, or reset.",
            instance=request.url.path,
        )
    if experiment is None:
        return _not_found(request)
    return JSONResponse(experiment_view(experiment))


@router.post(
    "/v1/chaos/reset",
    operation_id="resetChaos",
    summary="Reset all chaos",
    openapi_extra={"x-seeds-id": "A-20"},
)
async def reset(request: Request, role: Role = None) -> JSONResponse:
    if role != "admin":
        return _forbidden(request)
    return JSONResponse(reset_view(await _lab(request).reset()))
