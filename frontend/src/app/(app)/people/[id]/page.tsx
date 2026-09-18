"use client";

/**
 * Person detail — everything known about one remote agent in one place.
 *
 * The trusted list carries only `agent_id/display_name/endpoint/status`, so
 * the @handle, verification state, capabilities and key material here come
 * from the directory/discovery payload (which exposes its signed card only
 * when the card verified). The fingerprint is recomputed locally from the
 * verified public key in exactly the backend's format
 * (`fingerprint_from_public_key` in app/identity/crypto.py), so comparing it
 * over another channel confirms the same key both sides pinned.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { BadgeCheck, Copy, Check, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/ErrorState";
import { ApiError } from "@/lib/api/client";
import { useAsync } from "@/lib/useAsync";
import { shortContactId } from "@/lib/useContactNames";
import { formatDate } from "@/lib/utils";
import { DirectoryAgent, TrustedAgent, listTrusted, searchDirectory } from "@/lib/api/people";
import { listTasks } from "@/lib/api/tasks";
import { A2ATask } from "@/types/api";

type PersonData = {
  agentId: string;
  trusted: TrustedAgent | null;
  directory: DirectoryAgent | null;
  /** True when the directory could not be read at all (e.g. no gateway). */
  directoryFailed: boolean;
  /** True when the directory read succeeded but had no exact agent_id match. */
  directoryMiss: boolean;
  lastTask: A2ATask | null;
  /** True when the task list could not be read; lastTask is unknown, not empty. */
  tasksFailed: boolean;
};

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64.trim());
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * The backend's human-readable fingerprint (SHA-256 of the raw key, upper
 * hex, 8 groups of 4), recomputed from the verified public key. Returns null
 * when there is no key or it cannot be decoded — never a fabricated value.
 */
async function fingerprintForPublicKey(
  publicKeyB64: string | null | undefined
): Promise<string | null> {
  if (!publicKeyB64) return null;
  try {
    // Fresh allocation with no byte offset, so .buffer is exactly the key.
    const digest = await crypto.subtle.digest(
      "SHA-256",
      b64ToBytes(publicKeyB64).buffer as ArrayBuffer
    );
    const hex = Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
      .toUpperCase();
    const groups: string[] = [];
    for (let i = 0; i < 32; i += 4) groups.push(hex.slice(i, i + 4));
    return groups.join("-");
  } catch {
    return null;
  }
}

type CardCapability = {
  name?: string;
  description?: string;
  data_category?: string;
};

/** Capabilities from the verified card (objects) or the directory row (names). */
function cardCapabilities(directory: DirectoryAgent | null): CardCapability[] {
  const raw = directory?.card?.["capabilities"];
  if (Array.isArray(raw)) {
    return raw.flatMap((c) =>
      typeof c === "object" && c !== null
        ? [{ ...(c as Record<string, string>) }]
        : typeof c === "string"
          ? [{ name: c }]
          : []
    );
  }
  return (directory?.capabilities ?? []).map((name) => ({ name }));
}

function cardStringArray(
  directory: DirectoryAgent | null,
  field: string
): string[] {
  const raw = directory?.card?.[field];
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === "string");
}

async function loadPerson(agentId: string): Promise<PersonData> {
  const [trustedRes, dirRes, taskRes] = await Promise.allSettled([
    listTrusted(),
    searchDirectory(agentId),
    listTasks(),
  ]);
  if (trustedRes.status === "rejected") {
    throw new Error(
      trustedRes.reason instanceof ApiError
        ? trustedRes.reason.message
        : "Could not load contacts."
    );
  }
  const trusted =
    (trustedRes.value ?? []).find((a) => a.agent_id === agentId) ?? null;
  // Exact agent_id match ONLY. searchDirectory is a keyword search, so its
  // result list may contain other agents — falling back to value[0] here
  // would render another agent's key material and capabilities as this
  // person's (key-misattribution). A miss is null, never a neighbour's row.
  const directory =
    dirRes.status === "fulfilled"
      ? ((dirRes.value ?? []).find((a) => a.agent_id === agentId) ?? null)
      : null;
  const directoryMiss = dirRes.status === "fulfilled" && directory === null;
  let lastTask: A2ATask | null = null;
  if (taskRes.status === "fulfilled") {
    const peer = (taskRes.value.tasks ?? []).filter(
      (t) => t.sender_agent_id === agentId || t.recipient_agent_id === agentId
    );
    peer.sort((a, b) =>
      (b.updated_at ?? b.created_at ?? "").localeCompare(
        a.updated_at ?? a.created_at ?? ""
      )
    );
    lastTask = peer[0] ?? null;
  }
  const tasksFailed = taskRes.status === "rejected";
  if (!trusted && !directory) {
    throw new Error(
      dirRes.status === "rejected"
        ? "Could not load this person. The directory may be unavailable and they are not a contact."
        : "This person was not found in the directory and is not one of your contacts."
    );
  }
  return {
    agentId,
    trusted,
    directory,
    directoryFailed: dirRes.status === "rejected",
    directoryMiss,
    lastTask,
    tasksFailed,
  };
}

export default function PersonPage({ params }: { params: { id: string } }) {
  const agentId = decodeURIComponent(params.id);
  const { data, error, loading, reload } = useAsync(() => loadPerson(agentId));
  const [fingerprint, setFingerprint] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const publicKey = data?.directory?.public_key ?? null;
  useEffect(() => {
    let live = true;
    setFingerprint(null);
    if (!publicKey) return;
    void fingerprintForPublicKey(publicKey).then((fp) => {
      if (live) setFingerprint(fp);
    });
    return () => {
      live = false;
    };
  }, [publicKey]);

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      /* Clipboard denial leaves the value visible and selectable. */
    }
  }

  const displayName =
    data?.directory?.display_name ?? data?.trusted?.display_name ?? null;
  const handle = data?.directory?.handle ?? null;
  const verified = data?.directory?.verified ?? false;

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <header className="border-b border-neutral-800/60 px-6 py-4">
        <Link
          href="/people"
          className="text-[11px] text-neutral-500 underline-offset-2 hover:underline"
        >
          ← People
        </Link>
        <h1 className="mt-1 truncate text-sm font-semibold">
          {loading ? "Loading…" : (displayName ?? shortContactId(agentId))}
          {handle && (
            <span className="ml-2 font-normal text-neutral-500">{handle}</span>
          )}
        </h1>
      </header>

      <div className="mx-auto w-full max-w-3xl space-y-8 px-6 py-6">
        {error && <ErrorState message={error} onRetry={reload} />}
        {loading && (
          <div className="h-28 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40" />
        )}

        {data && (
          <>
            {/* --- Key & trust --- */}
            <section>
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                Key &amp; trust
              </h2>
              <div className="space-y-3 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-4">
                <div className="flex items-center gap-2">
                  {verified ? (
                    <span className="inline-flex items-center gap-1 rounded bg-emerald-950/50 px-1.5 py-0.5 text-[10px] text-emerald-400">
                      <BadgeCheck className="h-3 w-3" aria-hidden />
                      verified
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded bg-amber-950/50 px-1.5 py-0.5 text-[10px] text-amber-400">
                      <ShieldAlert className="h-3 w-3" aria-hidden />
                      unverified
                    </span>
                  )}
                  <span className="text-xs text-neutral-300">
                    {data.trusted
                      ? data.trusted.status === "revoked"
                        ? "Key pinned, but trust revoked"
                        : `Connected — key pinned${data.trusted.created_at ? ` ${formatDate(data.trusted.created_at)}` : ""}`
                      : "Not connected — no key pinned"}
                  </span>
                </div>
                {!verified && data.directory?.verification_error && (
                  <p className="text-[11px] text-amber-500/80">
                    {data.directory.verification_error}
                  </p>
                )}
                {data.directoryFailed && (
                  <p className="text-[11px] text-neutral-500">
                    Directory unavailable — showing contact record only.
                  </p>
                )}
                {data.directoryMiss && (
                  <p className="text-[11px] text-neutral-500">
                    Not found in the directory — showing contact record only.{" "}
                    <Link
                      href="/people"
                      className="underline underline-offset-2 hover:text-neutral-300"
                    >
                      Back to People
                    </Link>
                  </p>
                )}
                <CopyRow
                  label="Fingerprint"
                  value={fingerprint}
                  placeholder={
                    publicKey
                      ? "Computing…"
                      : "Unavailable — no verified key published"
                  }
                  onCopy={() =>
                    fingerprint && void copy("fingerprint", fingerprint)
                  }
                  copied={copied === "fingerprint"}
                  hint="Compare over another channel to confirm this is really them."
                />
                <CopyRow
                  label="Agent ID"
                  value={data.agentId}
                  onCopy={() => void copy("agent_id", data.agentId)}
                  copied={copied === "agent_id"}
                  mono
                />
                {publicKey && (
                  <CopyRow
                    label="Public key"
                    value={publicKey}
                    onCopy={() => void copy("public_key", publicKey)}
                    copied={copied === "public_key"}
                    mono
                  />
                )}
                {(data.directory?.endpoint ?? data.trusted?.endpoint) && (
                  <p className="truncate text-[11px] text-neutral-500">
                    {data.directory?.endpoint ?? data.trusted?.endpoint}
                  </p>
                )}
              </div>
            </section>
            {/* --- Capabilities (exactly what the verified card advertises) --- */}
            <CapabilitiesSection directory={data.directory} />

            {/* --- Last exchange --- */}
            <section>
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                Last exchange
              </h2>
              {data.tasksFailed ? (
                <p className="rounded-lg border border-amber-900/50 bg-amber-950/30 px-4 py-3 text-xs text-amber-300">
                  Could not load tasks — this list may be incomplete.
                </p>
              ) : !data.lastTask ? (
                <p className="rounded-lg border border-neutral-800/60 bg-neutral-900/30 px-4 py-3 text-xs text-neutral-500">
                  No tasks with this person yet.
                </p>
              ) : (
                <div className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 px-4 py-3">
                  <p className="text-sm text-neutral-100">
                    {data.lastTask.task_type ?? "Task"}
                    <span className="ml-2 rounded bg-neutral-800 px-1.5 py-0.5 font-mono text-[10px] text-neutral-400">
                      {data.lastTask.status}
                    </span>
                  </p>
                  {data.lastTask.purpose && (
                    <p className="mt-0.5 text-[11px] text-neutral-500">
                      {data.lastTask.purpose}
                    </p>
                  )}
                  {(data.lastTask.updated_at ?? data.lastTask.created_at) && (
                    <p className="mt-0.5 text-[11px] text-neutral-500">
                      {formatDate(
                        (data.lastTask.updated_at ??
                          data.lastTask.created_at) as string
                      )}
                    </p>
                  )}
                  <Link
                    href="/agent/tasks"
                    className="mt-1 inline-block text-[11px] text-neutral-400 underline-offset-2 hover:underline"
                  >
                    View in Tasks
                  </Link>
                </div>
              )}
            </section>

            {/* --- Per-person policy --- */}
            <section>
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                Policy
              </h2>
              <Link
                href="/agent/permissions#per-person"
                className="block rounded-lg border border-neutral-800/70 bg-neutral-900/30 px-4 py-3 transition-colors hover:bg-neutral-800/40"
              >
                <p className="text-sm text-neutral-100">
                  What this person may see
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-neutral-500">
                  Per-person policy rules live on the Permissions page.
                </p>
              </Link>
            </section>
          </>
        )}
      </div>
    </div>
  );
}

function CapabilitiesSection({
  directory,
}: {
  directory: DirectoryAgent | null;
}) {
  const caps = cardCapabilities(directory);
  const purposes = cardStringArray(directory, "supported_purposes");
  const issued = directory?.card?.["issued_at"];
  const expires = directory?.card?.["expires_at"];
  if (!directory) return null;
  return (
    <section>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-neutral-500">
        Capabilities
      </h2>
      {caps.length === 0 ? (
        <p className="rounded-lg border border-neutral-800/60 bg-neutral-900/30 px-4 py-3 text-xs text-neutral-500">
          {directory.verified
            ? "This card advertises no capabilities."
            : "Capabilities are shown only for verified cards."}
        </p>
      ) : (
        <ul className="space-y-2">
          {caps.map((cap) => (
            <li
              key={cap.name ?? JSON.stringify(cap)}
              className="rounded-lg border border-neutral-800/70 bg-neutral-900/30 px-4 py-3"
            >
              <p className="text-sm text-neutral-100">
                {cap.name ?? "Unnamed capability"}
              </p>
              {cap.description && (
                <p className="mt-0.5 text-[11px] leading-relaxed text-neutral-400">
                  {cap.description}
                </p>
              )}
              {cap.data_category && (
                <p className="mt-1 text-[10px] uppercase tracking-wide text-neutral-500">
                  {cap.data_category}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {purposes.length > 0 && (
        <p className="mt-2 text-[11px] text-neutral-500">
          Purposes: {purposes.join(", ")}
        </p>
      )}
      {(typeof issued === "string" || typeof expires === "string") && (
        <p className="mt-1 text-[11px] text-neutral-600">
          Card valid
          {typeof issued === "string" ? ` from ${formatDate(issued)}` : ""}
          {typeof expires === "string" ? ` until ${formatDate(expires)}` : ""}.
        </p>
      )}
    </section>
  );
}

function CopyRow({
  label,
  value,
  placeholder,
  onCopy,
  copied,
  mono,
  hint,
}: {
  label: string;
  value: string | null;
  placeholder?: string;
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
        {value && (
          <Button
            size="sm"
            variant="ghost"
            onClick={onCopy}
            aria-label={`Copy ${label}`}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-400" aria-hidden />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden />
            )}
          </Button>
        )}
      </div>
      <p
        className={`mt-0.5 break-all text-xs text-neutral-200 ${
          mono ? "font-mono" : ""
        }`}
      >
        {value ?? placeholder ?? "—"}
      </p>
      {hint && value && (
        <p className="mt-0.5 text-[11px] text-neutral-500">{hint}</p>
      )}
    </div>
  );
}
