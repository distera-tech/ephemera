"use client";

import { api } from "@/lib/api";
import { usePolling } from "@/hooks/usePolling";

import { Pill } from "./ui";

export function ModeBadge() {
  const { data, error } = usePolling(api.config, 30_000);
  if (error) return <Pill tone="bad">API offline</Pill>;
  if (!data) return null;
  return data.mode === "simulation" ? (
    <Pill tone="warn" className="ml-2">
      Simulation mode
    </Pill>
  ) : (
    <Pill tone="ok" className="ml-2">
      Real mode · Brev
    </Pill>
  );
}
