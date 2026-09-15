import { apiFetch } from "./client";
import { HealthResponse } from "@/types/api";

export async function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}
