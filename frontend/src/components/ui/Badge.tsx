import React from "react";

type BadgeVariant = "default" | "success" | "warning" | "danger" | "info" | "outline";

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
}

export function Badge({ children, variant = "default", className = "" }: BadgeProps) {
  const variants: Record<BadgeVariant, string> = {
    default: "bg-neutral-800 text-neutral-300",
    success: "bg-emerald-950/60 text-emerald-400",
    warning: "bg-amber-950/60 text-amber-400",
    danger: "bg-red-950/60 text-red-400",
    info: "bg-blue-950/60 text-blue-400",
    outline: "bg-transparent text-neutral-400 border border-neutral-700",
  };

  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium ${variants[variant]} ${className}`}>
      {children}
    </span>
  );
}

export function DecisionBadge({ decision }: { decision: string }) {
  const d = decision?.toUpperCase();
  if (d === "ALLOW") return <Badge variant="success">Allow</Badge>;
  if (d === "ASK") return <Badge variant="warning">Ask</Badge>;
  if (d === "DENY") return <Badge variant="danger">Deny</Badge>;
  return <Badge>{decision}</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  const s = status?.toLowerCase();
  if (["completed", "active", "valid", "ready", "ok", "success"].includes(s)) {
    return <Badge variant="success">{status}</Badge>;
  }
  if (["running", "in_progress", "negotiating"].includes(s)) {
    return <Badge variant="info">{status}</Badge>;
  }
  if (["waiting", "waiting_approval", "waiting_remote", "pending"].includes(s)) {
    return <Badge variant="warning">{status}</Badge>;
  }
  if (["failed", "revoked", "error", "expired", "denied", "cancelled"].includes(s)) {
    return <Badge variant="danger">{status}</Badge>;
  }
  return <Badge>{status}</Badge>;
}
