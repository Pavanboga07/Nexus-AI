"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, BadgeCheck, Search, ShieldAlert, UserPlus } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import {
  DirectoryAgent,
  TrustedAgent,
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
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
    setError(null);
    setNotice(null);
    setConnecting(true);
    try {
      const agent = await connectByCardUrl(cardUrl.trim());
      setNotice(`Connected to ${agent.display_name}.`);
      setCardUrl("");
      await loadTrusted();
    } catch (err) {
      // The backend refuses to register an agent whose card does not verify;
      // surfacing its message verbatim is more useful than a generic failure.
      setError(err instanceof ApiError ? err.message : "Could not connect.");
    } finally {
      setConnecting(false);
    }
  }

  async function onRevoke(agentId: string) {
    setError(null);
    try {
      await revokeTrust(agentId);
      await loadTrusted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not revoke.");
    }
  }

  async function onRemove(agentId: string) {
    setError(null);
    try {
      await removeTrust(agentId);
      await loadTrusted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove.");
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
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-xs text-red-300"
          >
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
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
                      <span className="truncate text-sm text-neutral-100">
                        {agent.display_name}
                      </span>
                      {agent.status === "revoked" && (
                        <span className="rounded bg-red-950/60 px-1.5 py-0.5 text-[10px] uppercase text-red-400">
                          revoked
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-neutral-500">
                      {agent.agent_id}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    {agent.status !== "revoked" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => onRevoke(agent.agent_id)}
                      >
                        Revoke
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onRemove(agent.agent_id)}
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
                        <span className="truncate text-sm text-neutral-100">
                          {agent.display_name}
                        </span>
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
                    {agent.is_trusted && (
                      <span className="shrink-0 text-[10px] uppercase text-neutral-500">
                        connected
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
