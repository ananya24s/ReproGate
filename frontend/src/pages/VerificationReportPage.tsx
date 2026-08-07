import { useParams } from "react-router-dom";

/**
 * Evidence view for a completed run: repository and issue details, generated
 * test, execution metrics, logs, classification, and the human decision.
 */
export function VerificationReportPage() {
  const { runId } = useParams<{ runId: string }>();

  return (
    <section>
      <h1 className="text-2xl font-semibold tracking-tight">
        Verification report
      </h1>
      <p className="mt-2 font-mono text-sm text-muted-foreground">{runId}</p>
    </section>
  );
}
