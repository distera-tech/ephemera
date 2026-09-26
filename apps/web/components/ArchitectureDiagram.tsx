/** Static architecture diagram (inline SVG, no external assets). */
export function ArchitectureDiagram() {
  const box = "fill-[var(--color-panel-2)] stroke-[var(--color-line-strong)]";
  const txt = "fill-[var(--color-fg)] font-mono text-[12px]";
  const sub = "fill-[var(--color-muted)] font-mono text-[10px]";
  return (
    <svg viewBox="0 0 860 330" className="h-auto w-full" role="img" aria-label="Ephemera architecture diagram">
      <defs>
        <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" className="fill-[var(--color-muted)]" />
        </marker>
      </defs>
      {/* Control plane */}
      <rect x="10" y="10" width="520" height="310" rx="10" className="fill-none stroke-[var(--color-line)]" strokeDasharray="4 4" />
      <text x="24" y="32" className={sub}>CONTROL PLANE (always on, no GPU)</text>

      <rect x="30" y="50" width="130" height="56" rx="6" className={box} />
      <text x="95" y="75" textAnchor="middle" className={txt}>Browser</text>
      <text x="95" y="92" textAnchor="middle" className={sub}>Next.js UI</text>

      <rect x="200" y="50" width="130" height="56" rx="6" className={box} />
      <text x="265" y="75" textAnchor="middle" className={txt}>API</text>
      <text x="265" y="92" textAnchor="middle" className={sub}>FastAPI · 202</text>

      <rect x="370" y="50" width="140" height="56" rx="6" className={box} />
      <text x="440" y="75" textAnchor="middle" className={txt}>PostgreSQL</text>
      <text x="440" y="92" textAnchor="middle" className={sub}>jobs · events</text>

      <rect x="200" y="160" width="310" height="70" rx="6" className="fill-[var(--color-panel-2)] stroke-[var(--color-accent)]" />
      <text x="355" y="186" textAnchor="middle" className={txt}>Ephemera Worker</text>
      <text x="355" y="203" textAnchor="middle" className={sub}>state machine · finally: destroy + verify</text>
      <text x="355" y="218" textAnchor="middle" className={sub}>reconciliation · limits · timeouts</text>

      <rect x="30" y="250" width="150" height="56" rx="6" className={box} />
      <text x="105" y="275" textAnchor="middle" className={txt}>Job temp dir</text>
      <text x="105" y="292" textAnchor="middle" className={sub}>deleted on every exit</text>

      <path d="M160 78 H198" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />
      <path d="M330 78 H368" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />
      <path d="M440 106 V158" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />
      <path d="M200 205 H105 V248" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />

      {/* Ephemeral compute */}
      <rect x="570" y="10" width="280" height="310" rx="10" className="fill-none stroke-[var(--color-warn)]" strokeDasharray="6 4" />
      <text x="584" y="32" className="fill-[var(--color-warn)] font-mono text-[10px]">EPHEMERAL GPU (exists only per job)</text>

      <rect x="590" y="60" width="240" height="56" rx="6" className={box} />
      <text x="710" y="84" textAnchor="middle" className={txt}>NVIDIA Brev instance</text>
      <text x="710" y="101" textAnchor="middle" className={sub}>L40S preferred · ephemera-&lt;id&gt;</text>

      <rect x="590" y="150" width="240" height="70" rx="6" className={box} />
      <text x="710" y="176" textAnchor="middle" className={txt}>vLLM (container)</text>
      <text x="710" y="193" textAnchor="middle" className={sub}>open-weight 8B model</text>
      <text x="710" y="208" textAnchor="middle" className={sub}>bound to 127.0.0.1:8000 only</text>

      <rect x="590" y="250" width="240" height="50" rx="6" className={box} />
      <text x="710" y="272" textAnchor="middle" className={txt}>/tmp/ephemera/jobs/&lt;id&gt;</text>
      <text x="710" y="288" textAnchor="middle" className={sub}>removed, then instance destroyed</text>

      <path d="M510 185 H588" className="stroke-[var(--color-warn)]" markerEnd="url(#arr)" />
      <text x="549" y="176" textAnchor="middle" className={sub}>brev</text>
      <text x="549" y="202" textAnchor="middle" className={sub}>SSH</text>
      <path d="M710 116 V148" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />
      <path d="M710 220 V248" className="stroke-[var(--color-muted)]" markerEnd="url(#arr)" />
    </svg>
  );
}
