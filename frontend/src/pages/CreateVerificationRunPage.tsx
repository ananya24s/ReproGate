import { AlertCircle } from "lucide-react";

import { IssueSummary } from "@/components/issue/IssueSummary";
import { IssueUrlForm } from "@/components/issue/IssueUrlForm";
import { useResolveIssue } from "@/hooks/useResolveIssue";

/**
 * Entry point of the workflow: accepts a GitHub issue URL and resolves it into
 * the repository and issue that a verification run would target.
 */
export function CreateVerificationRunPage() {
  const resolveIssue = useResolveIssue();

  return (
    <section className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Verify an issue
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Submit a GitHub issue URL to reproduce it inside an isolated sandbox.
        </p>
      </header>

      <IssueUrlForm
        onSubmit={(issueUrl) => resolveIssue.mutate(issueUrl)}
        isPending={resolveIssue.isPending}
        hasError={resolveIssue.isError}
      />

      <div aria-live="polite" aria-busy={resolveIssue.isPending}>
        {resolveIssue.isPending ? (
          <p className="text-sm text-muted-foreground">
            Resolving the issue on GitHub…
          </p>
        ) : null}

        {resolveIssue.isError ? (
          <div
            id="issue-url-error"
            role="alert"
            className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
          >
            <AlertCircle
              className="mt-0.5 size-4 shrink-0 text-destructive"
              aria-hidden="true"
            />
            <div>
              <p className="font-medium">Could not resolve that issue</p>
              <p className="mt-1 text-muted-foreground">
                {resolveIssue.error.message}
              </p>
            </div>
          </div>
        ) : null}

        {resolveIssue.isSuccess ? (
          <IssueSummary lookup={resolveIssue.data} />
        ) : null}
      </div>
    </section>
  );
}
