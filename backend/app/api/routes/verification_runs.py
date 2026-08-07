"""HTTP endpoints for verification runs.

Handlers validate input and delegate to the verification orchestrator. No
repository analysis, LLM, Docker, or classification logic belongs here.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.schemas.decision import HumanDecision, HumanDecisionCreate
from app.schemas.verification import (
    VerificationRunCreate,
    VerificationRunCreated,
    VerificationRunState,
)

router = APIRouter(prefix="/verification-runs", tags=["verification-runs"])


@router.post(
    "",
    response_model=VerificationRunCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a verification run",
)
async def create_verification_run(
    payload: VerificationRunCreate,
) -> VerificationRunCreated:
    """Accept an issue for verification and return the run identifier.

    Verification runs asynchronously; clients poll
    :func:`get_verification_run` until the run reaches a terminal state.
    """
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)


@router.get(
    "/{run_id}",
    response_model=VerificationRunState,
    summary="Get verification run status",
)
async def get_verification_run(run_id: UUID) -> VerificationRunState:
    """Return the current lifecycle state of a verification run."""
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{run_id}/report", summary="Get the full verification report")
async def get_verification_report(run_id: UUID) -> dict:
    """Return repository, issue, generated test, metrics, evidence, and
    classification for a completed run."""
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{run_id}/logs", summary="Get sandbox execution logs")
async def get_verification_logs(run_id: UUID) -> dict:
    """Return the Docker execution logs captured for a run."""
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/{run_id}/generated-test", summary="Get the generated reproduction test")
async def get_generated_test(run_id: UUID) -> dict:
    """Return the reproduction test generated for a run."""
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)


@router.post(
    "/{run_id}/decision",
    response_model=HumanDecision,
    status_code=status.HTTP_201_CREATED,
    summary="Record the human decision",
)
async def create_human_decision(
    run_id: UUID,
    payload: HumanDecisionCreate,
) -> HumanDecision:
    """Store the reviewer's final approval or rejection for a run."""
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED)
