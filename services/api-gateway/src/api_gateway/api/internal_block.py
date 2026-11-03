"""The gateway never routes /internal/ (service-to-service only; coding standards section 7)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api_gateway.runtime.problem import problem_response

router = APIRouter()


@router.api_route(
    "/internal/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def block_internal(request: Request, rest: str) -> JSONResponse:
    # Answer exactly like an unknown path so a caller learns nothing about internal routes.
    return problem_response(
        404, "NOT_FOUND", "Resource not found", detail="Not Found", instance=request.url.path
    )
