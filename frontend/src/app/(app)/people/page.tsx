"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { BadgeCheck, Search, ShieldAlert, UserPlus } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { ErrorState } from "@/components/ui/ErrorState";
import { ApiError } from "@/lib/api/client";
import { invalidateContactNames } from "@/lib/useContactNames";
import { formatDate } from "@/lib/utils";
import {
  DirectoryAgent,
  TrustedAgent,
  cardUrlFor,
  connectByCardUrl,
  listTrusted,
  removeTrust,
  revokeTrust,
  searchDirectory,
} from "@/lib/api/people";

export default function PeoplePage() {
  const [trusted, setTrusted] = useState<TrustedAgent[]>([]);
  const [loadingTrusted, setLoadingTrusted] = useState(true);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DirectoryAgent[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [cardUrl, setCardUrl] = useState("");
  const [connecting, setConnecting] = useState(false);
  /** The directory result with a connect in flight; its button shows busy. */
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** The trusted row with revoke/remove in flight; its buttons show busy. */
  const [busyAgentId, setBusyAgentId] = useState<string | null>(null);
  /** Pending destructive actions, confirmed through ConfirmModal. */
  const [revokeTarget, setRevokeTarget] = useState<TrustedAgent | null>(null);
  const [removeTarget, setRemoveTarget] = useState<TrustedAgent | null>(null);

  const loadTrusted = useCallback(async () => {
    setLoadingTrusted(true);
    try {
      setTrusted(await listTrusted());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load contacts.");
    } finally {
      setLoadingTrusted(false);
    }
  }, []);

  useEffect(() => {
    void loadTrusted();
  }, [loadTrusted]);

  async function onSearch(event: React.FormEvent) {
    event.preventDefault();
    if (searching) return;
    setError(null);
    setNotice(null);
    setSearching(true);
    try {
      setResults(await searchDirectory(query));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Directory search failed.");
      setResults(null);
    } finally {
      setSearching(false);
    }
  }

  async function onConnect(event: React.FormEvent) {
    event.preventDefault();
    if (connecting) return;
    setError(null);
    setNotice(null);
    setConnecting(true);
    try {
      const agent = await connectByCardUrl(cardUrl.trim());
      setNotice(`Connected to ${agent.display_name}.`);
      setCardUrl("");
      invalidateContactNames();
      await loadTrusted();
    } catch (err) {
      // The backend refuses to register an agent whose card does not verify;
      // surfacing its message verbatim is more useful than a generic failure.
      setError(err instanceof ApiError ? err.message : "Could not connect.");
    } finally {
      setConnecting(false);
    }
  }

  async function onConnectResult(agent: DirectoryAgent) {
    // Verified-only: the button is disabled otherwise, but guard anyway —
    // an unverified card must never reach the trust-creating endpoint.
    if (connectingId !== null || !agent.verified) return;
    const url = cardUrlFor(agent);
    if (!url) {
      setError("No endpoint to fetch that agent's card from.");
      return;
    }
    setError(null);
    setNotice(null);
    setConnectingId(agent.agent_id);
    try {
      const connected = await connectByCardUrl(url, agent.display_name);
      setNotice(`Connected to ${connected.display_name}.`);
      invalidateContactNames();
      await loadTrusted();
      setResults((prev) =>
        prev === null
          ? prev
          : prev.map((r) =>
              r.agent_id === agent.agent_id ? { ...r, is_trusted: true } : r
            )
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not connect.");
    } finally {
      setConnectingId(null);
    }
  }

  async function onRevokeConfirm() {
    if (!revokeTarget) return;
    if (busyAgentId !== null) return;
    const agentId = revokeTarget.agent_id;
    setRevokeTarget(null);
    setError(null);
    setBusyAgentId(agentId);
    try {
      await revokeTrust(agentId);
      invalidateContactNames();
      await loadTrusted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not revoke.");
    } finally {
      setBusyAgentId(null);
    }
  }

  async function onRemoveConfirm() {
    if (!removeTarget) return;
    if (busyAgentId !== null) return;
    const agentId = removeTarget.agent_id;
    setRemoveTarget(null);
    setError(null);
    setBusyAgentId(agentId);
    try {
      await removeTrust(agentId);
      invalidateContactNames();
      await loadTrusted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove.");
    } finally {
      setBusyAgentId(null);
    }
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <header className="border-b border-neutral-800/60 px-6 py-4">
        <h1 className="text-sm font-semibold">People</h1>
        <p className="text-[11px] text-neutral-500">
          Agents your agent is allowed to talk to. Each connection pins a
          cryptographic key.
        </p>
      </header>

      <div className="mx-auto w-full max-w-3xl space-y-8 px-6 py-6">
        {error && (
          <ErrorState
            message={error}
            onRetry={() => {
              setError(null);
              void loadTrusted();
            }}
          />
        )}
        {notice && (
          <div className="rounded-md border border-emerald-900/50 bg-emerald-950/30 px-3 py-2 text-xs text-emerald-300">
            {notice}
          </div>
        )}

        {/* --- Connected agents --- */}
        <section>
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Connected
          </h2>
          {loadingTrusted ? (
            <div className="h-16 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40" />
          ) : trusted.length === 0 ? (
            <div className="rounded-lg border border-neutral-800/60 bg-neutral-900/30 px-5 py-8 text-center">
              <UserPlus className="mx-auto h-5 w-5 text-neutral-600" aria-hidden />
              <p className="mt-3 text-sm text-neutral-300">
                No one connected yet.
              </p>
              <p className="mt-1 text-xs text-neutral-500">
                Connect by agent card URL below, or find someone by name.
              </p>
            </div>
          ) : (
            <ul className="space-y-2">
              {trusted.map((agent) => (
                <li
                  key={agent.agent_id}
                  className="flex items-center justify-between gap-4 rounded-lg border border-neutral-800/70 bg-neutral-900/40 px-4 py-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Link
                        href={`/people/${encodeURIComponent(agent.agent_id)}`}
                        className="truncate text-sm text-neutral-100 underline-offset-2 hover:underline"
                      >
                        {agent.display_name}
                      </Link>
                      {agent.status === "revoked" && (
                        <span className="rounded bg-red-950/60 px-1.5 py-0.5 text-[10px] uppercase text-red-400">
                          revoked
                        </span>
                      )}
                    </div>
                    {/* TrustedAgentOut returns only these fields — there is no
                        @handle, verification state, or capability list here, so
                        none is shown rather than invented. */}
                    <p className="mt-0.5 truncate font-mono text-[11px] text-neutral-500">
                      {agent.agent_id}
                    </p>
                    <p className="mt-0.5 truncate text-[11px] text-neutral-500">
                      {agent.endpoint}
                      {agent.created_at
                        ? ` · connected ${formatDate(agent.created_at)}`
                        : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    {agent.status !== "revoked" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setRevokeTarget(agent)}
                        isLoading={busyAgentId === agent.agent_id}
                        disabled={busyAgentId !== null && busyAgentId !== agent.agent_id}
                      >
                        Revoke
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setRemoveTarget(agent)}
                      disabled={busyAgentId !== null}
                    >
                      Remove
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* --- Connect by card URL --- */}
        <section>
          <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Connect by card URL
          </h2>
          <p className="mb-3 text-[11px] text-neutral-500">
            The card is fetched and its signature, key binding and validity are
            verified before anything is trusted.
          </p>
          <form onSubmit={onConnect} className="flex gap-2">
            <input
              type="url"
              required
              value={cardUrl}
              onChange={(e) => setCardUrl(e.target.value)}
              placeholder="https://friend.example.com/.well-known/nexus-agent.json"
              aria-label="Agent card URL"
              className="flex-1 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs outline-none focus:border-neutral-500"
            />
            <Button type="submit" size="sm" isLoading={connecting}>
              Connect
            </Button>
          </form>
        </section>

        {/* --- Directory search --- */}
        <section>
          <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            Find someone
          </h2>
          <p className="mb-3 text-[11px] text-neutral-500">
            Search the gateway directory. A directory entry is a hint: it is
            only trusted once its signed card verifies.
          </p>
          <form onSubmit={onSearch} className="flex gap-2">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="@handle, name, or nexus:ed25519:…"
              aria-label="Search the directory"
              className="flex-1 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs outline-none focus:border-neutral-500"
            />
            <Button type="submit" size="sm" variant="secondary" isLoading={searching}>
              <Search className="h-3.5 w-3.5" aria-hidden />
            </Button>
          </form>

          {results !== null && (
            <ul className="mt-3 space-y-2">
              {results.length === 0 && (
                <li className="text-xs text-neutral-500">No matches.</li>
              )}
              {results.map((agent) => (
                <li
                  key={agent.agent_id}
                  className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 px-4 py-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Link
                          href={`/people/${encodeURIComponent(agent.agent_id)}`}
                          className="truncate text-sm text-neutral-100 underline-offset-2 hover:underline"
                        >
                          {agent.display_name}
                        </Link>
                        {agent.handle && (
                          <span className="text-[11px] text-neutral-500">
                            {agent.handle}
                          </span>
                        )}
                        {/* A badge ONLY when the card signature verified. */}
                        {agent.verified ? (
                          <span
                            className="inline-flex items-center gap-1 rounded bg-emerald-950/50 px-1.5 py-0.5 text-[10px] text-emerald-400"
                            title="The agent card's Ed25519 signature verified"
                          >
                            <BadgeCheck className="h-3 w-3" aria-hidden />
                            verified
                          </span>
                        ) : (
                          <span
                            className="inline-flex items-center gap-1 rounded bg-amber-950/50 px-1.5 py-0.5 text-[10px] text-amber-400"
                            title={agent.verification_error ?? "Not verified"}
                          >
                            <ShieldAlert className="h-3 w-3" aria-hidden />
                            unverified
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 truncate font-mono text-[11px] text-neutral-500">
                        {agent.agent_id}
                      </p>
                      {!agent.verified && agent.verification_error && (
                        <p className="mt-1 text-[11px] text-amber-500/80">
                          {agent.verification_error}
                        </p>
                      )}
                      {agent.capabilities.length > 0 && (
                        <p className="mt-1 text-[11px] text-neutral-500">
                          {agent.capabilities.join(", ")}
                        </p>
                      )}
                    </div>
                    {agent.is_trusted ? (
                      <span className="shrink-0 text-[10px] uppercase text-neutral-500">
                        connected
                      </span>
                    ) : agent.verified ? (
                      <Button
                        size="sm"
                        onClick={() => void onConnectResult(agent)}
                        isLoading={connectingId === agent.agent_id}
                        disabled={connectingId !== null}
                        title="Fetch the signed card and pin its key"
                      >
                        Connect
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled
                        title={
                          agent.verification_error ??
                          "Not verified — connect is unavailable"
                        }
                      >
                        Connect
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {/* Revoke/remove confirmations via modal, no native dialogs. */}
      <ConfirmModal
        open={revokeTarget !== null}
        title="Revoke trust"
        body={`Revoke the pinned key for "${revokeTarget?.display_name ?? "this agent"}"? They will no longer be able to reach you until you reconnect.`}
        confirmLabel="Revoke trust"
        danger
        onConfirm={() => void onRevokeConfirm()}
        onCancel={() => setRevokeTarget(null)}
      />
      <ConfirmModal
        open={removeTarget !== null}
        title="Remove contact"
        body={`Remove "${removeTarget?.display_name ?? "this agent"}" from your contacts entirely?`}
        confirmLabel="Remove contact"
        danger
        onConfirm={() => void onRemoveConfirm()}
        onCancel={() => setRemoveTarget(null)}
      />
    </div>
  );
}
