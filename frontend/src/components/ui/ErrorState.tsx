"use client";

import { AlertCircle } from "lucide-react";

import { Button } from "@/components/ui/Button";

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
  hint?: string;
}

export function ErrorState({ message, onRetry, hint }: ErrorStateProps) {
  return (
    <div
      role="alert"
      className="mb-4 flex items-start gap-2 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-xs text-red-300"
    >
      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <span>{message}</span>
        {hint && <p className="mt-1 text-[11px] text-red-300/70">{hint}</p>}
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={onRetry}
        className="shrink-0 border-red-900/50 text-red-200 hover:bg-red-950/60 hover:text-white"
      >
        Retry
      </Button>
    </div>
  );
}
