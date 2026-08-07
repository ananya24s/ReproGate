/** Application route paths, declared once and referenced everywhere. */

export const paths = {
  home: "/",
  verificationRun: (runId = ":runId") => `/verification-runs/${runId}`,
  verificationRunReport: (runId = ":runId") =>
    `/verification-runs/${runId}/report`,
} as const;
