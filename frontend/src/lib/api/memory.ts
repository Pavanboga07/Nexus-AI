import { apiFetch } from "./client";
import { Memory, MemorySearchResult } from "@/types/api";

export async function listMemories(
  memoryType?: string,
  limit: number = 100
): Promise<{ memories: Memory[]; total: number }> {
  const query = new URLSearchParams();
  if (memoryType) query.append("memory_type", memoryType);
  if (limit) query.append("limit", limit.toString());

  const qs = query.toString();
  return apiFetch<{ memories: Memory[]; total: number }>(
    `/memories${qs ? `?${qs}` : ""}`
  );
}

export async function searchMemories(
  query: string,
  limit: number = 10,
  memoryTypes?: string[]
): Promise<{ results: MemorySearchResult[]; total: number }> {
  return apiFetch<{ results: MemorySearchResult[]; total: number }>(
    "/memories/search",
    {
      method: "POST",
      body: JSON.stringify({
        query,
        limit,
        memory_types: memoryTypes,
      }),
    }
  );
}

export async function deleteMemory(
  memoryId: string
): Promise<{ deleted: boolean; id: string }> {
  return apiFetch<{ deleted: boolean; id: string }>(`/memories/${memoryId}`, {
    method: "DELETE",
  });
}
