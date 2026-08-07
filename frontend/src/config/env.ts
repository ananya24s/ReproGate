/**
 * Typed access to build-time environment configuration.
 *
 * Vite inlines `import.meta.env` at build time, so these values are baked into
 * the bundle. Never place secrets here.
 */

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export const env = {
  /** Origin of the ReproGate backend, without a trailing slash. */
  apiBaseUrl: required(
    "VITE_API_BASE_URL",
    import.meta.env.VITE_API_BASE_URL,
  ).replace(/\/+$/, ""),

  /** Version prefix of the backend REST API. */
  apiVersionPrefix: import.meta.env.VITE_API_VERSION_PREFIX ?? "/api/v1",

  /** Interval, in milliseconds, at which in-progress runs are re-polled. */
  runPollIntervalMs: Number(import.meta.env.VITE_RUN_POLL_INTERVAL_MS ?? 2000),

  isProduction: import.meta.env.PROD,
} as const;
