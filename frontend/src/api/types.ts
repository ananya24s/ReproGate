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

export type GitHubIssueState = "open" | "closed";

export interface GitHubRepository {
  owner: string;
  name: string;
  full_name: string;
  default_branch: string;
  clone_url: string;
  html_url: string;
  language: string | null;
  description: string | null;
  is_private: boolean;
  is_archived: boolean;
  is_fork: boolean;
}

export interface GitHubIssue {
  number: number;
  title: string;
  body: string | null;
  state: GitHubIssueState;
  author: string | null;
  labels: string[];
  html_url: string;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface GitHubIssueLookupRequest {
  issue_url: string;
}

/** Repository and issue metadata resolved from an issue URL. */
export interface GitHubIssueLookup {
  repository: GitHubRepository;
  issue: GitHubIssue;
}

/** Error envelope returned by the backend exception handlers. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
  };
}
