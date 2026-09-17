import { AppShell } from "@/components/layout/AppShell";

/**
 * The authenticated application.
 *
 * A route group so the shell wraps every signed-in page but NOT /login: a
 * login form with a navigation sidebar (and a "Sign out" button) is a small but
 * real usability bug, and a route group is how Next.js expresses "same URL,
 * different chrome".
 *
 * This layout is a server component; the interactive parts (badge counts, sign
 * out) live in AppShell, which is a client component.
 */
export default function AppGroupLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <AppShell>{children}</AppShell>;
}
