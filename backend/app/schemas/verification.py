"""Request and response models for the verification API."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class VerificationRunStatus(str, Enum):
    """Lifecycle states of a verification run."""

    QUEUED = "QUEUED"
    CLONING = "CLONING"
    ANALYZING = "ANALYZING"
    GENERATING_TEST = "GENERATING_TEST"
    EXECUTING = "EXECUTING"
    BUILDING_EVIDENCE = "BUILDING_EVIDENCE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class VerificationRunCreate(BaseModel):
    """Input accepted when starting a new verification run."""

    model_config = ConfigDict(extra="forbid")

    issue_url: HttpUrl = Field(
        description="URL of the GitHub issue to reproduce.",
        examples=["https://github.com/owner/repo/issues/42"],
    )


class VerificationRunCreated(BaseModel):
    """Acknowledgement returned immediately after a run is accepted."""

    verification_run_id: UUID
    status: VerificationRunStatus


class VerificationRunState(BaseModel):
    """Current state of a verification run, returned to polling clients."""

    verification_run_id: UUID
    status: VerificationRunStatus
