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

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
