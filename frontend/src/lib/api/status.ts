/**
 * System status (`/system/status`), which requires a session.
 *
 * Deliberately separate from `/health`, which is public and minimal (M5): the
 * public liveness probe must not disclose which subsystems a deployment has
 * configured, so the detail lives here.
 */

import { apiFetch } from "./client";

export type StatusResponse = {
  status: string;
  version: string;
  environment: string;
  llm_provider: string;
  llm_configured: boolean;
  database: boolean;
  memory: boolean;
  identity: boolean;
  tools: boolean;
  a2a: boolean;
  autonomy: boolean;
  gateway: boolean;
};

export async function getStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>("/system/status");
}
