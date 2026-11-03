"""Error bodies in RFC 9457 Problem Details format (api/shared/common.yaml, schema `Problem`)."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from market_data_service.runtime.jsonlog import correlation_id_var

PROBLEM_MEDIA_TYPE = "application/problem+json"


class FieldError(BaseModel):
    field: str
    message: str


class Problem(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None
    correlation_id: str | None = None
    errors: list[FieldError] | None = None


def problem_response(
    status: int,
    code: str,
    title: str,
    *,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=f"https://circuitbreaker.local/errors/{code.lower().replace('_', '-')}",
        title=title,
        status=status,
        code=code,
        detail=detail,
        instance=instance,
        correlation_id=correlation_id_var.get(),
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_MEDIA_TYPE
    )


def not_implemented(request: Request, seeds_id: str) -> JSONResponse:
    """Phase 1 stubs: the operation is in the API spec but has no logic yet (contract first)."""
    return problem_response(
        501,
        "NOT_IMPLEMENTED",
        "Not implemented yet",
        detail=(
            f"{request.method} {request.url.path} ({seeds_id}) is defined in the API spec "
            "but not built yet."
        ),
        instance=request.url.path,
    )


def _field_name(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ("body", "query", "path", "header"):
        parts = parts[1:]
    return ".".join(parts) or "request"


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "BAD_REQUEST"
        return problem_response(
            exc.status_code,
            code,
            "Resource not found" if exc.status_code == 404 else "Bad request",
            detail=str(exc.detail),
            instance=request.url.path,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(field=_field_name(tuple(e["loc"])), message=e["msg"]) for e in exc.errors()
        ]
        return problem_response(
            422,
            "VALIDATION_FAILED",
            "Validation failed",
            detail="One or more fields are invalid.",
            instance=request.url.path,
            errors=errors,
        )
