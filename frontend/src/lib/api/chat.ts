import { apiFetch } from "./client";
import { Session, ChatResponse } from "@/types/api";

export async function listSessions(): Promise<string[]> {
  return apiFetch<string[]>("/sessions");
}

export async function createSession(): Promise<{ session_id: string }> {
  return apiFetch<{ session_id: string }>("/sessions", {
    method: "POST",
  });
}

export async function getSession(sessionId: string): Promise<Session> {
  return apiFetch<Session>(`/sessions/${sessionId}`);
}

export async function clearSession(sessionId: string): Promise<Session> {
  return apiFetch<Session>(`/sessions/${sessionId}`, {
    method: "DELETE",
  });
}

export async function sendChatMessage(
  sessionId: string,
  message: string
): Promise<ChatResponse> {
  return apiFetch<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      message,
    }),
  });
}
