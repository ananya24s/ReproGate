/**
 * Query key factory.
 *
 * All cache keys are declared here so invalidation stays consistent across
 * features.
 */

export const queryKeys = {
  verificationRuns: {
    all: ["verification-runs"] as const,
    detail: (runId: string) => ["verification-runs", runId] as const,
    report: (runId: string) => ["verification-runs", runId, "report"] as const,
    logs: (runId: string) => ["verification-runs", runId, "logs"] as const,
    generatedTest: (runId: string) =>
      ["verification-runs", runId, "generated-test"] as const,
  },
} as const;
