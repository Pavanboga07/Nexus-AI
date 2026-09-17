import { useCallback, useEffect, useRef, useState } from "react";

interface UseAsyncResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Run an async fetcher with first-paint-safe defaults.
 *
 * `loading` starts `true` so pages never flash a false empty state
 * ("No entries found") before the first response arrives. The fetcher
 * is read through a ref so callers may pass an inline closure without
 * retriggering the load on every render.
 */
export function useAsync<T>(fetcher: () => Promise<T>): UseAsyncResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    void fetcherRef
      .current()
      .then((result) => setData(result))
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Something went wrong.")
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return { data, error, loading, reload };
}
