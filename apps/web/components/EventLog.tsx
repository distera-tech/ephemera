import type { JobEvent } from "@/lib/api";
import { time } from "@/lib/format";

const levelCls = { info: "text-muted", warning: "text-warn", error: "text-bad" } as const;

export function EventLog({ events }: { events: JobEvent[] }) {
  return (
    <div className="max-h-96 overflow-auto font-mono text-xs" data-testid="event-log">
      <table className="w-full border-collapse">
        <tbody>
          {events.map((e) => (
            <tr key={e.id} className="border-b border-line/50 align-top">
              <td className="py-1 pr-3 whitespace-nowrap text-dim tabular-nums">{time(e.timestamp)}</td>
              <td className={`py-1 pr-3 whitespace-nowrap ${levelCls[e.level] ?? "text-muted"}`}>{e.event_type}</td>
              <td className="py-1 text-fg/90">{e.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
