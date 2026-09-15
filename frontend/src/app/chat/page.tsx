"use client";

import React, { useEffect, useState, useRef } from "react";
import { ChatSidebar } from "@/components/chat/ChatSidebar";
import {
  listSessions,
  createSession,
  getSession,
  clearSession,
  sendChatMessage,
} from "@/lib/api/chat";
import { Message } from "@/types/api";
import { ArrowUp, Copy, Check, Trash2 } from "lucide-react";

export default function ChatPage() {
  const [sessions, setSessions] = useState<string[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputMessage, setInputMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Load sessions on mount
  useEffect(() => {
    async function loadSessions() {
      try {
        setLoading(true);
        const sids = await listSessions();
        setSessions(sids);
        if (sids.length > 0) {
          setActiveSessionId(sids[0]);
        } else {
          const created = await createSession();
          setSessions([created.session_id]);
          setActiveSessionId(created.session_id);
        }
      } catch (err) {
        console.error("Failed to load sessions", err);
      } finally {
        setLoading(false);
      }
    }
    loadSessions();
  }, []);

  // Load session messages
  useEffect(() => {
    if (!activeSessionId) return;
    async function loadMessages() {
      try {
        const sessionData = await getSession(activeSessionId!);
        setMessages(sessionData.messages || []);
      } catch (err) {
        console.error("Failed to load session messages", err);
      }
    }
    loadMessages();
  }, [activeSessionId]);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  const handleCreateSession = async () => {
    try {
      const created = await createSession();
      setSessions((prev) => [created.session_id, ...prev]);
      setActiveSessionId(created.session_id);
      setMessages([]);
    } catch (err) {
      console.error("Failed to create session", err);
    }
  };

  const handleClearSession = async () => {
    if (!activeSessionId) return;
    if (!confirm("Clear this conversation?")) return;
    try {
      await clearSession(activeSessionId);
      setMessages([]);
    } catch (err) {
      console.error("Failed to clear session", err);
    }
  };

  const handleSendMessage = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!inputMessage.trim() || !activeSessionId || sending) return;

    const userText = inputMessage.trim();
    setInputMessage("");

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }

    const userMsg: Message = { role: "user", content: userText };
    setMessages((prev) => [...prev, userMsg]);
    setSending(true);

    try {
      const res = await sendChatMessage(activeSessionId, userText);
      const assistantMsg: Message = { role: "assistant", content: res.response };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: any) {
      const errorMsg: Message = {
        role: "assistant",
        content: `Something went wrong: ${err.message || "Failed to get a response."}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setSending(false);
    }
  };

  const copyMessage = (text: string, index: number) => {
    navigator.clipboard.writeText(text);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const handleTextareaChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInputMessage(e.target.value);
    // Auto-resize
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  };

  return (
    <div className="flex h-full bg-neutral-950">
      {/* Sidebar */}
      <ChatSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onNewSession={handleCreateSession}
      />

      {/* Chat area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Minimal top bar - only shows clear action */}
        {messages.length > 0 && (
          <div className="flex items-center justify-end px-4 py-2 border-b border-neutral-800/30">
            <button
              onClick={handleClearSession}
              className="flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition-colors px-2 py-1 rounded hover:bg-neutral-800/50"
            >
              <Trash2 className="w-3 h-3" />
              Clear
            </button>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto">
          {messages.length === 0 && !loading ? (
            /* Empty state */
            <div className="h-full flex flex-col items-center justify-center px-4">
              <div className="text-center max-w-md space-y-6">
                <div>
                  <h2 className="text-2xl font-semibold text-neutral-100 tracking-tight">
                    Nexus
                  </h2>
                  <p className="text-neutral-400 mt-2 text-sm">
                    How can I help?
                  </p>
                </div>

                <div className="space-y-2 w-full max-w-sm mx-auto">
                  <button
                    onClick={() =>
                      setInputMessage("What can you do for me as my personal AI?")
                    }
                    className="w-full text-left px-4 py-3 rounded-lg border border-neutral-800 text-sm text-neutral-400 hover:text-neutral-200 hover:border-neutral-700 transition-colors"
                  >
                    What can you do for me?
                  </button>
                  <button
                    onClick={() =>
                      setInputMessage("Check my schedule availability for tomorrow.")
                    }
                    className="w-full text-left px-4 py-3 rounded-lg border border-neutral-800 text-sm text-neutral-400 hover:text-neutral-200 hover:border-neutral-700 transition-colors"
                  >
                    Check my schedule for tomorrow
                  </button>
                </div>
              </div>
            </div>
          ) : (
            /* Message list */
            <div className="max-w-chat mx-auto px-4 py-6 space-y-6">
              {messages.map((msg, index) => {
                const isUser = msg.role === "user";
                return (
                  <div key={index} className="group">
                    {/* Sender label */}
                    <div className="text-xs font-medium text-neutral-500 mb-1.5">
                      {isUser ? "You" : "Nexus"}
                    </div>

                    {/* Message content */}
                    <div className="relative">
                      <div
                        className={`text-sm leading-relaxed whitespace-pre-wrap ${
                          isUser ? "text-neutral-200" : "text-neutral-300"
                        }`}
                      >
                        {msg.content}
                      </div>

                      {/* Copy button */}
                      <button
                        onClick={() => copyMessage(msg.content, index)}
                        className="absolute -top-1 right-0 opacity-0 group-hover:opacity-100 p-1 rounded text-neutral-500 hover:text-neutral-300 transition-opacity"
                        title="Copy"
                      >
                        {copiedIndex === index ? (
                          <Check className="w-3.5 h-3.5 text-emerald-500" />
                        ) : (
                          <Copy className="w-3.5 h-3.5" />
                        )}
                      </button>
                    </div>
                  </div>
                );
              })}

              {/* Thinking indicator */}
              {sending && (
                <div>
                  <div className="text-xs font-medium text-neutral-500 mb-1.5">
                    Nexus
                  </div>
                  <div className="flex items-center gap-1 text-neutral-500">
                    <span className="w-1.5 h-1.5 rounded-full bg-neutral-500 animate-blink-1" />
                    <span className="w-1.5 h-1.5 rounded-full bg-neutral-500 animate-blink-2" />
                    <span className="w-1.5 h-1.5 rounded-full bg-neutral-500 animate-blink-3" />
                    <span className="text-xs text-neutral-500 ml-2">
                      Thinking...
                    </span>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Composer */}
        <div className="border-t border-neutral-800/50 bg-neutral-950">
          <div className="max-w-chat mx-auto px-4 py-3">
            <form
              onSubmit={handleSendMessage}
              className="relative flex items-end gap-2"
            >
              <textarea
                ref={textareaRef}
                value={inputMessage}
                onChange={handleTextareaChange}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSendMessage();
                  }
                }}
                rows={1}
                placeholder="Message Nexus..."
                className="flex-1 bg-neutral-900 border border-neutral-800 focus:border-neutral-600 rounded-lg px-4 py-3 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none resize-none min-h-[44px] max-h-[200px]"
              />
              {inputMessage.trim() && (
                <button
                  type="submit"
                  disabled={sending}
                  className="shrink-0 w-8 h-8 flex items-center justify-center rounded-md bg-white text-neutral-900 hover:bg-neutral-200 disabled:opacity-40 transition-colors"
                >
                  <ArrowUp className="w-4 h-4" />
                </button>
              )}
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
