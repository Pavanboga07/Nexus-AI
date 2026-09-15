"use client";

import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { PageShell } from "@/components/ui/PageShell";
import {
  getIdentity,
  getAgentCard,
  verifySignature,
  IdentityInfo,
} from "@/lib/api/identity";
import { AgentCard } from "@/types/api";
import { copyToClipboard } from "@/lib/utils";

export default function IdentityPage() {
  const [identity, setIdentity] = useState<IdentityInfo | null>(null);
  const [card, setCard] = useState<AgentCard | null>(null);
  const [loading, setLoading] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const [verifyMsg, setVerifyMsg] = useState("Hello from Nexus");
  const [verifySig, setVerifySig] = useState("");
  const [verifyPubKey, setVerifyPubKey] = useState("");
  const [verifyAgentId, setVerifyAgentId] = useState("");
  const [verifyResult, setVerifyResult] = useState<{
    valid: boolean;
    error?: string;
  } | null>(null);
  const [verifying, setVerifying] = useState(false);

  const fetchIdentityData = async () => {
    try {
      setLoading(true);
      const [idRes, cardRes] = await Promise.allSettled([
        getIdentity(),
        getAgentCard(),
      ]);

      if (idRes.status === "fulfilled") {
        setIdentity(idRes.value);
        setVerifyPubKey(idRes.value.public_key);
        setVerifyAgentId(idRes.value.agent_id);
      }
      if (cardRes.status === "fulfilled") {
        setCard(cardRes.value);
        if (cardRes.value.signature) {
          setVerifySig(cardRes.value.signature);
        }
      }
    } catch (err) {
      console.error("Failed to load identity data", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchIdentityData();
  }, []);

  const handleCopy = async (text: string, keyName: string) => {
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopiedKey(keyName);
      setTimeout(() => setCopiedKey(null), 2000);
    }
  };

  const handleVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      setVerifying(true);
      setVerifyResult(null);
      const res = await verifySignature({
        agent_id: verifyAgentId.trim(),
        message: verifyMsg,
        signature: verifySig.trim(),
        public_key: verifyPubKey.trim(),
      });
      setVerifyResult(res);
    } catch (err: any) {
      setVerifyResult({
        valid: false,
        error: err.message || "Verification failed",
      });
    } finally {
      setVerifying(false);
    }
  };

  return (
    <PageShell
      title="Identity"
      action={
        <Button variant="outline" size="sm" onClick={fetchIdentityData}>
          Refresh
        </Button>
      }
    >
      <div className="space-y-12">
        <section>
          <h2 className="text-sm font-medium text-neutral-100 mb-4">Core Identity</h2>
          {loading ? (
             <div className="text-sm text-neutral-400">Loading identity...</div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              <div className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                  <div className="text-xs text-neutral-500 mb-1">Agent ID (DID)</div>
                  <div className="text-sm font-mono text-neutral-100 break-all">{identity?.agent_id || "..."}</div>
                </div>
                <Button size="sm" variant="ghost" onClick={() => identity && handleCopy(identity.agent_id, "did")}>
                  {copiedKey === "did" ? "Copied" : "Copy"}
                </Button>
              </div>
              <div className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                  <div className="text-xs text-neutral-500 mb-1">Public Key</div>
                  <div className="text-sm font-mono text-neutral-100 break-all">{identity?.public_key || "..."}</div>
                </div>
                <Button size="sm" variant="ghost" onClick={() => identity && handleCopy(identity.public_key, "pub")}>
                  {copiedKey === "pub" ? "Copied" : "Copy"}
                </Button>
              </div>
              <div className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                  <div className="text-xs text-neutral-500 mb-1">Fingerprint</div>
                  <div className="text-sm font-mono text-neutral-100 break-all">{identity?.fingerprint || "..."}</div>
                </div>
                <Button size="sm" variant="ghost" onClick={() => identity && handleCopy(identity.fingerprint, "fp")}>
                  {copiedKey === "fp" ? "Copied" : "Copy"}
                </Button>
              </div>
            </div>
          )}
        </section>

        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-medium text-neutral-100">Agent Card</h2>
            <Button size="sm" variant="ghost" onClick={() => card && handleCopy(JSON.stringify(card, null, 2), "card")}>
              {copiedKey === "card" ? "Copied" : "Copy JSON"}
            </Button>
          </div>
          <pre className="p-4 bg-neutral-900 border border-neutral-800 rounded-lg text-xs font-mono text-neutral-400 overflow-x-auto">
            {card ? JSON.stringify(card, null, 2) : "Loading card..."}
          </pre>
        </section>

        <section>
          <h2 className="text-sm font-medium text-neutral-100 mb-4">Signature Verifier</h2>
          <form onSubmit={handleVerify} className="space-y-4 max-w-2xl">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-neutral-500 block mb-1">Agent ID</label>
                <input
                  type="text"
                  value={verifyAgentId}
                  onChange={(e) => setVerifyAgentId(e.target.value)}
                  className="w-full bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-100 font-mono focus:outline-none focus:border-neutral-700"
                />
              </div>
              <div>
                <label className="text-xs text-neutral-500 block mb-1">Public Key</label>
                <input
                  type="text"
                  value={verifyPubKey}
                  onChange={(e) => setVerifyPubKey(e.target.value)}
                  className="w-full bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-100 font-mono focus:outline-none focus:border-neutral-700"
                />
              </div>
            </div>
            <div>
              <label className="text-xs text-neutral-500 block mb-1">Message</label>
              <textarea
                value={verifyMsg}
                onChange={(e) => setVerifyMsg(e.target.value)}
                rows={2}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-lg p-3 text-sm text-neutral-100 font-mono focus:outline-none focus:border-neutral-700"
              />
            </div>
            <div>
              <label className="text-xs text-neutral-500 block mb-1">Signature (Base64)</label>
              <input
                type="text"
                value={verifySig}
                onChange={(e) => setVerifySig(e.target.value)}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-100 font-mono focus:outline-none focus:border-neutral-700"
              />
            </div>
            <div className="flex items-center gap-4">
              <Button type="submit" disabled={verifying || !verifySig} isLoading={verifying}>
                Verify Signature
              </Button>
              {verifyResult && (
                <div className={`text-sm ${verifyResult.valid ? 'text-emerald-500' : 'text-rose-500'}`}>
                  {verifyResult.valid ? 'Signature Valid' : `Invalid: ${verifyResult.error}`}
                </div>
              )}
            </div>
          </form>
        </section>
      </div>
    </PageShell>
  );
}
