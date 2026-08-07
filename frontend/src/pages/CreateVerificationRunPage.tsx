/**
 * Entry point of the workflow: accepts a GitHub issue URL and starts a
 * verification run.
 */
export function CreateVerificationRunPage() {
  return (
    <section>
      <h1 className="text-2xl font-semibold tracking-tight">
        Verify an issue
      </h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Submit a GitHub issue URL to reproduce it inside an isolated sandbox.
      </p>
    </section>
  );
}
