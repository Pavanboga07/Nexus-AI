"use client";

/**
 * Entry point: choose a destination based on whether a session exists.
 *
 * The API enforces authentication, so sending a signed-out visitor to the
 * inbox would show a wall of 401s. The inbox is the default landing place for
 * a signed-in user because it answers "what needs me?" - which is the reason to
 * open the app at all.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { getAuthStatus } from "@/lib/api/auth";

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getAuthStatus();
        if (cancelled) return;
        const signedIn = status.authenticated || !status.auth_required;
        router.replace(signedIn ? "/inbox" : "/login");
      } catch {
        if (!cancelled) router.replace("/login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  return (
    <main className="flex min-h-screen items-center justify-center">
      <p className="text-sm text-neutral-400">Loading…</p>
    </main>
  );
}
