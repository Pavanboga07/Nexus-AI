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
import { ApiError, apiFetch } from "@/lib/api/client";
import { getCapabilities, CapabilityInfo } from "@/lib/api/identity";
import { getStatus, StatusResponse } from "@/lib/api/status";

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

  const load = useCallback(async () => {
    setError(null);
    try {
      const [id, st, capRes] = await Promise.all([
        apiFetch<Identity>("/identity"),
        getStatus().catch(() => null),
        getCapabilities().catch(() => null),
      ]);
      setIdentity(id);
      setStatus(st);
      setCapabilities(capRes ? capRes.capabilities : []);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not read your agent's identity."
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

        {/* --- Subsystem status --- */}
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
          {capabilities === null ? (
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
          <p className="mt-3 text-[11px] text-neutral-600">
            Every screen the sidebar does not show is reachable from here.
          </p>
        </section>
      </div>
    </div>
  );
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
