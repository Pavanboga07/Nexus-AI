"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge, StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { getPolicyAudit } from "@/lib/api/policy";
import { getToolAudit } from "@/lib/api/tools";
import { getA2AAudit } from "@/lib/api/a2a";
import { formatDate } from "@/lib/utils";
import {
  loadContactNames,
  resolveContactName,
} from "@/lib/useContactNames";
import {
  RefreshCw,
  Search,
} from "lucide-react";

type UnifiedEvent = {
  id: string;
  category: "policy" | "tool" | "a2a";
  timestamp: string | null;
  title: string;
  subtitle: string;
  status: string;
  raw: unknown;
};

export default function ActivityPage() {
  const [events, setEvents] = useState<UnifiedEvent[]>([]);
  const [filteredEvents, setFilteredEvents] = useState<UnifiedEvent[]>([]);
  const [activeCategory, setActiveCategory] = useState("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedEvent, setSelectedEvent] = useState<UnifiedEvent | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState<string[]>([]);

  const fetchAllAuditData = async () => {
    try {
      setLoading(true);
      // Contact names resolve agent IDs to display names in subtitles; unknown
      // IDs fall back to a short slice (never a raw ID).
      const names = await loadContactNames();
      const [polRes, toolRes, a2aRes] = await Promise.allSettled([
        getPolicyAudit(),
        getToolAudit(),
        getA2AAudit(),
      ]);

      const unified: UnifiedEvent[] = [];
      // An audit source that failed is reported, not silently omitted: a trail
      // with a hole in it looks identical to a trail with nothing in it, and
      // only one of those is safe to conclude from.
      const failures: string[] = [];

      // Every field name here is the wire name. The previous version read
      // `audits`, `timestamp`, `resource`, `entries`, `caller`, `duration_ms`
      // and `success` - none of which any of these endpoints returns.
      if (polRes.status === "fulfilled") {
        polRes.value.decisions.forEach((d) => {
          unified.push({
            id: d.id,
            category: "policy",
            timestamp: d.created_at ?? null,
            title: `Policy: ${d.data_category}:${d.action}`,
            subtitle: `Purpose: ${d.purpose} · From: ${resolveContactName(names, d.requester_agent_id)} · ${d.reason}`,
            status: d.decision,
            raw: d,
          });
        });
      } else {
        failures.push("policy decisions");
      }

      if (toolRes.status === "fulfilled") {
        toolRes.value.executions.forEach((t) => {
          unified.push({
            id: t.id,
            category: "tool",
            timestamp: t.created_at ?? null,
            title: `Tool: ${t.tool_name}`,
            subtitle: `Purpose: ${t.purpose} · Policy: ${t.policy_decision}${
              t.error_code ? ` · ${t.error_code}` : ""
            }`,
            status: t.status,
            raw: t,
          });
        });
      } else {
        failures.push("tool executions");
      }

      if (a2aRes.status === "fulfilled") {
        a2aRes.value.messages.forEach((m) => {
          unified.push({
            id: m.id,
            category: "a2a",
            timestamp: m.created_at ?? null,
            title: `Agent message: ${m.message_type}`,
            subtitle: `From: ${resolveContactName(names, m.sender_agent_id)} -> To: ${resolveContactName(
              names,
              m.recipient_agent_id
            )} · ${m.status}${m.policy_decision ? ` · policy ${m.policy_decision}` : ""}`,
            status: m.status,
            raw: m,
          });
        });
      } else {
        failures.push("agent messages");
      }

      setUnavailable(failures);

      // Sort by timestamp descending. Records without one are kept, at the end,
      // rather than collapsing to 1970 and being buried.
      unified.sort((a, b) => {
        if (!a.timestamp && !b.timestamp) return 0;
        if (!a.timestamp) return 1;
        if (!b.timestamp) return -1;
        return new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime();
      });

      setEvents(unified);
    } catch (err) {
      console.error("Failed to load audit logs", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAllAuditData();
  }, []);

  // Filter events
  useEffect(() => {
    let list = events;
    if (activeCategory !== "all") {
      list = list.filter((e) => e.category === activeCategory);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(
        (e) =>
          e.title.toLowerCase().includes(q) ||
          e.subtitle.toLowerCase().includes(q) ||
          e.status.toLowerCase().includes(q)
      );
    }
    setFilteredEvents(list);
  }, [events, activeCategory, searchQuery]);

  const filters = [
    { id: "all", label: `All Events (${events.length})` },
    { id: "policy", label: `Policy (${events.filter(e => e.category === "policy").length})` },
    { id: "tool", label: `Tools (${events.filter(e => e.category === "tool").length})` },
    { id: "a2a", label: `A2A (${events.filter(e => e.category === "a2a").length})` },
  ];

  // Group events by date. Records the backend left undated are grouped under
  // their own heading rather than being dated 1 Jan 1970 by `new Date(null)`.
  const groupedEvents = filteredEvents.reduce((acc, event) => {
    const date = event.timestamp
      ? new Date(event.timestamp).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          year: "numeric",
        })
      : "No timestamp recorded";
    if (!acc[date]) {
      acc[date] = [];
    }
    acc[date].push(event);
    return acc;
  }, {} as Record<string, UnifiedEvent[]>);

  return (
    <PageShell 
      title="Activity" 
      subtitle="Everything your agent decided or sent: policy evaluations, tool runs, and agent messages"
      action={
        <Button
          variant="outline"
          size="sm"
          leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
          onClick={fetchAllAuditData}
        >
          Refresh
        </Button>
      }
    >
      {unavailable.length > 0 && (
        <div className="mb-4 rounded-md border border-amber-900/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          Some sources could not be read, so this trail is incomplete:{" "}
          {unavailable.join(", ")}.
        </div>
      )}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
        <div className="flex flex-wrap gap-2">
          {filters.map(f => (
            <button
              key={f.id}
              onClick={() => setActiveCategory(f.id)}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                activeCategory === f.id 
                  ? "bg-neutral-800 text-neutral-100" 
                  : "text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div className="relative max-w-xs w-full">
          <Search className="w-4 h-4 text-neutral-500 absolute left-3 top-2.5" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search audit trail..."
            className="w-full bg-neutral-900 border border-neutral-800 rounded-md pl-9 pr-4 py-1.5 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-blue-500"
          />
        </div>
      </div>

      {loading ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">Loading audit feed...</p>
        </div>
      ) : filteredEvents.length === 0 ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">No entries found matching filter.</p>
        </div>
      ) : (
        <div className="space-y-8">
          {Object.entries(groupedEvents).map(([date, dayEvents]) => (
            <div key={date}>
              <h3 className="text-xs font-medium text-neutral-500 mb-3 tracking-wide uppercase">
                {date}
              </h3>
              <div className="divide-y divide-neutral-800 border-t border-neutral-800">
                {dayEvents.map((evt) => {
                  const categoryBadge = {
                    policy: <Badge variant="info">POLICY</Badge>,
                    tool: <Badge variant="warning">TOOL</Badge>,
                    a2a: <Badge variant="default">A2A</Badge>,
                  }[evt.category];

                  return (
                    <div
                      key={evt.id}
                      onClick={() => setSelectedEvent(evt)}
                      className="py-4 flex items-center justify-between gap-4 cursor-pointer hover:bg-neutral-900/50 transition-colors -mx-2 px-2 rounded-md"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        {categoryBadge}
                        <div className="min-w-0">
                          <div className="text-sm font-medium text-neutral-100 truncate">
                            {evt.title}
                          </div>
                          <div className="text-xs text-neutral-400 truncate mt-0.5">
                            {evt.subtitle}
                          </div>
                        </div>
                      </div>

                      <div className="flex flex-col items-end gap-1.5 shrink-0">
                        <StatusBadge status={evt.status} />
                        <span className="text-[11px] text-neutral-500 font-mono">
                          {evt.timestamp
                            ? new Date(evt.timestamp).toLocaleTimeString(undefined, {
                                hour: "2-digit",
                                minute: "2-digit",
                              })
                            : "—"}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Raw Event Detail Modal */}
      {selectedEvent && (
        <Modal
          isOpen={!!selectedEvent}
          onClose={() => setSelectedEvent(null)}
          title={selectedEvent.title}
          subtitle={`Recorded ${formatDate(selectedEvent.timestamp)}`}
          maxWidth="xl"
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-neutral-800">
              <span className="text-xs text-neutral-400">Status / Decision:</span>
              <StatusBadge status={selectedEvent.status} />
            </div>

            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Raw Event Payload
              </label>
              <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto max-h-72">
                {JSON.stringify(selectedEvent.raw, null, 2)}
              </pre>
            </div>

            <div className="pt-2 flex justify-end">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setSelectedEvent(null)}
              >
                Close
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </PageShell>
  );
}
