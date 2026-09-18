"use client";

import React, { useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge, DecisionBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ErrorState } from "@/components/ui/ErrorState";
import { useAsync } from "@/lib/useAsync";
import {
  listPolicies,
  listConsents,
  evaluatePolicy,
  getPolicyAudit,
  PolicyAuditEntry,
} from "@/lib/api/policy";
import { PolicyRule, Consent, PolicyEvaluateResponse } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";
import {
  Shield,
  CheckCircle2,
  Play,
  RefreshCw,
  Activity,
} from "lucide-react";

export default function PermissionsPage() {
  // The three sources load together; a total failure throws so the page shows
  // an error with Retry, while a partial failure lists what is missing.
  const { data, error: loadError, loading, reload } = useAsync(async () => {
    const [polRes, conRes, audRes] = await Promise.allSettled([
      listPolicies(),
      listConsents(),
      getPolicyAudit(),
    ]);
    const failed: string[] = [];
    let policies: PolicyRule[] = [];
    let consents: Consent[] = [];
    let audits: PolicyAuditEntry[] = [];
    if (polRes.status === "fulfilled") policies = polRes.value.policies ?? [];
    else failed.push("policy rules");
    if (conRes.status === "fulfilled") consents = conRes.value.consents ?? [];
    else failed.push("consents");
    if (audRes.status === "fulfilled") audits = audRes.value.decisions ?? [];
    else failed.push("decision audit");
    if (failed.length === 3) {
      throw new Error(
        "Could not load permissions. The policy subsystem may be unavailable."
      );
    }
    return { policies, consents, audits, failed };
  });
  const policies = data?.policies ?? [];
  const consents = data?.consents ?? [];
  const audits = data?.audits ?? [];
  const failedSources = data?.failed ?? [];

  // Simulator state
  const [isSimulatorOpen, setIsSimulatorOpen] = useState(false);
  const [simAction, setSimAction] = useState("tool:execute");
  const [simResource, setSimResource] = useState("tool:calculator");
  const [simPeerId, setSimPeerId] = useState("");
  const [simResult, setSimResult] = useState<PolicyEvaluateResponse | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);

  const handleSimulate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (simulating) return;
    try {
      setSimulating(true);
      setSimResult(null);
      setSimError(null);
      const res = await evaluatePolicy({
        action: simAction,
        resource: simResource,
        peer_agent_id: simPeerId || undefined,
      });
      setSimResult(res);
    } catch (err: any) {
      setSimError(`Policy evaluation failed: ${err.message}`);
    } finally {
      setSimulating(false);
    }
  };

  return (
    <PageShell
      title="Permissions"
      subtitle="Deterministic owner-controlled security policies and human-in-the-loop authorization"
      action={
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setIsSimulatorOpen(true)}
          >
            <Play className="w-4 h-4 mr-2 text-blue-500" />
            Simulator
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </Button>
        </div>
      }
    >
      <div className="space-y-12">
        {loadError && <ErrorState message={loadError} onRetry={reload} />}
        {failedSources.length > 0 && (
          <div className="rounded-md border border-amber-900/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
            Some sources could not be read, so this view is incomplete:{" "}
            {failedSources.join(", ")}.
          </div>
        )}
        {/* Policies Section */}
        <div>
          <div className="mb-4">
            <h2 className="text-sm font-medium text-neutral-100 flex items-center gap-2">
              <Shield className="w-4 h-4 text-neutral-400" />
              Policy Rules
            </h2>
          </div>
          
          {loading ? (
            <div className="py-16 text-center">
              <p className="text-sm text-neutral-400">Loading...</p>
            </div>
          ) : loadError && policies.length === 0 ? null : policies.length === 0 ? (
            <div className="py-16 text-center border border-neutral-800 rounded-lg">
              <p className="text-sm text-neutral-400">No custom policy rules configured.</p>
              <p className="text-xs text-neutral-500 mt-1">
                Nexus default rule: Non-destructive read actions ALLOW; sensitive actions ASK.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              {policies.map((p) => (
                <div key={p.id} className="py-4 flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-neutral-100">
                      {p.action} <span className="text-neutral-500 mx-1">on</span>{" "}
                      {p.data_category}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5">
                      For <span className="text-neutral-300">{p.purpose}</span>{" "}
                      · discloses{" "}
                      <span className="text-neutral-300">{p.disclosure_scope}</span>
                    </div>
                    <div className="text-xs text-neutral-500 mt-0.5 font-mono">
                      Rule {truncateId(p.id, 6)} · from{" "}
                      {truncateId(p.requester_agent_id, 10)} · priority{" "}
                      {p.priority}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <DecisionBadge decision={p.decision} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Consents Section */}
        <div>
          <div className="mb-4">
            <h2 className="text-sm font-medium text-neutral-100 flex items-center gap-2">
              <CheckCircle2 className="w-4 h-4 text-neutral-400" />
              Active Consents
            </h2>
          </div>

          {consents.length === 0 ? (
            <div className="py-16 text-center border border-neutral-800 rounded-lg">
              <p className="text-sm text-neutral-400">No active owner consents recorded.</p>
              <p className="text-xs text-neutral-500 mt-1">
                Consents are created dynamically when you approve an ASK prompt in chat.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              {consents.map((c) => {
                const expired =
                  !!c.expires_at && new Date(c.expires_at).getTime() < Date.now();
                return (
                  <div key={c.id} className="py-4 flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium text-neutral-100">
                        {c.action} <span className="text-neutral-500 mx-1">on</span>{" "}
                        {c.data_category}
                      </div>
                      <div className="text-xs text-neutral-400 mt-0.5">
                        For <span className="text-neutral-300">{c.purpose}</span>{" "}
                        · discloses{" "}
                        <span className="text-neutral-300">{c.disclosure_scope}</span>
                      </div>
                      <div className="text-xs text-neutral-500 mt-0.5 font-mono">
                        {truncateId(c.id, 8)} · from{" "}
                        {truncateId(c.requester_agent_id, 10)} ·{" "}
                        {c.expires_at ? `expires ${formatDate(c.expires_at)}` : "no expiry"}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {/* `used_at` is how the backend reports a spent consent;
                          the old code read a `used` boolean that does not exist
                          and so always showed "Reusable". */}
                      {c.used_at ? (
                        <Badge variant="warning">Used</Badge>
                      ) : expired ? (
                        <Badge variant="warning">Expired</Badge>
                      ) : (
                        <Badge variant={c.single_use ? "warning" : "info"}>
                          {c.single_use ? "Single-use" : "Reusable"}
                        </Badge>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Audit Section */}
        <div>
          <div className="mb-4">
            <h2 className="text-sm font-medium text-neutral-100 flex items-center gap-2">
              <Activity className="w-4 h-4 text-neutral-400" />
              Decision Audit
            </h2>
          </div>

          {audits.length === 0 ? (
            <div className="py-16 text-center border border-neutral-800 rounded-lg">
              <p className="text-sm text-neutral-400">No policy evaluation audits recorded yet.</p>
            </div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              {audits.map((a) => (
                <div key={a.id} className="py-4 flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-neutral-100">
                      {a.action} <span className="text-neutral-500 mx-1">on</span>{" "}
                      {a.data_category}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5">
                      {formatDate(a.created_at)} · {a.reason}
                    </div>
                    <div className="text-xs text-neutral-500 mt-0.5 font-mono">
                      Purpose: {a.purpose} · From:{" "}
                      {truncateId(a.requester_agent_id, 10)}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <DecisionBadge decision={a.decision} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Simulator Modal */}
      <Modal
        isOpen={isSimulatorOpen}
        onClose={() => setIsSimulatorOpen(false)}
        title="Policy Simulator"
        subtitle="Simulate how Nexus will evaluate any proposed action against your owner policies"
      >
        <form onSubmit={handleSimulate} className="space-y-4">
          <div>
            <label className="text-xs text-neutral-400 block mb-1">Action</label>
            <input
              type="text"
              value={simAction}
              onChange={(e) => setSimAction(e.target.value)}
              placeholder="e.g. tool:execute"
              className="w-full bg-neutral-950 border border-neutral-800 rounded px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
            />
          </div>

          <div>
            <label className="text-xs text-neutral-400 block mb-1">Resource</label>
            <input
              type="text"
              value={simResource}
              onChange={(e) => setSimResource(e.target.value)}
              placeholder="e.g. tool:calculator"
              className="w-full bg-neutral-950 border border-neutral-800 rounded px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
            />
          </div>

          <div>
            <label className="text-xs text-neutral-400 block mb-1">Peer Agent ID (Optional)</label>
            <input
              type="text"
              value={simPeerId}
              onChange={(e) => setSimPeerId(e.target.value)}
              placeholder="did:nexus:..."
              className="w-full bg-neutral-950 border border-neutral-800 rounded px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
            />
          </div>

          <div className="pt-2 flex justify-end">
            <Button
              type="submit"
              disabled={simulating}
              isLoading={simulating}
            >
              Evaluate Policy
            </Button>
          </div>
        </form>

        {simResult && (
          <div className="mt-6 pt-6 border-t border-neutral-800">
            <h4 className="text-sm font-medium text-neutral-100 mb-4">Evaluation Verdict</h4>
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <span className="text-xs text-neutral-400">Decision:</span>
                <DecisionBadge decision={simResult.decision} />
              </div>
              <div className="p-3 bg-neutral-950 border border-neutral-800 rounded text-xs space-y-1">
                <div className="text-neutral-400">Reason: <span className="text-neutral-100">{simResult.reason}</span></div>
                {simResult.rule_id && (
                  <div className="text-neutral-400">Matched Rule ID: <span className="text-neutral-100">{simResult.rule_id}</span></div>
                )}
              </div>
            </div>
          </div>
        )}
      </Modal>
    </PageShell>
  );
}
