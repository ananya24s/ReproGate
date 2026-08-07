import { ExternalLink } from "lucide-react";

import type { GitHubIssueLookup, GitHubIssueState } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

const STATE_STYLES: Record<GitHubIssueState, string> = {
  open: "bg-emerald-600 text-white",
  closed: "bg-violet-600 text-white",
};

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatDate(isoDate: string): string {
  const parsed = new Date(isoDate);
  return Number.isNaN(parsed.getTime()) ? isoDate : dateFormatter.format(parsed);
}

interface IssueSummaryProps {
  lookup: GitHubIssueLookup;
}

export function IssueSummary({ lookup }: IssueSummaryProps) {
  const { repository, issue } = lookup;

  return (
    <Card>
      <CardHeader>
        <CardDescription className="flex items-center gap-2">
          <a
            href={repository.html_url}
            target="_blank"
            rel="noreferrer noopener"
            className="font-medium hover:text-foreground hover:underline"
          >
            {repository.full_name}
          </a>
          {repository.language ? (
            <Badge variant="outline">{repository.language}</Badge>
          ) : null}
        </CardDescription>

        <CardTitle className="flex flex-wrap items-baseline gap-x-2">
          <span>{issue.title}</span>
          <span className="font-normal text-muted-foreground">
            #{issue.number}
          </span>
        </CardTitle>

        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Badge className={cn("border-transparent", STATE_STYLES[issue.state])}>
            {issue.state}
          </Badge>
          {issue.labels.map((label) => (
            <Badge key={label} variant="secondary">
              {label}
            </Badge>
          ))}
          {issue.labels.length === 0 ? (
            <span className="text-xs text-muted-foreground">No labels</span>
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <dl className="grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Author
            </dt>
            <dd className="mt-1">{issue.author ?? "Unknown"}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Created
            </dt>
            <dd className="mt-1">
              <time dateTime={issue.created_at}>
                {formatDate(issue.created_at)}
              </time>
            </dd>
          </div>
        </dl>

        <a
          href={issue.html_url}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground hover:underline"
        >
          View on GitHub
          <ExternalLink className="size-3.5" aria-hidden="true" />
        </a>
      </CardContent>
    </Card>
  );
}
