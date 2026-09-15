import React from "react";

// Minimal card wrapper — used sparingly in secondary pages
interface CardProps {
  children: React.ReactNode;
  className?: string;
  onClick?: () => void;
}

export function Card({ children, className = "", onClick }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={`bg-neutral-900 border border-neutral-800 rounded-lg p-4 ${
        onClick ? "cursor-pointer hover:bg-neutral-800/60" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

// StatCard removed — no longer used in redesign
