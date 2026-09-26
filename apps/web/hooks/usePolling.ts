"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Poll an async loader. Stops when `enabled` is false; never overlaps requests. */
export function usePolling<T>(loader: () => Promise<T>, intervalMs: number, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loaderRef = useRef(loader);
  const inFlight = useRef(false);

  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setData(await loaderRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    // Initial fetch goes through the same timer path so no state is set synchronously in the effect.
    const first = setTimeout(refresh, 0);
    if (!enabled) return () => clearTimeout(first);
    const id = setInterval(refresh, intervalMs);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [refresh, intervalMs, enabled]);

  return { data, error, refresh };
}
