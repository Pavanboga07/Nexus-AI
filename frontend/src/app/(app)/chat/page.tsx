"use client";

/**
 * Chat with your own agent, with approvals inline.
 *
 * The point of inlining approvals is that the conversation is where the user
 * already has context. Sending them to another page to answer "may I share your
 * availability with Rahul?" loses the thread they were just reading.
 *
 * Conversation history is preserved across sessions via the sessions API, so a
 * refresh does not lose the thread.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Send } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { ApiError, apiFetch } from "@/lib/api/client";
import { useContactNames } from "@/lib/useContactNames";
import { formatDate } from "@/lib/utils";
import {
  ApprovalItem,
  decideApproval,
  listApprovals,
  sourceLabel,
} from "@/lib/api/approvals";

type Message = { role: "user" | "assistant" | "system"; content: string };

type SessionResponse = {
  session_id: string;
  messages: Message[];
  created_at: string;
  updated_at: string;
};

const LAST_SESSION_KEY = "nexus.lastSessionId";

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [startingNew, setStartingNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<ApprovalItem[]>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // Approval evidence resolves agent IDs to display names, as the Inbox does.
  const { resolve: resolveName } = useContactNames();

  const refreshPending = useCallback(async () => {
    try {
      const { items } = await listApprovals();
      setPending(items);
    } catch {
      // The inline approvals panel is a convenience; a failure here must not
      // break the conversation.
    }
  }, []);

  // Resume the last session if it still exists, otherwise start a new one.
  // A stale id in localStorage is expected (the session may have been cleared),
  // so a failure falls through to creating a fresh session rather than erroring.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const remembered =
          typeof window !== "undefined"
            ? window.localStorage.getItem(LAST_SESSION_KEY)
            : null;
        if (remembered) {
          try {
            const existing = await apiFetch<SessionResponse>(
              `/sessions/${remembered}`
            );
            if (!cancelled) {
              setSessionId(existing.session_id);
              setMessages(existing.messages ?? []);
              await refreshPending();
              return;
            }
          } catch {
            // Fall through: start a new session.
          }
        }
        const created = await apiFetch<{ session_id: string }>("/sessions", {
          method: "POST",
        });
        if (cancelled) return;
        setSessionId(created.session_id);
        window.localStorage.setItem(LAST_SESSION_KEY, created.session_id);
        await refreshPending();
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.message
              : "Could not start a conversation."
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshPending]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, pending.length]);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || !sessionId || sending) return;

    setSending(true);
    setError(null);
    const optimistic: Message = { role: "user", content: text };
    setMessages((prev) => [...prev, optimistic]);
    setDraft("");
    try {
      const reply = await apiFetch<{ response: string }>("/chat", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: reply.response },
      ]);
      // The agent may have paused for approval while answering.
      await refreshPending();
    } catch (err) {
      // Roll the optimistic message back: a message the server never saw must
      // not sit in the thread as if it were sent.
      setMessages((prev) => prev.filter((m) => m !== optimistic));
      setError(
        err instanceof ApiError ? err.message : "That message could not be sent."
      );
    } finally {
      setSending(false);
    }
  }

  async function startNewSession() {
    if (startingNew) return;
    setStartingNew(true);
    setError(null);
    try {
      const created = await apiFetch<{ session_id: string }>("/sessions", {
        method: "POST",
      });
      setSessionId(created.session_id);
      setMessages([]);
      window.localStorage.setItem(LAST_SESSION_KEY, created.session_id);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not start a new chat."
      );
    } finally {
      setStartingNew(false);
    }
  }

  async function decide(item: ApprovalItem, decision: "approve" | "deny") {
    setError(null);
    try {
      await decideApproval(item.source, item.recordId, decision);
      setPending((prev) => prev.filter((i) => i.id !== item.id));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That decision was not recorded."
      );
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-neutral-800/60 px-6 py-4">
        <div>
          <h1 className="text-sm font-semibold">Chat</h1>
          <p className="text-[11px] text-neutral-500">
            It will ask you here before acting on your behalf.
          </p>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={startNewSession}
          isLoading={startingNew}
          disabled={startingNew}
        >
          New chat
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto max-w-3xl space-y-4">
          {messages.length === 0 && !sending && (
            <p className="pt-6 text-center text-xs text-neutral-500">
              Say something to begin.
            </p>
          )}

          {messages
            .filter((m) => m.role !== "system")
            .map((message, index) => (
              <div
                key={index}
                className={message.role === "user" ? "flex justify-end" : "flex"}
              >
                <div
                  className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-3.5 py-2 text-sm leading-relaxed ${
                    message.role === "user"
                      ? "bg-neutral-100 text-neutral-900"
                      : "border border-neutral-800/70 bg-neutral-900/50 text-neutral-200"
                  }`}
                >
                  {message.content}
                </div>
              </div>
            ))}

          {sending && (
            <div className="flex" aria-live="polite">
              <div className="rounded-lg border border-neutral-800/70 bg-neutral-900/50 px-3.5 py-2 text-sm text-neutral-500">
                Thinking…
              </div>
            </div>
          )}

          {/* Inline approvals: never send the user elsewhere mid-conversation. */}
          {pending.length > 0 && (
            <div className="rounded-lg border border-amber-900/40 bg-amber-950/20 p-3">
              <p className="text-[11px] font-medium uppercase tracking-wide text-amber-500">
                Waiting for you
              </p>
              <ul className="mt-2 space-y-2">
                {pending.slice(0, 3).map((item) => (
                  <li
                    key={item.id}
                    className="rounded border border-neutral-800/60 bg-neutral-900/40 px-3 py-2"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-xs text-neutral-200">
                          <span className="mr-1.5 text-[10px] uppercase text-neutral-500">
                            {sourceLabel(item.source)}
                          </span>
                          {item.summary || item.title}
                        </p>
                        {/* The evidence: the same fields the Inbox shows, so the
                            decision is made with context, not blindly. */}
                        <details className="mt-1.5">
                          <summary className="cursor-pointer text-[11px] text-neutral-500 hover:text-neutral-300">
                            Why this needs you
                          </summary>
                          <dl className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
                            {item.requestedBy && (
                              <div className="flex gap-1">
                                <dt>From</dt>
                                <dd className="truncate text-neutral-400">
                                  {resolveName(item.requestedBy)}
                                </dd>
                              </div>
                            )}
                            {item.category && (
                              <div className="flex gap-1">
                                <dt>Data</dt>
                                <dd className="text-neutral-400">
                                  {item.category}
                                </dd>
                              </div>
                            )}
                            {item.purpose && (
                              <div className="flex gap-1">
                                <dt>Purpose</dt>
                                <dd className="text-neutral-400">
                                  {item.purpose}
                                </dd>
                              </div>
                            )}
                            {item.requestedAction && (
                              <div className="flex gap-1">
                                <dt>Action</dt>
                                <dd className="text-neutral-400">
                                  {item.requestedAction}
                                </dd>
                              </div>
                            )}
                            {item.expiresAt && (
                              <div className="flex gap-1">
                                <dt>Expires</dt>
                                <dd className="text-neutral-400">
                                  {formatDate(item.expiresAt)}
                                </dd>
                              </div>
                            )}
                          </dl>
                        </details>
                      </div>
                      <div className="flex shrink-0 gap-1.5">
                        <Button size="sm" onClick={() => decide(item, "approve")}>
                          Approve
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => decide(item, "deny")}
                        >
                          No
                        </Button>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      <div className="border-t border-neutral-800/60 px-6 py-3">
        <div className="mx-auto max-w-3xl">
          {error && (
            <p role="alert" className="mb-2 text-[11px] text-red-400">
              {error}
            </p>
          )}
          <form onSubmit={send} className="flex gap-2">
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={sessionId ? "Ask your agent…" : "Starting…"}
              aria-label="Message"
              disabled={!sessionId || sending}
              className="flex-1 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-500 disabled:opacity-50"
            />
            <Button type="submit" isLoading={sending} disabled={!draft.trim()}>
              <Send className="h-3.5 w-3.5" aria-hidden />
            </Button>
          </form>
        </div>
      </div>
    </div>
  );
}
