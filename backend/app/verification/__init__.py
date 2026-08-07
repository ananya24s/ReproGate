"""Owns the end-to-end verification workflow.

The orchestrator coordinates issue analysis, context retrieval, candidate
test generation, sandbox execution, evidence construction, and
classification."""

from app.verification.issue_analyzer import IssueAnalyzer

__all__ = ["IssueAnalyzer"]
