"""Low-level authenticated GitHub HTTP client.

Owns transport concerns only: base URL, authentication, retries, rate-limit
handling, and error translation. It exposes no domain vocabulary."""
