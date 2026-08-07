"""Request and response models for the human decision API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HumanDecisionCreate(BaseModel):
    """Final human approval or rejection of a verification run."""

    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(description="Whether the reviewer accepted the evidence.")
    reviewer_notes: str | None = Field(default=None, max_length=4000)


class HumanDecision(BaseModel):
    """A recorded human decision."""

    verification_run_id: UUID
    approved: bool
    reviewer_notes: str | None
    reviewed_at: datetime
