"use client";

import React from "react";
import Link from "next/link";
import { ArrowLeft, Brain, Users, CheckSquare, GitBranch, Wrench, Shield, Key, Activity, Sliders } from "lucide-react";

const categories = [
  { name: "Autonomy & Decisions", href: "/autonomy", icon: Sliders, description: "Modes, limits, and human-in-the-loop approvals" },
  { name: "Workflows", href: "/workflows", icon: GitBranch, description: "Multi-step coordination" },
  { name: "Tasks", href: "/tasks", icon: CheckSquare, description: "Delegated work and negotiations" },
  { name: "Memory", href: "/memory", icon: Brain, description: "Stored knowledge and preferences" },
  { name: "Agents", href: "/agents", icon: Users, description: "Trusted peers and discovery" },
  { name: "Tools", href: "/tools", icon: Wrench, description: "Available capabilities" },
  { name: "Permissions", href: "/permissions", icon: Shield, description: "Access control and policies" },
  { name: "Identity", href: "/identity", icon: Key, description: "Cryptographic identity" },
  { name: "Activity", href: "/activity", icon: Activity, description: "Audit log" },
];

export default function SettingsPage() {
  return (
    <div className="flex-1 flex flex-col h-full overflow-y-auto bg-neutral-950">
      <div className="max-w-2xl mx-auto px-6 py-8 w-full">
        {/* Header */}
        <div className="flex items-center gap-3 mb-8">
          <Link
            href="/"
            className="text-neutral-500 hover:text-neutral-300 transition-colors p-1 -ml-1 rounded hover:bg-neutral-800/50"
          >
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <h1 className="text-lg font-semibold text-neutral-100">Settings</h1>
        </div>

        {/* Categories */}
        <div className="divide-y divide-neutral-800/60">
          {categories.map((cat) => {
            const Icon = cat.icon;
            return (
              <Link
                key={cat.href}
                href={cat.href}
                className="flex items-center gap-4 py-4 px-2 -mx-2 rounded-md hover:bg-neutral-900/60 transition-colors group"
              >
                <div className="w-8 h-8 flex items-center justify-center rounded-md bg-neutral-800/60 text-neutral-400 group-hover:text-neutral-200 transition-colors">
                  <Icon className="w-4 h-4" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-neutral-200 group-hover:text-neutral-100">
                    {cat.name}
                  </div>
                  <div className="text-xs text-neutral-500">
                    {cat.description}
                  </div>
                </div>
                <svg className="w-4 h-4 text-neutral-600 group-hover:text-neutral-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5l7 7-7 7" />
                </svg>
              </Link>
            );
          })}
        </div>

        {/* Version info */}
        <div className="mt-8 pt-4 border-t border-neutral-800/40 text-xs text-neutral-600">
          Nexus v0.10.0
        </div>
      </div>
    </div>
  );
}
