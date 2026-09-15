"use client";

import React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

interface PageShellProps {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  maxWidth?: string;
}

export function PageShell({
  title,
  subtitle,
  action,
  children,
  maxWidth = "max-w-4xl",
}: PageShellProps) {
  return (
    <div className="flex-1 flex flex-col h-full overflow-y-auto bg-neutral-950">
      {/* Minimal top bar */}
      <div className="sticky top-0 z-10 bg-neutral-950/80 backdrop-blur-sm border-b border-neutral-800/50">
        <div className={`${maxWidth} mx-auto px-6 py-3 flex items-center justify-between`}>
          <div className="flex items-center gap-3">
            <Link
              href="/"
              className="text-neutral-500 hover:text-neutral-300 transition-colors p-1 -ml-1 rounded hover:bg-neutral-800/50"
              title="Back to chat"
            >
              <ArrowLeft className="w-4 h-4" />
            </Link>
            <div>
              <h1 className="text-sm font-semibold text-neutral-100">{title}</h1>
              {subtitle && (
                <p className="text-[11px] text-neutral-500">{subtitle}</p>
              )}
            </div>
          </div>
          {action && <div>{action}</div>}
        </div>
      </div>

      {/* Page content */}
      <div className={`${maxWidth} mx-auto px-6 py-6 w-full`}>
        {children}
      </div>
    </div>
  );
}
