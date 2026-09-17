import { apiFetch } from "./client";

export interface AuthUser {
  owner_id: string;
  email: string;
  display_name?: string | null;
}

export interface AuthStatus {
  authenticated: boolean;
  auth_required: boolean;
  registration_open: boolean;
  user?: AuthUser | null;
}

export interface SessionResult {
  token: string;
  user: AuthUser;
  expires_at: string;
}

/** Whether auth is enforced and whether this browser is signed in. */
export async function getAuthStatus(): Promise<AuthStatus> {
  return apiFetch<AuthStatus>("/auth/status");
}

export async function register(
  email: string,
  password: string,
  displayName?: string
): Promise<SessionResult> {
  return apiFetch<SessionResult>("/auth/register", {
    method: "POST",
    body: JSON.stringify({
      email,
      password,
      display_name: displayName || null,
    }),
  });
}

export async function login(
  email: string,
  password: string
): Promise<SessionResult> {
  return apiFetch<SessionResult>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<{ logged_out: boolean }> {
  return apiFetch<{ logged_out: boolean }>("/auth/logout", { method: "POST" });
}

export async function me(): Promise<AuthUser> {
  return apiFetch<AuthUser>("/auth/me");
}

export async function changePassword(
  currentPassword: string,
  newPassword: string
): Promise<{ message: string }> {
  return apiFetch<{ message: string }>("/auth/password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}
