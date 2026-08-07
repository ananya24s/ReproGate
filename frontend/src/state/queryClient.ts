/**
 * Server-state configuration.
 *
 * Verification runs are asynchronous: the client creates a run, receives an
 * identifier, then polls until the run reaches a terminal state. React Query
 * owns that cache and its refetch behavior.
 */

import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";

const MAX_RETRIES = 2;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Client errors are deterministic; only retry transport and 5xx faults.
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          return false;
        }
        return failureCount < MAX_RETRIES;
      },
    },
    mutations: {
      retry: false,
    },
  },
});
