import { useMutation } from "@tanstack/react-query";

import { apiRequest, type ApiError } from "@/api/client";
import { endpoints } from "@/api/endpoints";
import type { GitHubIssueLookup, GitHubIssueLookupRequest } from "@/api/types";

/**
 * Resolves a GitHub issue URL into repository and issue metadata.
 *
 * A mutation rather than a query: resolution is triggered by the user
 * submitting the form, not by rendering a route.
 */
export function useResolveIssue() {
  return useMutation<GitHubIssueLookup, ApiError, string>({
    mutationFn: (issueUrl) => {
      const body: GitHubIssueLookupRequest = { issue_url: issueUrl };
      return apiRequest<GitHubIssueLookup>(endpoints.issues.resolve(), {
        method: "POST",
        body,
      });
    },
  });
}
