export class ApiError extends Error {
  status: number;
  data: unknown;

  constructor(message: string, status: number, data?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

const BASE_URL = ((): string => {
  const url = process.env.NEXT_PUBLIC_API_URL;
  if (!url) {
    if (process.env.NODE_ENV === "production") {
      // Fail fast: a production build without the API URL would silently
      // talk to localhost and fail in confusing ways.
      throw new Error(
        "NEXT_PUBLIC_API_URL is not set. Set it to your API origin " +
          "(e.g. https://api.example.com); refusing to silently fall back " +
          "to localhost in a production build."
      );
    }
    return "http://localhost:8000";
  }
  return url;
})();

/** Human-readable message from anything caught: Error, ApiError, or junk. */
export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message || err.name || "Unknown error";
  if (typeof err === "string") return err;
  try {
    return JSON.stringify(err);
  } catch {
    return "Unknown error";
  }
}

export async function apiFetch<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BASE_URL}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };

  try {
    const res = await fetch(url, {
      ...options,
      headers,
      // The API authenticates via an HttpOnly session cookie. Without this the
      // browser will not attach it cross-origin, and every data endpoint
      // returns 401. The backend allow-lists specific origins and enables
      // credentials for exactly those origins (never a wildcard).
      credentials: "include",
    });

    if (!res.ok) {
      let errorData: unknown;
      let errorMessage = `HTTP error ${res.status}`;
      try {
        errorData = await res.json();
        // The backend uses ONE error envelope (M5):
        //   {"error": {"code", "message", "kind", "details?"}}
        // Older endpoints used {"detail": "..."}; both are handled so a stale
        // response shape degrades to a useful message instead of "HTTP 400".
        if (typeof errorData === "object" && errorData !== null) {
          const body = errorData as Record<string, unknown>;
          const envelope = body.error;
          if (typeof envelope === "object" && envelope !== null) {
            const message = (envelope as Record<string, unknown>).message;
            if (typeof message === "string" && message) {
              errorMessage = message;
            }
          } else if (typeof envelope === "string" && envelope) {
            errorMessage = envelope;
          } else {
            const detail = body.detail;
            if (typeof detail === "string") {
              errorMessage = detail;
            } else if (typeof detail === "object" && detail !== null) {
              errorMessage = JSON.stringify(detail);
            }
          }
        }
      } catch {
        errorMessage = res.statusText || errorMessage;
      }

      if (res.status === 401) {
        // Expired (or missing) session: the HttpOnly cookie is stale, so any
        // client-side state derived from it is stale too. Full navigation —
        // not router-push — because router state may be stale as well. The
        // login page itself is exempt: a failed sign-in also returns 401 and
        // must stay on the form instead of reloading it in a loop. Guarded
        // for SSR/tests where `window` does not exist.
        // NOTE: there is no local auth token to clear (grep: the only
        // localStorage key is the chat session id, not auth; the session
        // lives in an HttpOnly cookie the backend clears on logout/expiry).
        if (
          typeof window !== "undefined" &&
          window.location.pathname !== "/login"
        ) {
          window.location.assign("/login");
        }
      }

      throw new ApiError(errorMessage, res.status, errorData);
    }

    if (res.status === 204) {
      return {} as T;
    }

    return await res.json();
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      throw err;
    }
    const message = err instanceof Error ? err.message : "Network error";
    throw new ApiError(`Connection to Nexus backend failed: ${message}`, 0);
  }
}
