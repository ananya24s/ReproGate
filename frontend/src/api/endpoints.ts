/**
 * Canonical REST paths exposed by the backend.
 *
 * Paths are declared once here so no component builds a URL by hand.
 */

export const endpoints = {
  health: () => "/health",

  issues: {
    resolve: () => "/issues/resolve",
  },

  verificationRuns: {
    create: () => "/verification-runs",
    detail: (runId: string) => `/verification-runs/${runId}`,
    report: (runId: string) => `/verification-runs/${runId}/report`,
    logs: (runId: string) => `/verification-runs/${runId}/logs`,
    generatedTest: (runId: string) => `/verification-runs/${runId}/generated-test`,
    decision: (runId: string) => `/verification-runs/${runId}/decision`,
  },
} as const;
