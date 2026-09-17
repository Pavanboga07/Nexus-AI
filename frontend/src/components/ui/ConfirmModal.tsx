"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  requireReason?: boolean;
  onConfirm: (reason?: string) => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  body,
  confirmLabel,
  danger = false,
  requireReason = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (open) setReason("");
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = "unset";
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, onCancel]);

  if (!open) return null;

  const reasonMissing = requireReason && reason.trim().length === 0;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onCancel}
      />
      <div className="relative z-10 w-full max-w-md rounded-lg border border-neutral-800 bg-neutral-900 p-5 shadow-xl">
        <h3 className="text-base font-semibold text-neutral-100">{title}</h3>
        <p className="mt-2 text-sm leading-relaxed text-neutral-400">{body}</p>
        {requireReason && (
          <label className="mt-3 block">
            <span className="mb-1 block text-xs text-neutral-400">Reason</span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              autoFocus
              placeholder="Tell them why…"
              className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-2.5 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-neutral-500 focus:outline-none"
            />
          </label>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button size="sm" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            variant={danger ? "danger" : "primary"}
            disabled={reasonMissing}
            onClick={() =>
              onConfirm(requireReason ? reason.trim() : undefined)
            }
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
