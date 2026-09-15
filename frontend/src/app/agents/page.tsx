"use client";

import React, { useEffect, useState } from "react";
import { Badge, StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { PageShell } from "@/components/ui/PageShell";
import {
  listTrustedAgents,
  registerTrustedAgent,
  revokeTrustedAgent,
  discoverRemoteAgent,
} from "@/lib/api/a2a";
import { AgentCard, TrustedAgent } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";

export default function AgentsPage() {
  const [trustedAgents, setTrustedAgents] = useState<TrustedAgent[]>([]);
  const [loading, setLoading] = useState(false);

  // Discover modal & state
  const [discoverModalOpen, setDiscoverModalOpen] = useState(false);
  const [endpointUrl, setEndpointUrl] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [discoveredCard, setDiscoveredCard] = useState<AgentCard | null>(null);
  const [discoveredVerified, setDiscoveredVerified] = useState<boolean | null>(null);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);

  const fetchTrustedAgents = async () => {
    try {
      setLoading(true);
      const res = await listTrustedAgents();
      setTrustedAgents(res.agents || []);
    } catch (err) {
      console.error("Failed to load trusted agents", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTrustedAgents();
  }, []);

  const handleRevoke = async (agentId: string) => {
    if (!confirm(`Revoke trust for agent ${agentId}?`)) return;
    try {
      await revokeTrustedAgent(agentId);
      await fetchTrustedAgents();
    } catch (err: any) {
      alert(`Failed to revoke agent: ${err.message}`);
    }
  };

  const handleDiscover = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!endpointUrl.trim()) return;

    try {
      setDiscovering(true);
      setDiscoverError(null);
      setDiscoveredCard(null);
      setDiscoveredVerified(null);

      const res = await discoverRemoteAgent(endpointUrl.trim());
      setDiscoveredCard(res.card);
      setDiscoveredVerified(res.verified);
      await fetchTrustedAgents();
    } catch (err: any) {
      setDiscoverError(err.message || "Failed to discover agent at specified endpoint.");
    } finally {
      setDiscovering(false);
    }
  };

  const handleTrustDiscovered = async () => {
    if (!discoveredCard) return;
    try {
      setRegistering(true);
      await registerTrustedAgent({
        agent_id: discoveredCard.agent_id,
        display_name: discoveredCard.name,
        public_key: discoveredCard.public_key,
        endpoint: discoveredCard.endpoints.a2a || endpointUrl,
      });
      await fetchTrustedAgents();
      setDiscoverModalOpen(false);
      setDiscoveredCard(null);
      setEndpointUrl("");
    } catch (err: any) {
      alert(`Failed to register agent: ${err.message}`);
    } finally {
      setRegistering(false);
    }
  };

  return (
    <PageShell
      title="Agents"
      action={
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchTrustedAgents}>
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setDiscoverModalOpen(true);
              setDiscoverError(null);
              setDiscoveredCard(null);
              setEndpointUrl("");
            }}
          >
            Discover Peer
          </Button>
        </div>
      }
    >
      <div className="space-y-6">
        {loading ? (
          <div className="py-16 text-center">
            <p className="text-sm text-neutral-400">Loading trusted agents...</p>
          </div>
        ) : trustedAgents.length === 0 ? (
          <div className="py-16 text-center">
            <p className="text-sm text-neutral-400">No trusted agents found.</p>
            <p className="text-xs text-neutral-500 mt-1">
              Click Discover Peer to connect with other agents.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-neutral-800 border-y border-neutral-800">
            {trustedAgents.map((agent) => (
              <div key={agent.agent_id} className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-neutral-100">
                      {agent.display_name || "Nexus Peer"}
                    </span>
                    <StatusBadge status={agent.status} />
                  </div>
                  <div className="text-xs text-neutral-400 mt-1 font-mono">
                    {truncateId(agent.agent_id, 12)}
                  </div>
                  {agent.capabilities && agent.capabilities.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {agent.capabilities.map((cap) => (
                        <span key={cap} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-800 text-neutral-400">
                          {cap}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-4 shrink-0">
                  <div className="text-right hidden md:block">
                    <div className="text-xs text-neutral-400">Trusted since</div>
                    <div className="text-xs text-neutral-500">{formatDate(agent.created_at)}</div>
                  </div>
                  {agent.status === "active" ? (
                    <Button variant="ghost" size="sm" onClick={() => handleRevoke(agent.agent_id)}>
                      Revoke
                    </Button>
                  ) : (
                    <Badge variant="danger">Revoked</Badge>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <Modal
        isOpen={discoverModalOpen}
        onClose={() => setDiscoverModalOpen(false)}
        title="Discover Agent"
      >
        <form onSubmit={handleDiscover} className="space-y-4">
          <div>
            <label className="text-sm font-medium text-neutral-100 block mb-1.5">
              Remote Endpoint URL
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={endpointUrl}
                onChange={(e) => setEndpointUrl(e.target.value)}
                placeholder="http://peer.example.com/.well-known/nexus-agent.json"
                className="flex-1 bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-neutral-700 font-mono"
              />
              <Button type="submit" disabled={discovering || !endpointUrl.trim()} isLoading={discovering}>
                Probe
              </Button>
            </div>
          </div>

          {discoverError && (
            <div className="p-3 rounded-lg bg-rose-950/30 border border-rose-900 text-sm text-rose-400">
              {discoverError}
            </div>
          )}

          {discoveredCard && (
            <div className="p-4 rounded-lg bg-neutral-900 border border-neutral-800 space-y-4">
              <div className="flex items-start justify-between">
                <div>
                  <h4 className="text-sm font-medium text-neutral-100">{discoveredCard.name}</h4>
                  <p className="text-xs text-neutral-500 font-mono mt-1">{discoveredCard.agent_id}</p>
                </div>
                <Badge variant={discoveredVerified ? "success" : "danger"}>
                  {discoveredVerified ? "Verified" : "Invalid"}
                </Badge>
              </div>
              
              <div className="text-sm text-neutral-400">
                {discoveredCard.description || "No description provided."}
              </div>

              {discoveredCard.capabilities && (
                <div className="flex flex-wrap gap-1.5">
                  {discoveredCard.capabilities.map((cap) => (
                    <span key={cap} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-300">
                      {cap}
                    </span>
                  ))}
                </div>
              )}

              <div className="pt-4 border-t border-neutral-800 flex justify-end gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => setDiscoveredCard(null)}>
                  Cancel
                </Button>
                <Button
                  type="button"
                  size="sm"
                  onClick={handleTrustDiscovered}
                  isLoading={registering}
                  disabled={!discoveredVerified}
                >
                  Trust & Register
                </Button>
              </div>
            </div>
          )}
        </form>
      </Modal>
    </PageShell>
  );
}
