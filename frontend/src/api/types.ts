/**
 * Transport types mirroring the backend REST contract.
 *
 * These must stay in sync with `backend/app/schemas`.
 */

/** Lifecycle states of a verification run. */
export const VERIFICATION_RUN_STATUSES = [
  "QUEUED",
  "CLONING",
  "ANALYZING",
  "GENERATING_TEST",
  "EXECUTING",
  "BUILDING_EVIDENCE",
  "COMPLETED",
  "FAILED",
] as const;

export type VerificationRunStatus = (typeof VERIFICATION_RUN_STATUSES)[number];

/** States after which a run no longer changes and polling should stop. */
export const TERMINAL_RUN_STATUSES: readonly VerificationRunStatus[] = [
  "COMPLETED",
  "FAILED",
];

export interface VerificationRunCreate {
  issue_url: string;
}

export interface VerificationRunCreated {
  verification_run_id: string;
  status: VerificationRunStatus;
}

export interface VerificationRunState {
  verification_run_id: string;
  status: VerificationRunStatus;
}

export interface HumanDecisionCreate {
  approved: boolean;
  reviewer_notes?: string | null;
}

export interface HumanDecision {
  verification_run_id: string;
  approved: boolean;
  reviewer_notes: string | null;
  reviewed_at: string;
}

/** Error envelope returned by the backend exception handlers. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
  };
}
