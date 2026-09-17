import { useCallback, useEffect, useState } from "react";

import { listTrusted } from "./api/people";
import { truncateId } from "./utils";

/**
 * Contact names: never show an ID where a name exists.
 *
 * The trusted-agents list is loaded once per session (module-level promise
 * cache) and shared by every page through this hook. Unknown IDs fall back to
 * a short fingerprint-style slice (`abcd1234…wxyz1234`) — never a raw
 * 24-character slice, which is unreadable and looks like a redacted secret.
 */
let cachedNames: Promise<Map<string, string>> | null = null;

function loadNamesOnce(): Promise<Map<string, string>> {
  if (!cachedNames) {
    cachedNames = listTrusted()
      .then(
        (agents) =>
          new Map(
            (agents ?? []).map((a) => [a.agent_id, a.display_name] as const)
          )
      )
      // A names failure must not break the page that wanted a label: every
      // caller falls back to the short-ID form below.
      .catch(() => new Map<string, string>());
  }
  return cachedNames;
}

/** Forget the cached map so the next hook mount refetches (after connect/remove). */
export function invalidateContactNames(): void {
  cachedNames = null;
}

/**
 * The shared names map as a promise, for non-component code (e.g. a fetcher
 * that bakes display strings). Components should prefer {@link useContactNames}.
 */
export function loadContactNames(): Promise<Map<string, string>> {
  return loadNamesOnce();
}

/** Short, obviously-truncated fallback for an ID with no known name. */
export function shortContactId(id?: string | null): string {
  if (!id) return "a remote agent";
  return truncateId(id, 8);
}

/** Resolve an agent ID to its display name, falling back to {@link shortContactId}. */
export function resolveContactName(
  names: Map<string, string>,
  id?: string | null
): string {
  if (!id) return "a remote agent";
  return names.get(id) ?? shortContactId(id);
}

export function useContactNames(): {
  names: Map<string, string>;
  resolve: (id?: string | null) => string;
  loading: boolean;
  reload: () => void;
} {
  const [names, setNames] = useState<Map<string, string>>(new Map());
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    setLoading(true);
    void loadNamesOnce().then((map) => {
      setNames(new Map(map));
      setLoading(false);
    });
  }, []);

  useEffect(() => {
    let live = true;
    void loadNamesOnce().then((map) => {
      if (!live) return;
      setNames(new Map(map));
      setLoading(false);
    });
    return () => {
      live = false;
    };
  }, []);

  const resolve = useCallback(
    (id?: string | null) => resolveContactName(names, id),
    [names]
  );

  return { names, resolve, loading, reload };
}
