"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge, DecisionBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import {
  listPolicies,
  listConsents,
  evaluatePolicy,
  getPolicyAudit,
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
  const [policies, setPolicies] = useState<PolicyRule[]>([]);
  const [consents, setConsents] = useState<Consent[]>([]);
  const [audits, setAudits] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  // Simulator state
  const [isSimulatorOpen, setIsSimulatorOpen] = useState(false);
  const [simAction, setSimAction] = useState("tool:execute");
  const [simResource, setSimResource] = useState("tool:calculator");
  const [simPeerId, setSimPeerId] = useState("");
  const [simResult, setSimResult] = useState<PolicyEvaluateResponse | null>(null);
  const [simulating, setSimulating] = useState(false);

  const fetchData = async () => {
    try {
      setLoading(true);
      const [polRes, conRes, audRes] = await Promise.allSettled([
        listPolicies(),
        listConsents(),
        getPolicyAudit(),
      ]);

      if (polRes.status === "fulfilled") setPolicies(polRes.value.policies || []);
      if (conRes.status === "fulfilled") setConsents(conRes.value.consents || []);
      if (audRes.status === "fulfilled") setAudits(audRes.value.audits || []);
    } catch (err) {
      console.error("Failed to load policy data", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleSimulate = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      setSimulating(true);
      setSimResult(null);
      const res = await evaluatePolicy({
        action: simAction,
        resource: simResource,
        peer_agent_id: simPeerId || undefined,
      });
      setSimResult(res);
    } catch (err: any) {
      alert(`Policy evaluation failed: ${err.message}`);
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
            onClick={fetchData}
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </Button>
        </div>
      }
    >
      <div className="space-y-12">
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
          ) : policies.length === 0 ? (
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
                      {p.action} <span className="text-neutral-500 mx-1">on</span> {p.resource}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5 font-mono">
                      Rule ID: {truncateId(p.id, 6)} • Type: {p.rule_type || "explicit"}
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
              {consents.map((c) => (
                <div key={c.consent_id} className="py-4 flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-neutral-100">
                      {c.action} <span className="text-neutral-500 mx-1">on</span> {c.resource}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5 font-mono">
                      ID: {truncateId(c.consent_id, 8)} • Expires: {formatDate(c.expires_at)}
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge variant={c.single_use ? "warning" : "info"}>
                      {c.single_use ? "Single-use" : "Reusable"}
                    </Badge>
                  </div>
                </div>
              ))}
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
                      {a.action} <span className="text-neutral-500 mx-1">on</span> {a.resource}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5">
                      {formatDate(a.timestamp)} • Reason: {a.reason}
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
