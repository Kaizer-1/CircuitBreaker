"""No business API: only /healthz, /readyz and /metrics (docs/05-coding-standards.md)."""

from fastapi import APIRouter

router = APIRouter()
