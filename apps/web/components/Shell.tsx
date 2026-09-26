import Link from "next/link";
import type { ReactNode } from "react";

import { ModeBadge } from "./ModeBadge";

export function Logo() {
  return (
    <span className="flex items-center gap-2">
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="3" width="18" height="18" rx="3" fill="none" stroke="currentColor" strokeWidth="1.6" />
        <rect x="7.5" y="7.5" width="9" height="9" rx="1" fill="none" stroke="currentColor" strokeWidth="1.4" strokeDasharray="2 2" />
        <circle cx="12" cy="12" r="1.6" fill="currentColor" />
      </svg>
      <span className="font-mono text-sm font-bold tracking-[0.28em]">EPHEMERA</span>
    </span>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <div className="border-b border-warn/20 bg-warn-bg/60 px-4 py-1.5 text-center font-mono text-[11px] tracking-wide text-warn">
        Demo data only — do not upload real confidential information. Hackathon prototype using synthetic/public
        non-sensitive data.
      </div>
      <header className="sticky top-0 z-10 border-b border-line bg-bg/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <Link href="/" className="text-fg hover:text-accent">
            <Logo />
          </Link>
          <nav className="flex items-center gap-1 text-sm">
            <Link href="/dashboard" className="rounded px-3 py-1.5 text-muted hover:bg-panel-2 hover:text-fg">
              Control plane
            </Link>
            <Link href="/architecture" className="rounded px-3 py-1.5 text-muted hover:bg-panel-2 hover:text-fg">
              Architecture
            </Link>
            <ModeBadge />
          </nav>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">{children}</main>
      <footer className="border-t border-line px-4 py-4 text-center text-xs text-dim">
        Ephemera — open-source ephemeral AI inference orchestrator. Application-level data deletion; no claim of
        regulatory compliance or physical-media erasure.
      </footer>
    </div>
  );
}
