/**
 * The API's error envelope.
 *
 * The backend uses ONE shape for every expected failure (M5):
 *
 *   {"error": {"code": "not_found", "message": "...", "kind": "...",
 *              "details": {...}}}
 *
 * Reading `detail` instead (as the UI used to) meant every failure rendered as
 * a bare "HTTP 400" with no explanation.
 */
export type ApiErrorBody = {
  error: {
    code: string;
    message: string;
    kind: string;
    details?: Record<string, unknown>;
  };
};

/** A short, human-readable summary of a detail bag. */
export function formatErrorDetails(details?: Record<string, unknown>): string | null {
  if (!details) return null;
  if (Array.isArray(details.errors)) {
    const first = details.errors[0] as Record<string, unknown> | undefined;
    if (first && typeof first.msg === "string") {
      const loc = Array.isArray(first.loc) ? first.loc.join(".") : "";
      return loc ? `${loc}: ${first.msg}` : first.msg;
    }
  }
  const entries = Object.entries(details);
  if (entries.length === 0) return null;
  return entries
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join(", ");
}
