import { Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface IssueUrlFormProps {
  onSubmit: (issueUrl: string) => void;
  isPending: boolean;
  hasError: boolean;
}

export function IssueUrlForm({
  onSubmit,
  isPending,
  hasError,
}: IssueUrlFormProps) {
  const [issueUrl, setIssueUrl] = useState("");
  const trimmed = issueUrl.trim();

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (trimmed && !isPending) {
      onSubmit(trimmed);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
      <div className="flex-1">
        <label htmlFor="issue-url" className="sr-only">
          GitHub issue URL
        </label>
        <Input
          id="issue-url"
          name="issue-url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://github.com/owner/repo/issues/42"
          value={issueUrl}
          aria-invalid={hasError || undefined}
          aria-describedby={hasError ? "issue-url-error" : undefined}
          disabled={isPending}
          onChange={(event) => setIssueUrl(event.target.value)}
        />
      </div>
      <Button type="submit" disabled={!trimmed || isPending}>
        {isPending ? (
          <>
            <Loader2 className="animate-spin" aria-hidden="true" />
            Resolving
          </>
        ) : (
          "Resolve issue"
        )}
      </Button>
    </form>
  );
}
