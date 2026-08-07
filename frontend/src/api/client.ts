/**
 * Transport layer for the ReproGate backend.
 *
 * This module owns URL construction, headers, JSON encoding, and error
 * translation. It defines no domain operations — feature modules compose
 * `apiRequest` with the paths declared in `endpoints.ts`.
 */

import type { ApiErrorBody } from "@/api/types";
import { env } from "@/config/env";

/** An error response returned by the backend, or a transport failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface ApiRequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as ApiErrorBody).error?.code === "string"
  );
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON error responses fall through to the status text below.
  }

  if (isApiErrorBody(body)) {
    return new ApiError(response.status, body.error.code, body.error.message);
  }
  return new ApiError(
    response.status,
    "http_error",
    response.statusText || `Request failed with status ${response.status}`,
  );
}

/**
 * Issue a JSON request against the versioned backend API.
 *
 * @param path Path relative to the API version prefix, from `endpoints.ts`.
 * @throws {ApiError} When the network fails or the backend returns a non-2xx status.
 */
export async function apiRequest<TResponse>(
  path: string,
  { method = "GET", body, signal }: ApiRequestOptions = {},
): Promise<TResponse> {
  const url = `${env.apiBaseUrl}${env.apiVersionPrefix}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      signal,
      headers: {
        Accept: "application/json",
        ...(body === undefined
          ? {}
          : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(
      0,
      "network_error",
      cause instanceof Error ? cause.message : "The request could not be sent.",
    );
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) {
    return undefined as TResponse;
  }

  return (await response.json()) as TResponse;
}
