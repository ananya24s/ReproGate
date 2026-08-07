import { useParams } from "react-router-dom";

/**
 * Progress view for a single run. Polls the run until it reaches a terminal
 * state, then links through to the report.
 */
export function VerificationRunPage() {
  const { runId } = useParams<{ runId: string }>();

  return (
    <section>
      <h1 className="text-2xl font-semibold tracking-tight">
        Verification run
      </h1>
      <p className="mt-2 font-mono text-sm text-muted-foreground">{runId}</p>
    </section>
  );
}
