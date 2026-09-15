"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { Plus, Settings, Search } from "lucide-react";
import { StatusDot } from "@/components/layout/StatusIndicator";
import { getSession } from "@/lib/api/chat";

interface ChatSidebarProps {
  sessions: string[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
}

// Simple time grouping
function getTimeGroup(sessions: string[], _index: number): string | null {
  // Since sessions are just IDs without timestamps from the list endpoint,
  // we just show them in order. In a richer implementation, 
  // we'd group by creation date.
  if (_index === 0) return "Conversations";
  return null;
}

export function ChatSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
}: ChatSidebarProps) {
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearch, setShowSearch] = useState(false);

  // Fetch first message preview for each session
  useEffect(() => {
    async function loadPreviews() {
      const newPreviews: Record<string, string> = {};
      for (const sid of sessions.slice(0, 20)) {
        if (previews[sid]) continue;
        try {
          const session = await getSession(sid);
          const firstUserMsg = session.messages?.find(
            (m) => m.role === "user"
          );
          newPreviews[sid] = firstUserMsg
            ? firstUserMsg.content.slice(0, 60)
            : "New conversation";
        } catch {
          newPreviews[sid] = "New conversation";
        }
      }
      if (Object.keys(newPreviews).length > 0) {
        setPreviews((prev) => ({ ...prev, ...newPreviews }));
      }
    }
    if (sessions.length > 0) loadPreviews();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions]);

  const filteredSessions = searchQuery
    ? sessions.filter((sid) => {
        const preview = previews[sid] || "";
        return preview.toLowerCase().includes(searchQuery.toLowerCase());
      })
    : sessions;

  return (
    <aside className="w-[260px] bg-neutral-950 border-r border-neutral-800/60 flex flex-col h-full shrink-0 select-none">
      {/* Brand */}
      <div className="px-4 pt-4 pb-2">
        <Link href="/" className="text-base font-semibold text-neutral-100 tracking-tight">
          Nexus
        </Link>
      </div>

      {/* New chat + Search */}
      <div className="px-3 pb-2 space-y-1">
        <button
          onClick={onNewSession}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-neutral-300 hover:bg-neutral-800/60 rounded-md transition-colors"
        >
          <Plus className="w-4 h-4 text-neutral-500" />
          New chat
        </button>
        <button
          onClick={() => setShowSearch(!showSearch)}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-neutral-400 hover:bg-neutral-800/60 rounded-md transition-colors"
        >
          <Search className="w-4 h-4 text-neutral-500" />
          Search
        </button>
        {showSearch && (
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search conversations..."
            autoFocus
            className="w-full px-3 py-1.5 text-xs bg-neutral-900 border border-neutral-800 rounded-md text-neutral-200 placeholder-neutral-500 focus:outline-none focus:border-neutral-600"
          />
        )}
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {filteredSessions.map((sid, index) => {
          const isActive = sid === activeSessionId;
          const preview = previews[sid] || "New conversation";
          const group = getTimeGroup(filteredSessions, index);

          return (
            <React.Fragment key={sid}>
              {group && (
                <div className="px-2 pt-4 pb-1 text-[11px] font-medium text-neutral-500 uppercase tracking-wider">
                  {group}
                </div>
              )}
              <button
                onClick={() => onSelectSession(sid)}
                className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors truncate ${
                  isActive
                    ? "bg-neutral-800/80 text-neutral-100"
                    : "text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/40"
                }`}
              >
                <span className="truncate block">{preview}</span>
              </button>
            </React.Fragment>
          );
        })}
      </div>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-neutral-800/60 space-y-2">
        <StatusDot />
        <Link
          href="/settings"
          className="flex items-center gap-2 text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
        >
          <Settings className="w-3.5 h-3.5" />
          Settings
        </Link>
      </div>
    </aside>
  );
}
