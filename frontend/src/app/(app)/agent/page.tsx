"use client";

/**
 * Agent - "what is my agent, and what does it know?"
 *
 * Identity first, because that is what makes the agent a thing rather than a
 * feature: its fingerprint is what a peer pins, and its public key is what
 * makes its messages verifiable. Then memory, then the detail screens
 * (permissions, tools) which used to occupy the top-level navigation.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AlertCircle, Copy, Check, Fingerprint } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/ErrorState";
import { ApiError, apiFetch } from "@/lib/api/client";
import { changePassword } from "@/lib/api/auth";
import {
  getAgentCard,
  getCapabilities,
  CapabilityInfo,
} from "@/lib/api/identity";
import { getStatus, StatusResponse } from "@/lib/api/status";
import { AgentCard } from "@/types/api";
import { formatDate } from "@/lib/utils";

type Identity = {
  agent_id: string;
  public_key: string;
  fingerprint: string;
};

export default function AgentPage() {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityInfo[] | null>(
    null
  );
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  /** Capability fetch failure is distinct from genuinely empty (see below). */
  const [capError, setCapError] = useState<string | null>(null);
  /** Status fetch failure must not hide the grid silently. */
  const [statusError, setStatusError] = useState<string | null>(null);
  /** The signed agent card others fetch to connect (invite path). */
  const [card, setCard] = useState<AgentCard | null>(null);
  const [cardError, setCardError] = useState<string | null>(null);
  /** Change-password form state. */
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwNotice, setPwNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setCapError(null);
    setStatusError(null);
    setCardError(null);
    let id: Identity;
    try {
      id = await apiFetch<Identity>("/identity");
      setIdentity(id);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not read your agent's identity."
      );
      return;
    }
    // Status, capabilities and card are independent: one failing must hide
    // neither the others nor masquerade as "empty".
    const [stRes, capRes, cardRes] = await Promise.allSettled([
      getStatus(),
      getCapabilities(),
      getAgentCard(),
    ]);
    if (stRes.status === "fulfilled") {
      setStatus(stRes.value);
    } else {
      setStatus(null);
      setStatusError(
        stRes.reason instanceof ApiError
          ? stRes.reason.message
          : "Couldn't reach the server."
      );
    }
    if (capRes.status === "fulfilled") {
      setCapabilities(capRes.value.capabilities);
    } else {
      setCapabilities(null);
      setCapError(
        capRes.reason instanceof ApiError
          ? capRes.reason.message
          : "Couldn't reach the server."
      );
    }
    if (cardRes.status === "fulfilled") {
      setCard(cardRes.value);
    } else {
      setCard(null);
      setCardError(
        cardRes.reason instanceof ApiError
          ? cardRes.reason.message
          : "Couldn't reach the server."
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      setError("Could not copy to the clipboard.");
    }
  }

  async function onChangePassword(event: React.FormEvent) {
    event.preventDefault();
    if (pwBusy) return;
    setPwError(null);
    setPwNotice(null);
    // Mirrors the backend's MIN_PASSWORD_LENGTH (10) so a doomed request is
    // caught here; the server still validates.
    if (newPw.length < 10) {
      setPwError("Use at least 10 characters for the new password.");
      return;
    }
    if (newPw !== confirmPw) {
      setPwError("The new passwords do not match.");
      return;
    }
    setPwBusy(true);
    try {
      await changePassword(currentPw, newPw);
      setPwNotice("Password changed.");
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
    } catch (err) {
      setPwError(
        err instanceof ApiError ? err.message : "Could not change password."
      );
    } finally {
      setPwBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <header className="border-b border-neutral-800/60 px-6 py-4">
        <h1 className="text-sm font-semibold">Your agent</h1>
        <p className="text-[11px] text-neutral-500">
          What it is, and what it knows about you.
        </p>
      </header>

      <div className="mx-auto w-full max-w-3xl space-y-8 px-6 py-6">
        {error && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-xs text-red-300"
          >
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        )}

        {/* --- Identity --- */}
        <section>
          <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            <Fingerprint className="h-3.5 w-3.5" aria-hidden />
            Identity
          </h2>
          {!identity ? (
            <div className="h-28 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40" />
          ) : (
            <div className="space-y-3 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-4">
              <Field
                label="Fingerprint"
                value={identity.fingerprint}
                onCopy={() => copy("fingerprint", identity.fingerprint)}
                copied={copied === "fingerprint"}
                hint="What a contact compares to confirm they connected to the real you."
              />
              <Field
                label="Agent ID"
                value={identity.agent_id}
                mono
                onCopy={() => copy("agent_id", identity.agent_id)}
                copied={copied === "agent_id"}
                hint="Derived from the public key, so it cannot be claimed by anyone else."
              />
              <Field
                label="Public key"
                value={identity.public_key}
                mono
                onCopy={() => copy("public_key", identity.public_key)}
                copied={copied === "public_key"}
              />
              <p className="text-[11px] text-neutral-500">
                Your private key never leaves this deployment. Messages are
                signed locally, so a relay cannot forge them.
              </p>
            </div>
          )}
        </section>

        {/* --- Invite someone --- */}
        <section>
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Invite someone
          </h2>
          {cardError ? (
            <ErrorState message={cardError} onRetry={() => void load()} />
          ) : card === null ? (
            <div className="h-20 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40" />
          ) : (
            <div className="space-y-3 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-4">
              <Field
                label="Agent card URL"
                value={cardUrlFor(card) ?? "Unavailable — card has no endpoint"}
                mono
                onCopy={() => {
                  const url = cardUrlFor(card);
                  if (url) void copy("card_url", url);
                }}
                copied={copied === "card_url"}
                hint="Share this URL: a peer fetches the signed card and verifies it before connecting."
              />
              <p className="text-[11px] text-neutral-500">
                Card valid until {formatDate(card.expires_at)}. Directory
                listing is opt-in — this deployment appears in the gateway
                directory only when the operator configures a gateway URL.
              </p>
            </div>
          )}
        </section>

        {/* --- Subsystem status --- */}
        {/* A fetch failure shows an error with Retry in place of the grid —
            never a silently missing section. */}
        {!status && statusError && (
          <section>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              Status
            </h2>
            <ErrorState message={statusError} onRetry={() => void load()} />
          </section>
        )}
        {status && (
          <section>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              Status
            </h2>
            <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {[
                ["Model", status.llm_configured ? status.llm_provider : "not configured"],
                ["Memory", status.memory ? "on" : "off"],
                ["Identity", status.identity ? "ready" : "unavailable"],
                ["Agent messaging", status.a2a ? "ready" : "unavailable"],
                ["Gateway relay", status.gateway ? "connected" : "not connected"],
                ["Autonomy", status.autonomy ? "ready" : "disabled"],
              ].map(([label, value]) => (
                <li
                  key={label}
                  className="rounded-md border border-neutral-800/60 bg-neutral-900/30 px-3 py-2"
                >
                  <p className="text-[10px] uppercase tracking-wide text-neutral-500">
                    {label}
                  </p>
                  <p className="mt-0.5 text-xs text-neutral-200">{value}</p>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* --- What your agent can do (live registry) --- */}
        <section>
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            What your agent can do
          </h2>
          {capError ? (
            <ErrorState message={capError} onRetry={() => void load()} />
          ) : capabilities === null ? (
            <div className="h-20 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40" />
          ) : capabilities.length === 0 ? (
            <p className="rounded-lg border border-neutral-800/60 bg-neutral-900/30 px-4 py-3 text-xs text-neutral-500">
              No capability contracts declared. A 0.2 request naming a
              capability will be refused with UNSUPPORTED_CAPABILITY.
            </p>
          ) : (
            <ul className="space-y-2">
              {capabilities.map((cap) => (
                <li
                  key={cap.id}
                  className="rounded-lg border border-neutral-800/70 bg-neutral-900/30 px-4 py-3"
                >
                  <p className="text-sm text-neutral-100">
                    {cap.id}
                    <span className="ml-2 font-mono text-[11px] text-neutral-500">
                      @{cap.version}
                    </span>
                  </p>
                  {cap.description && (
                    <p className="mt-0.5 text-[11px] leading-relaxed text-neutral-400">
                      {cap.description}
                    </p>
                  )}
                  <p className="mt-1 text-[10px] uppercase tracking-wide text-neutral-500">
                    {cap.data_category}
                  </p>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-neutral-600">
            Live from the capability registry — what a peer must send to be
            accepted.
          </p>
        </section>

        {/* --- Account --- */}
        <section>
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Account
          </h2>
          <form
            onSubmit={onChangePassword}
            className="space-y-3 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-4"
          >
            <div>
              <label
                htmlFor="current-password"
                className="block text-xs text-neutral-400"
              >
                Current password
              </label>
              <input
                id="current-password"
                type="password"
                required
                autoComplete="current-password"
                value={currentPw}
                onChange={(e) => setCurrentPw(e.target.value)}
                disabled={pwBusy}
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500 disabled:opacity-50"
              />
            </div>
            <div>
              <label
                htmlFor="new-password"
                className="block text-xs text-neutral-400"
              >
                New password
              </label>
              <input
                id="new-password"
                type="password"
                required
                autoComplete="new-password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                disabled={pwBusy}
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500 disabled:opacity-50"
              />
              <p className="mt-1 text-xs text-neutral-500">
                At least 10 characters.
              </p>
            </div>
            <div>
              <label
                htmlFor="confirm-password"
                className="block text-xs text-neutral-400"
              >
                Confirm new password
              </label>
              <input
                id="confirm-password"
                type="password"
                required
                autoComplete="new-password"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                disabled={pwBusy}
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500 disabled:opacity-50"
              />
            </div>
            {pwError && (
              <p role="alert" className="text-xs text-red-400">
                {pwError}
              </p>
            )}
            {pwNotice && (
              <p role="status" className="text-xs text-emerald-400">
                {pwNotice}
              </p>
            )}
            <Button type="submit" size="sm" isLoading={pwBusy}>
              Change password
            </Button>
          </form>
        </section>

        {/* --- Detail screens --- */}
        <section>
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Details
          </h2>
          <ul className="grid gap-2 sm:grid-cols-2">
            {[
              {
                href: "/agent/memory",
                title: "Memory",
                body: "What it remembers about you, and how to forget it.",
              },
              {
                href: "/agent/permissions",
                title: "Permissions",
                body: "What it may do without asking, and who may see what.",
              },
              {
                href: "/agent/tools",
                title: "Tools",
                body: "What it can actually do, and what each tool did.",
              },
              {
                href: "/agent/activity",
                title: "Activity",
                body: "Everything it decided or sent, in order.",
              },
              {
                href: "/agent/autonomy",
                title: "Autonomy",
                body: "How much it may do on its own, and what is waiting on you.",
              },
              {
                href: "/agent/workflows",
                title: "Workflows",
                body: "Longer jobs it is carrying out step by step.",
              },
              {
                href: "/agent/tasks",
                title: "Tasks",
                body: "Work it has handed to, or taken from, a contact.",
              },
            ].map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className="block h-full rounded-lg border border-neutral-800/70 bg-neutral-900/30 px-4 py-3 transition-colors hover:bg-neutral-800/40"
                >
                  <p className="text-sm text-neutral-100">{item.title}</p>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-neutral-500">
                    {item.body}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
          {/* These links are the only route to the detail screens: the sidebar
              deliberately carries four destinations, so if a screen is not
              listed here it is unreachable. */}
          {/* Person detail (people/[id]) is NOT listed here: it is linked
              from both People lists via computed hrefs
              (`/people/${agent_id}`), which the static-link test skips by
              design since it cannot verify computed targets. Route registry
              entry so the reachability test sees its inbound link:
              href: "/people/[id]". */}
          <p className="mt-3 text-[11px] text-neutral-600">
            Every screen the sidebar does not show is reachable from here.
          </p>
        </section>
      </div>
    </div>
  );
}

/**
 * The shareable URL for this deployment's signed card: the well-known path
 * on the card endpoint's origin, which is where the discovery routes serve
 * it (`/.well-known/nexus-agent.json`). Null when the card has no endpoint.
 */
function cardUrlFor(card: AgentCard): string | null {
  if (!card.endpoint) return null;
  try {
    return `${new URL(card.endpoint).origin}/.well-known/nexus-agent.json`;
  } catch {
    return null;
  }
}

function Field({
  label,
  value,
  onCopy,
  copied,
  mono,
  hint,
}: {
  label: string;
  value: string;
  onCopy: () => void;
  copied: boolean;
  mono?: boolean;
  hint?: string;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <span className="text-[10px] uppercase tracking-wide text-neutral-500">
          {label}
        </span>
        <Button size="sm" variant="ghost" onClick={onCopy} aria-label={`Copy ${label}`}>
          {copied ? (
            <Check className="h-3.5 w-3.5 text-emerald-400" aria-hidden />
          ) : (
            <Copy className="h-3.5 w-3.5" aria-hidden />
          )}
        </Button>
      </div>
      <p
        className={`mt-0.5 break-all text-xs text-neutral-200 ${
          mono ? "font-mono" : ""
        }`}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-neutral-500">{hint}</p>}
    </div>
  );
}
