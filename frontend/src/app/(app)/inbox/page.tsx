"use client";

/**
 * Inbox - the product's centre.
 *
 * One question: what needs me? Approvals arrive from four subsystems (tasks,
 * workflows, autonomy, orchestration) and are presented identically, because
 * the user is deciding about an ACTION, not about which subsystem asked.
 *
 * Each card shows the evidence needed to decide: who is asking, what they want
 * to do, and which data category and purpose are involved - which is exactly
 * what the policy engine evaluated. A decision UI that hides those fields asks
 * the user to approve something opaque.
 */

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Check, Clock, X } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { useContactNames } from "@/lib/useContactNames";
import { formatDate } from "@/lib/utils";
import {
  ApprovalItem,
  decideApproval,
  listApprovals,
  sourceLabel,
} from "@/lib/api/approvals";

type LoadState = "loading" | "ready" | "error";

/**
 * One approval card, shared by all three panes. The only differences are the
 * peer label ("From" for needs-you, "Waiting on" for the outbox, "With" for
 * done), the outcome badge (done only), and the action buttons (needs-you
 * only) — the evidence block is identical so a decision reads the same
 * everywhere.
 */
function InboxCard({
  item,
  peerLabel,
  resolveName,
  outcomeText,
  positiveOutcome,
  actions,
  highlight,
}: {
  item: ApprovalItem;
  peerLabel: string;
  resolveName: (id?: string | null) => string;
  outcomeText?: string | null;
  positiveOutcome?: boolean;
  actions?: React.ReactNode;
  /** Decision in flight: flashes the card until it moves to Done. */
  highlight?: "approve" | "deny";
}) {
  const title =
    item.source === "task" && item.requestedBy
      ? `${peerLabel === "From" ? "Task from" : peerLabel === "Waiting on" ? "Task to" : "Task with"} ${resolveName(item.requestedBy)}`
      : item.title;
  return (
    <li
      className={`rounded-lg border px-4 py-3 transition-colors ${
        highlight === "approve"
          ? "border-emerald-900/60 bg-emerald-950/20"
          : highlight === "deny"
            ? "border-neutral-800 bg-neutral-900/20"
            : "border-neutral-800/70 bg-neutral-900/40"
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-neutral-400">
              {sourceLabel(item.source)}
            </span>
            <h2 className="truncate text-sm font-medium text-neutral-100">
              {title}
            </h2>
            {outcomeText && (
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                  positiveOutcome
                    ? "bg-emerald-950/60 text-emerald-400"
                    : "bg-neutral-800 text-neutral-400"
                }`}
              >
                {outcomeText}
              </span>
            )}
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-neutral-400">
            {item.summary}
          </p>

          {/* The evidence: what the policy engine actually evaluated. */}
          <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
            {item.requestedBy && (
              <div className="flex gap-1">
                <dt>{peerLabel}</dt>
                <dd className="truncate text-neutral-400">
                  {resolveName(item.requestedBy)}
                </dd>
              </div>
            )}
            {item.category && (
              <div className="flex gap-1">
                <dt>Data</dt>
                <dd className="text-neutral-400">{item.category}</dd>
              </div>
            )}
            {item.purpose && (
              <div className="flex gap-1">
                <dt>Purpose</dt>
                <dd className="text-neutral-400">{item.purpose}</dd>
              </div>
            )}
            {item.requestedAction && (
              <div className="flex gap-1">
                <dt>Action</dt>
                <dd className="text-neutral-400">{item.requestedAction}</dd>
              </div>
            )}
            {item.expiresAt && (
              <div className="flex items-center gap-1">
                <Clock className="h-3 w-3" aria-hidden />
                <dd>expires {formatDate(item.expiresAt)}</dd>
              </div>
            )}
          </dl>
        </div>

        {actions && <div className="flex shrink-0 gap-1.5">{actions}</div>}
      </div>
    </li>
  );
}

/** Outcomes that read as a success in the Done pane. */
function isPositiveOutcome(outcome: string | null | undefined): boolean {
  return outcome === "approved" || outcome === "completed";
}

function outcomeLabel(outcome: string | null | undefined): string | null {
  if (!outcome) return null;
  if (outcome === "approved") return "Approved";
  if (outcome === "declined") return "Declined";
  return outcome;
}

export default function InboxPage() {
  const [state, setState] = useState<LoadState>("loading");
  const [items, setItems] = useState<ApprovalItem[]>([]);
  const [waiting, setWaiting] = useState<ApprovalItem[]>([]);
  const [doneBackend, setDoneBackend] = useState<ApprovalItem[]>([]);
  /** Items decided in this session, moved here instead of vanishing. */
  const [doneLocal, setDoneLocal] = useState<ApprovalItem[]>([]);
  const [unavailable, setUnavailable] = useState<
    Array<{ source: string; reason: string }>
  >([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [decided, setDecided] = useState<Record<string, "approve" | "deny">>({});
  // Contact names resolve agent IDs to display names; unknown IDs fall back
  // to a short slice (never a raw 24-char slice).
  const { resolve: resolveName } = useContactNames();

  const load = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      const result = await listApprovals();
      setItems(result.items);
      setWaiting(result.waitingOutbox ?? []);
      setDoneBackend(result.recentlyDecided ?? []);
      setUnavailable(result.unavailable);
      setState("ready");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load your inbox."
      );
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(item: ApprovalItem, decision: "approve" | "deny") {
    if (busyId !== null) return;
    setBusyId(item.id);
    setError(null);
    try {
      await decideApproval(item.source, item.recordId, decision);
      setDecided((prev) => ({ ...prev, [item.id]: decision }));
      // Move it to Done after a beat so the user sees what happened —
      // decided cards keep their outcome visible instead of vanishing.
      setTimeout(() => {
        setItems((prev) => prev.filter((i) => i.id !== item.id));
        setDecided((prev) => {
          const next = { ...prev };
          delete next[item.id];
          return next;
        });
        setDoneLocal((prev) =>
          prev.some((i) => i.id === item.id)
            ? prev
            : [
                {
                  ...item,
                  outcome: decision === "approve" ? "approved" : "declined",
                },
                ...prev,
              ]
        );
      }, 600);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${item.title}: ${err.message}`
          : "That decision could not be recorded."
      );
    } finally {
      setBusyId(null);
    }
  }

  const done = [...doneLocal, ...doneBackend];

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <header className="border-b border-neutral-800/60 px-6 py-4">
        <h1 className="text-sm font-semibold">Inbox</h1>
        <p className="text-[11px] text-neutral-500">
          {state === "ready"
            ? items.length === 0
              ? "Nothing needs you right now."
              : `${items.length} item${items.length === 1 ? "" : "s"} waiting for a decision.`
            : "Checking what needs you…"}
        </p>
      </header>

      <div className="mx-auto w-full max-w-3xl px-6 py-6">
        {error && (
          <div
            role="alert"
            className="mb-4 flex items-start gap-2 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-xs text-red-300"
          >
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        )}

        {unavailable.length > 0 && (
          <p className="mb-4 text-[11px] text-neutral-600">
            Not available on this deployment:{" "}
            {unavailable.map((u) => `${u.source} (${u.reason})`).join(", ")}.
          </p>
        )}

        {state === "loading" && (
          <div className="space-y-2" aria-busy="true">
            {[0, 1].map((i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-lg border border-neutral-800/60 bg-neutral-900/40"
              />
            ))}
          </div>
        )}

        {state === "ready" && items.length === 0 && waiting.length === 0 && done.length === 0 && (
          <div className="rounded-lg border border-neutral-800/60 bg-neutral-900/30 px-6 py-10 text-center">
            <Check className="mx-auto h-5 w-5 text-emerald-500" aria-hidden />
            <p className="mt-3 text-sm text-neutral-300">You are all caught up.</p>
            <p className="mt-1 text-xs text-neutral-500">
              Your agent will ask here before it does anything that affects other
              people or shares your information.
            </p>
          </div>
        )}

        {state === "ready" && (items.length > 0 || waiting.length > 0 || done.length > 0) && (
          <div className="space-y-8">
            <section aria-label="Needs you">
              <h2 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
                Needs you{items.length > 0 ? ` (${items.length})` : ""}
              </h2>
              {items.length === 0 ? (
                <p className="text-xs text-neutral-600">Nothing waiting for a decision.</p>
              ) : (
                <ul className="space-y-2">
                  {items.map((item) => {
                    const meta = decided[item.id];
                    return (
                      <InboxCard
                        key={item.id}
                        item={item}
                        peerLabel="From"
                        resolveName={resolveName}
                        highlight={meta}
                        actions={
                          <>
                            <Button
                              size="sm"
                              onClick={() => decide(item, "approve")}
                              isLoading={busyId === item.id}
                              disabled={Boolean(meta)}
                            >
                              Approve
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => decide(item, "deny")}
                              disabled={Boolean(meta) || busyId === item.id}
                            >
                              <X className="h-3.5 w-3.5" aria-hidden />
                            </Button>
                          </>
                        }
                      />
                    );
                  })}
                </ul>
              )}
            </section>

            <section aria-label="Waiting on others">
              <h2 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
                Waiting on others{waiting.length > 0 ? ` (${waiting.length})` : ""}
              </h2>
              {waiting.length === 0 ? (
                <p className="text-xs text-neutral-600">
                  Nothing you sent is waiting on someone else.
                </p>
              ) : (
                <ul className="space-y-2">
                  {waiting.map((item) => (
                    <InboxCard
                      key={item.id}
                      item={item}
                      peerLabel="Waiting on"
                      resolveName={resolveName}
                    />
                  ))}
                </ul>
              )}
            </section>

            <section aria-label="Done today">
              <h2 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-neutral-500">
                Done today{done.length > 0 ? ` (${done.length})` : ""}
              </h2>
              {done.length === 0 ? (
                <p className="text-xs text-neutral-600">
                  Decisions you make will land here with their outcome.
                </p>
              ) : (
                <ul className="space-y-2">
                  {done.map((item) => (
                    <InboxCard
                      key={item.id}
                      item={item}
                      peerLabel="With"
                      resolveName={resolveName}
                      outcomeText={outcomeLabel(item.outcome)}
                      positiveOutcome={isPositiveOutcome(item.outcome)}
                    />
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
