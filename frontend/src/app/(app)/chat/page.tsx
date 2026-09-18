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
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Send } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ApiError, apiFetch } from "@/lib/api/client";
import { useContactNames } from "@/lib/useContactNames";
import { useCapabilities } from "@/lib/useCapabilities";
import { listTrustedAgents, TrustedAgent } from "@/lib/api/a2a";
import {
  CAPABILITY_TASK_TYPES,
  delegateTask,
  payloadForTaskType,
} from "@/lib/api/tasks";
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
  /** The inline approval with a decision in flight; its buttons show busy. */
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // Approval evidence resolves agent IDs to display names, as the Inbox does.
  const { resolve: resolveName } = useContactNames();

  // Ask-a-person: explicit contact → capability → purpose → summary picker
  // that creates a task through the delegate endpoint. Nothing is inferred
  // from free text; every dimension is a deliberate selection.
  const [askOpen, setAskOpen] = useState(false);
  const [askContact, setAskContact] = useState("");
  const [askCapability, setAskCapability] = useState("");
  const [askPurpose, setAskPurpose] = useState("");
  const [askSummary, setAskSummary] = useState("");
  const [askSending, setAskSending] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const [askSentTo, setAskSentTo] = useState<string | null>(null);
  const [contacts, setContacts] = useState<TrustedAgent[]>([]);
  const [contactsError, setContactsError] = useState<string | null>(null);
  const {
    capabilities,
    loading: capsLoading,
    error: capsError,
    reload: reloadCaps,
  } = useCapabilities();
  const askTaskType = CAPABILITY_TASK_TYPES[askCapability] ?? "";

  useEffect(() => {
    if (!askOpen) return;
    let live = true;
    setContactsError(null);
    void listTrustedAgents()
      .then((res) => {
        if (live) setContacts(res.agents ?? []);
      })
      .catch((err: unknown) => {
        if (live)
          setContactsError(
            err instanceof ApiError ? err.message : "Could not load contacts."
          );
      });
    return () => {
      live = false;
    };
  }, [askOpen]);

  function closeAsk() {
    setAskOpen(false);
    setAskContact("");
    setAskCapability("");
    setAskPurpose("");
    setAskSummary("");
    setAskError(null);
    setAskSentTo(null);
  }

  async function sendAsk(event: React.FormEvent) {
    event.preventDefault();
    if (askSending) return;
    if (!askContact) {
      setAskError("Please choose who to ask.");
      return;
    }
    if (!askTaskType) {
      setAskError(
        askCapability
          ? "This capability cannot be sent as a task yet."
          : "Please choose what to ask for."
      );
      return;
    }
    const summary = askSummary.trim();
    if (!summary) {
      setAskError("Please say what you are asking for.");
      return;
    }
    setAskSending(true);
    setAskError(null);
    try {
      await delegateTask({
        recipient_agent_id: askContact,
        task_type: askTaskType,
        purpose: askPurpose.trim() || `Chat ${askTaskType}`,
        payload: payloadForTaskType(askTaskType, summary),
      });
      setAskSentTo(resolveName(askContact));
    } catch (err) {
      setAskError(
        err instanceof ApiError ? err.message : "That request could not be sent."
      );
    } finally {
      setAskSending(false);
    }
  }

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
    if (decidingId !== null) return;
    setDecidingId(item.id);
    setError(null);
    try {
      await decideApproval(item.source, item.recordId, decision);
      setPending((prev) => prev.filter((i) => i.id !== item.id));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That decision was not recorded."
      );
    } finally {
      setDecidingId(null);
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
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setAskError(null);
              setAskSentTo(null);
              setAskOpen(true);
            }}
          >
            Ask a person
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={startNewSession}
            isLoading={startingNew}
            disabled={startingNew}
          >
            New chat
          </Button>
        </div>
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
                  className={`max-w-[85%] rounded-lg px-3.5 py-2 text-sm leading-relaxed ${
                    message.role === "user"
                      ? "whitespace-pre-wrap bg-neutral-100 text-neutral-900"
                      : "break-words border border-neutral-800/70 bg-neutral-900/50 text-neutral-200"
                  }`}
                >
                  {message.role === "user" ? (
                    message.content
                  ) : (
                    // No rehype-raw: raw HTML in model output stays inert text.
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ href, children }) => {
                          // Model text is untrusted: javascript:/data: links
                          // render as inert text, never as clickable hrefs.
                          if (!href || !/^(https?:|mailto:)/i.test(href)) {
                            return <span>{children}</span>;
                          }
                          return (
                            <a
                              href={href}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="underline underline-offset-2 hover:text-neutral-100"
                            >
                              {children}
                            </a>
                          );
                        },
                        // Tailwind preflight strips list markers; re-add them.
                        ul: ({ children }) => (
                          <ul className="my-1.5 list-disc space-y-1 pl-5">
                            {children}
                          </ul>
                        ),
                        ol: ({ children }) => (
                          <ol className="my-1.5 list-decimal space-y-1 pl-5">
                            {children}
                          </ol>
                        ),
                        p: ({ children }) => (
                          <p className="my-1.5 first:mt-0 last:mb-0">
                            {children}
                          </p>
                        ),
                        pre: ({ children }) => (
                          <pre className="my-1.5 overflow-x-auto rounded bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
                            {children}
                          </pre>
                        ),
                        code: ({ children }) => (
                          <code className="rounded bg-neutral-950 px-1 py-0.5 font-mono text-xs text-neutral-300 [pre_&]:bg-transparent [pre_&]:p-0">
                            {children}
                          </code>
                        ),
                        table: ({ children }) => (
                          <div className="my-1.5 overflow-x-auto">
                            <table className="w-full border-collapse text-xs">
                              {children}
                            </table>
                          </div>
                        ),
                        th: ({ children }) => (
                          <th className="border border-neutral-700 bg-neutral-950 px-2 py-1 text-left font-semibold">
                            {children}
                          </th>
                        ),
                        td: ({ children }) => (
                          <td className="border border-neutral-800/70 px-2 py-1">
                            {children}
                          </td>
                        ),
                        blockquote: ({ children }) => (
                          <blockquote className="my-1.5 border-l-2 border-neutral-700 pl-3 text-neutral-400">
                            {children}
                          </blockquote>
                        ),
                        h1: ({ children }) => (
                          <h1 className="mb-1 mt-2 text-base font-semibold first:mt-0">
                            {children}
                          </h1>
                        ),
                        h2: ({ children }) => (
                          <h2 className="mb-1 mt-2 text-sm font-semibold first:mt-0">
                            {children}
                          </h2>
                        ),
                        h3: ({ children }) => (
                          <h3 className="mb-1 mt-2 text-[13px] font-semibold first:mt-0">
                            {children}
                          </h3>
                        ),
                        h4: ({ children }) => (
                          <h4 className="mb-1 mt-2 text-xs font-semibold first:mt-0">
                            {children}
                          </h4>
                        ),
                        h5: ({ children }) => (
                          <h5 className="mb-1 mt-2 text-xs font-semibold first:mt-0">
                            {children}
                          </h5>
                        ),
                        h6: ({ children }) => (
                          <h6 className="mb-1 mt-2 text-xs font-semibold first:mt-0">
                            {children}
                          </h6>
                        ),
                        hr: () => <hr className="my-2 border-neutral-800/70" />,
                        img: ({ src, alt }) => {
                          // Model text is untrusted: only http(s) images render,
                          // constrained to the bubble; anything else shows alt
                          // text (or nothing) rather than a broken/chrome URL.
                          if (!src || !/^https?:/i.test(src)) {
                            return alt ? <span>{alt}</span> : null;
                          }
                          return (
                            <img
                              src={src}
                              alt={alt ?? ""}
                              className="my-1.5 max-w-full rounded"
                            />
                          );
                        },
                      }}
                    >
                      {message.content}
                    </ReactMarkdown>
                  )}
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
                        <Button
                          size="sm"
                          onClick={() => decide(item, "approve")}
                          isLoading={decidingId === item.id}
                          disabled={decidingId !== null}
                        >
                          Approve
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => decide(item, "deny")}
                          isLoading={decidingId === item.id}
                          disabled={decidingId !== null}
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

      {/* Ask a person: the same delegate flow as Tasks, reached mid-conversation. */}
      <Modal
        isOpen={askOpen}
        onClose={closeAsk}
        title="Ask a person"
        subtitle="Send a task to one of your trusted contacts"
      >
        {askSentTo ? (
          <div className="space-y-4">
            <p className="text-sm text-neutral-200">
              Sent to {askSentTo}. It will appear under{" "}
              <Link href="/inbox" className="underline underline-offset-2 hover:text-neutral-100">
                Waiting on others
              </Link>{" "}
              in your inbox.
            </p>
            <div className="flex justify-end">
              <Button size="sm" onClick={closeAsk}>
                Done
              </Button>
            </div>
          </div>
        ) : (
          <form onSubmit={sendAsk} className="space-y-4">
            {askError && (
              <p role="alert" className="text-xs text-red-400">
                {askError}
              </p>
            )}
            <div>
              <label className="mb-1 block text-xs font-medium text-neutral-400">
                Who to ask
              </label>
              {contactsError ? (
                <div className="flex items-center gap-2">
                  <p role="alert" className="flex-1 text-xs text-red-400">
                    Couldn&apos;t load contacts: {contactsError}
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setContactsError(null);
                      void listTrustedAgents()
                        .then((res) => setContacts(res.agents ?? []))
                        .catch((err: unknown) =>
                          setContactsError(
                            err instanceof ApiError
                              ? err.message
                              : "Could not load contacts."
                          )
                        );
                    }}
                  >
                    Retry
                  </Button>
                </div>
              ) : contacts.length === 0 ? (
                <p className="text-xs text-neutral-500">
                  No trusted contacts yet. Connect with someone from People first.
                </p>
              ) : (
                <select
                  value={askContact}
                  onChange={(e) => setAskContact(e.target.value)}
                  className="w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 font-mono text-sm text-neutral-100 focus:border-blue-500 focus:outline-none"
                >
                  <option value="">-- Select a person --</option>
                  {contacts.map((c) => (
                    <option key={c.agent_id} value={c.agent_id}>
                      {c.display_name}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-neutral-400">
                  What to ask for
                </label>
                {capsLoading ? (
                  <p className="py-2 text-xs text-neutral-500" aria-busy="true">
                    Loading capabilities…
                  </p>
                ) : capsError ? (
                  <div className="flex items-center gap-2">
                    <p role="alert" className="flex-1 text-xs text-red-400">
                      Couldn&apos;t load capabilities.
                    </p>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={reloadCaps}
                    >
                      Retry
                    </Button>
                  </div>
                ) : (
                  <select
                    value={askCapability}
                    onChange={(e) => setAskCapability(e.target.value)}
                    className="w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 font-mono text-sm text-neutral-100 focus:border-blue-500 focus:outline-none"
                  >
                    <option value="">-- Select --</option>
                    {capabilities.map((cap) => (
                      <option key={cap.id} value={cap.id} title={cap.description}>
                        {cap.id}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-neutral-400">
                  Purpose
                </label>
                <input
                  type="text"
                  value={askPurpose}
                  onChange={(e) => setAskPurpose(e.target.value)}
                  placeholder="e.g. Thursday planning"
                  maxLength={64}
                  className="w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none"
                />
              </div>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-neutral-400">
                What should they do?
              </label>
              <textarea
                value={askSummary}
                onChange={(e) => setAskSummary(e.target.value)}
                rows={3}
                placeholder="e.g. Are you free Thursday at 6pm?"
                className="w-full rounded-md border border-neutral-800 bg-neutral-900 p-3 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none"
              />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={closeAsk}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                size="sm"
                disabled={askSending || !askContact || !askTaskType || !askSummary.trim()}
                isLoading={askSending}
              >
                Send request
              </Button>
            </div>
          </form>
        )}
      </Modal>
    </div>
  );
}
