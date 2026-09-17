"use client";

/**
 * The application shell.
 *
 * Navigation is derived from the four questions a person actually has about
 * their agent:
 *
 *   Inbox   what needs me?
 *   People  who can my agent talk to?
 *   Chat    what did I ask it?
 *   Agent   what is it, and what does it know?
 *
 * Everything else (policies, tools, workflows, autonomy, orchestration) is
 * reachable from the Agent page as detail, rather than competing for top-level
 * space. The previous UI put implementation nouns - "orchestration runs",
 * "workflow state", "a2a audit" - in the primary navigation, which asked the
 * user to learn the architecture before they could use it.
 */

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Bot,
  Inbox,
  LogOut,
  MessageSquare,
  Users,
} from "lucide-react";

import { getAuthStatus, logout } from "@/lib/api/auth";
import { listApprovals } from "@/lib/api/approvals";

type NavItem = {
  href: string;
  label: string;
  icon: typeof Inbox;
  /** Whether the pending-approval count is shown on this item. */
  badge?: boolean;
};

const NAV: NavItem[] = [
  { href: "/inbox", label: "Inbox", icon: Inbox, badge: true },
  { href: "/people", label: "People", icon: Users },
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/agent", label: "Agent", icon: Bot },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [pending, setPending] = useState<number | null>(null);
  const [signedIn, setSignedIn] = useState<boolean | null>(null);

  // A count of what needs the user is the one piece of state worth surfacing
  // globally: it is the reason to open the app.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getAuthStatus();
        if (cancelled) return;
        setSignedIn(status.authenticated || !status.auth_required);
        if (!status.auth_required && !status.authenticated) {
          // Development without auth: the API still works.
          setSignedIn(true);
        }
        if (!status.auth_required && !status.authenticated) return;
        const { items } = await listApprovals();
        if (!cancelled) setPending(items.length);
      } catch {
        // A nav badge must never break the page.
        if (!cancelled) setPending(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  async function onSignOut() {
    try {
      await logout();
    } finally {
      router.replace("/login");
    }
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="w-52 shrink-0 border-r border-neutral-800/70 bg-neutral-950 flex flex-col">
        <div className="px-4 py-4">
          <span className="text-sm font-semibold tracking-tight">Nexus</span>
        </div>

        <nav className="flex-1 px-2 space-y-0.5" aria-label="Main">
          {NAV.map(({ href, label, icon: Icon, badge }) => {
            const active = pathname === href || pathname.startsWith(`${href}/`);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
                  active
                    ? "bg-neutral-800/80 text-neutral-100"
                    : "text-neutral-400 hover:bg-neutral-800/40 hover:text-neutral-200"
                }`}
              >
                <Icon className="h-4 w-4" aria-hidden />
                <span className="flex-1">{label}</span>
                {badge && pending !== null && pending > 0 && (
                  <span
                    className="rounded-full bg-blue-600 px-1.5 py-0.5 text-[10px] font-semibold text-white"
                    aria-label={`${pending} waiting for you`}
                  >
                    {pending > 99 ? "99+" : pending}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        {signedIn && (
          <div className="border-t border-neutral-800/70 p-2">
            <button
              type="button"
              onClick={onSignOut}
              className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-neutral-500 transition-colors hover:bg-neutral-800/40 hover:text-neutral-300"
            >
              <LogOut className="h-4 w-4" aria-hidden />
              Sign out
            </button>
          </div>
        )}
      </aside>

      <main className="flex-1 overflow-hidden">{children}</main>
    </div>
  );
}
