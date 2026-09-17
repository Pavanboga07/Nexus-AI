"use client";

/**
 * Sign-in / registration.
 *
 * The API enforces authentication (NEXUS_AUTH_REQUIRED, default true outside
 * development), so the app cannot be used without a session. This page is the
 * minimum needed to obtain one; the full redesign (Inbox-first information
 * architecture) is tracked separately.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { getAuthStatus, login, register } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";

type Mode = "login" | "register";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checked, setChecked] = useState(false);
  const [registrationOpen, setRegistrationOpen] = useState(true);

  // If a session already exists, or auth is not enforced, skip sign-in.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getAuthStatus();
        if (cancelled) return;
        setRegistrationOpen(status.registration_open);
        if (status.authenticated) {
          router.replace("/inbox");
          return;
        }
        if (!status.auth_required) {
          router.replace("/inbox");
          return;
        }
      } catch {
        // Backend unreachable: stay on the page and let submit surface it.
      } finally {
        if (!cancelled) setChecked(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "register") {
        await register(email, password, displayName || undefined);
      } else {
        await login(email, password);
      }
      router.replace("/inbox");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Something went wrong. Please try again."
      );
    } finally {
      setBusy(false);
    }
  }

  if (!checked) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-neutral-400">Checking session…</p>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold">Nexus</h1>
        <p className="mt-1 text-sm text-neutral-400">
          {mode === "login"
            ? "Sign in to your agent."
            : "Create an account to get started."}
        </p>

        <form onSubmit={onSubmit} className="mt-5 space-y-3">
          <div>
            <label htmlFor="email" className="block text-xs text-neutral-400">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500"
            />
          </div>

          {mode === "register" && (
            <div>
              <label
                htmlFor="displayName"
                className="block text-xs text-neutral-400"
              >
                Name (optional)
              </label>
              <input
                id="displayName"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500"
              />
            </div>
          )}

          <div>
            <label htmlFor="password" className="block text-xs text-neutral-400">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              autoComplete={
                mode === "login" ? "current-password" : "new-password"
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500"
            />
            {mode === "register" && (
              <p className="mt-1 text-xs text-neutral-500">
                At least 10 characters.
              </p>
            )}
          </div>

          {error && (
            <p role="alert" className="text-xs text-red-400">
              {error}
            </p>
          )}

          <Button type="submit" disabled={busy} className="w-full">
            {busy
              ? "Please wait…"
              : mode === "login"
                ? "Sign in"
                : "Create account"}
          </Button>
        </form>

        {registrationOpen && (
          <button
            type="button"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError(null);
            }}
            className="mt-4 w-full text-center text-xs text-neutral-400 underline-offset-2 hover:underline"
          >
            {mode === "login"
              ? "Need an account? Create one"
              : "Already have an account? Sign in"}
          </button>
        )}
      </Card>
    </main>
  );
}
