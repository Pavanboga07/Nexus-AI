"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge, StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import {
  listTasks,
  getTask,
  delegateTask,
  approveTask,
  rejectTask,
  cancelTask,
  negotiateTask,
} from "@/lib/api/tasks";
import { listTrustedAgents } from "@/lib/api/a2a";
import { A2ATask, TrustedAgent } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";
import {
  Plus,
  RefreshCw,
} from "lucide-react";

export default function TasksPage() {
  const [tasks, setTasks] = useState<A2ATask[]>([]);
  const [trustedAgents, setTrustedAgents] = useState<TrustedAgent[]>([]);
  const [selectedTask, setSelectedTask] = useState<A2ATask | null>(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [loading, setLoading] = useState(false);

  // New task modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [recipientAgentId, setRecipientAgentId] = useState("");
  const [taskType, setTaskType] = useState("availability_query");
  const [purpose, setPurpose] = useState("");
  const [payloadText, setPayloadText] = useState('{"date": "2026-09-15"}');
  const [creating, setCreating] = useState(false);

  // Negotiation modal
  const [negotiateModalOpen, setNegotiateModalOpen] = useState(false);
  const [counterTerms, setCounterTerms] = useState('{"candidate_time": "15:00"}');
  const [negotiating, setNegotiating] = useState(false);

  const fetchTasks = async (status?: string) => {
    try {
      setLoading(true);
      const s = status === "all" ? undefined : status;
      const res = await listTasks(s);
      setTasks(res.tasks || []);
    } catch (err) {
      console.error("Failed to load tasks", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks(statusFilter);
    listTrustedAgents()
      .then((res) => setTrustedAgents(res.agents || []))
      .catch(() => {});
  }, [statusFilter]);

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!recipientAgentId) {
      alert("Please select a recipient agent.");
      return;
    }
    let payloadObj = {};
    try {
      payloadObj = JSON.parse(payloadText);
    } catch {
      alert("Payload must be valid JSON.");
      return;
    }

    try {
      setCreating(true);
      await delegateTask({
        recipient_agent_id: recipientAgentId,
        task_type: taskType,
        purpose: purpose || `Delegated ${taskType}`,
        payload: payloadObj,
      });
      setCreateModalOpen(false);
      setPurpose("");
      await fetchTasks(statusFilter);
    } catch (err: any) {
      alert(`Failed to delegate task: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleApprove = async (taskId: string) => {
    try {
      await approveTask(taskId);
      await fetchTasks(statusFilter);
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        setSelectedTask(updated);
      }
    } catch (err: any) {
      alert(`Failed to approve task: ${err.message}`);
    }
  };

  const handleReject = async (taskId: string) => {
    const reason = prompt("Enter reason for rejection:", "Declined by owner");
    if (!reason) return;
    try {
      await rejectTask(taskId, reason);
      await fetchTasks(statusFilter);
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        setSelectedTask(updated);
      }
    } catch (err: any) {
      alert(`Failed to reject task: ${err.message}`);
    }
  };

  const handleCancel = async (taskId: string) => {
    if (!confirm("Are you sure you want to cancel this task?")) return;
    try {
      await cancelTask(taskId);
      await fetchTasks(statusFilter);
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        setSelectedTask(updated);
      }
    } catch (err: any) {
      alert(`Failed to cancel task: ${err.message}`);
    }
  };

  const handleNegotiateSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedTask) return;
    let termsObj = {};
    try {
      termsObj = JSON.parse(counterTerms);
    } catch {
      alert("Counter terms must be valid JSON.");
      return;
    }

    try {
      setNegotiating(true);
      await negotiateTask(selectedTask.task_id, {
        proposal_payload: termsObj,
        purpose: "negotiate-terms",
      });
      setNegotiateModalOpen(false);
      await fetchTasks(statusFilter);
      const updated = await getTask(selectedTask.task_id);
      setSelectedTask(updated);
    } catch (err: any) {
      alert(`Failed to submit counter-offer: ${err.message}`);
    } finally {
      setNegotiating(false);
    }
  };

  const filters = [
    { id: "all", label: "All" },
    { id: "pending", label: "Pending" },
    { id: "negotiating", label: "Negotiating" },
    { id: "completed", label: "Completed" },
    { id: "failed", label: "Failed" },
  ];

  return (
    <PageShell 
      title="Tasks" 
      subtitle="Asynchronous task delegation, multi-round negotiations, and consent approvals"
      action={
        <Button
          size="sm"
          leftIcon={<Plus className="w-3.5 h-3.5" />}
          onClick={() => setCreateModalOpen(true)}
        >
          Delegate Task
        </Button>
      }
    >
      <div className="flex items-center justify-between mb-6">
        <div className="flex gap-2">
          {filters.map(f => (
            <button
              key={f.id}
              onClick={() => setStatusFilter(f.id)}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                statusFilter === f.id 
                  ? "bg-neutral-800 text-neutral-100" 
                  : "text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <Button
          variant="outline"
          size="sm"
          leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
          onClick={() => fetchTasks(statusFilter)}
        >
          Refresh
        </Button>
      </div>

      {loading ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">Loading tasks...</p>
        </div>
      ) : tasks.length === 0 ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">No tasks found in this view.</p>
          <p className="text-xs text-neutral-500 mt-1">
            Tasks represent delegated actions with remote agents.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-neutral-800">
          {tasks.map((task) => (
            <div key={task.task_id} className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={task.status} />
                  <span className="text-sm font-medium text-neutral-100">
                    {task.capability || (task as any).task_type || "A2A Task"}
                  </span>
                  {task.round > 1 && (
                    <Badge variant="info">Round {task.round}</Badge>
                  )}
                </div>
                <div className="text-xs text-neutral-400 mt-1.5 font-mono flex items-center gap-3">
                  <span>ID: {truncateId(task.task_id, 8)}</span>
                  <span>Peer: {truncateId(task.recipient_id || (task as any).sender_id, 8)}</span>
                  <span>{formatDate(task.created_at)}</span>
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                {task.status === "pending" && (
                  <>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => handleApprove(task.task_id)}
                    >
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleReject(task.task_id)}
                    >
                      Reject
                    </Button>
                  </>
                )}
                {task.status === "negotiating" && (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => {
                      setSelectedTask(task);
                      setNegotiateModalOpen(true);
                    }}
                  >
                    Counter
                  </Button>
                )}
                {["pending", "running", "negotiating"].includes(task.status) && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleCancel(task.task_id)}
                  >
                    Cancel
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setSelectedTask(task)}
                >
                  Details
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Delegate Task Modal */}
      <Modal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title="Delegate New Task"
        subtitle="Initiate a secure task to a trusted peer"
      >
        <form onSubmit={handleCreateTask} className="space-y-4">
          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Recipient Agent
            </label>
            {trustedAgents.length === 0 ? (
              <div className="p-3 bg-amber-950/20 text-xs text-amber-500">
                No trusted agents available.
              </div>
            ) : (
              <select
                value={recipientAgentId}
                onChange={(e) => setRecipientAgentId(e.target.value)}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 font-mono"
              >
                <option value="">-- Select Agent --</option>
                {trustedAgents.map((agt) => (
                  <option key={agt.agent_id} value={agt.agent_id}>
                    {agt.display_name} ({truncateId(agt.agent_id, 8)})
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Capability
              </label>
              <select
                value={taskType}
                onChange={(e) => setTaskType(e.target.value)}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 font-mono"
              >
                <option value="availability_query">availability_query</option>
                <option value="meeting_proposal">meeting_proposal</option>
                <option value="calculator">calculator</option>
                <option value="custom">custom</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Purpose
              </label>
              <input
                type="text"
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                placeholder="e.g. Sync availability"
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Payload (JSON)
            </label>
            <textarea
              value={payloadText}
              onChange={(e) => setPayloadText(e.target.value)}
              rows={4}
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md p-3 text-sm text-neutral-100 font-mono focus:outline-none focus:border-blue-500"
            />
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setCreateModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={creating || !recipientAgentId}
              isLoading={creating}
            >
              Dispatch Task
            </Button>
          </div>
        </form>
      </Modal>

      {/* Task Details Modal */}
      {selectedTask && (
        <Modal
          isOpen={!!selectedTask}
          onClose={() => setSelectedTask(null)}
          title="Task Details"
          subtitle={`Task ID: ${selectedTask.task_id}`}
          maxWidth="2xl"
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-neutral-800">
              <div className="flex items-center gap-2">
                <StatusBadge status={selectedTask.status} />
                <Badge variant="outline">
                  Round {selectedTask.round} / {selectedTask.max_rounds || 5}
                </Badge>
              </div>
              <span className="text-xs text-neutral-400 font-mono">
                {formatDate(selectedTask.created_at)}
              </span>
            </div>

            {selectedTask.history && selectedTask.history.length > 0 && (
              <div>
                <label className="text-xs text-neutral-400 font-medium block mb-2">
                  Negotiation Timeline
                </label>
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {selectedTask.history.map((h, i) => (
                    <div
                      key={i}
                      className="p-3 bg-neutral-900 border border-neutral-800 text-xs"
                    >
                      <div className="flex items-center justify-between text-neutral-400 font-mono mb-1">
                        <span>Round {h.round}</span>
                        <span>By: {truncateId(h.proposed_by, 8)}</span>
                      </div>
                      <pre className="text-neutral-300 font-mono text-[11px] overflow-x-auto">
                        {JSON.stringify(h.terms, null, 2)}
                      </pre>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Input Payload
              </label>
              <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto">
                {JSON.stringify(selectedTask.payload, null, 2)}
              </pre>
            </div>

            {selectedTask.result != null && (
              <div>
                <label className="text-xs text-neutral-400 font-medium block mb-1">
                  Result
                </label>
                <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto">
                  {JSON.stringify(selectedTask.result, null, 2)}
                </pre>
              </div>
            )}

            <div className="pt-3 border-t border-neutral-800 flex justify-end gap-2">
              {selectedTask.status === "negotiating" && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => setNegotiateModalOpen(true)}
                >
                  Counter-Offer
                </Button>
              )}
              {selectedTask.status === "pending" && (
                <>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => handleApprove(selectedTask.task_id)}
                  >
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => handleReject(selectedTask.task_id)}
                  >
                    Reject
                  </Button>
                </>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() => setSelectedTask(null)}
              >
                Close
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* Counter-Offer Modal */}
      <Modal
        isOpen={negotiateModalOpen}
        onClose={() => setNegotiateModalOpen(false)}
        title="Submit Counter-Offer"
        subtitle="Propose adjusted parameters to peer agent"
      >
        <form onSubmit={handleNegotiateSubmit} className="space-y-4">
          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Counter Terms (JSON)
            </label>
            <textarea
              value={counterTerms}
              onChange={(e) => setCounterTerms(e.target.value)}
              rows={4}
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md p-3 text-sm text-neutral-100 font-mono focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setNegotiateModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={negotiating}
              isLoading={negotiating}
            >
              Send
            </Button>
          </div>
        </form>
      </Modal>
    </PageShell>
  );
}
