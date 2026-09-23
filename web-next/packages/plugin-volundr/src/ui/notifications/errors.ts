/**
 * A readable reason for a failed call: the API's `detail` when the server gave
 * one (ApiClientError carries it separately from its generic message).
 */
export function describeError(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
  }
  return error instanceof Error && error.message ? error.message : fallback;
}
