/*
 * Control-room log: every decision the monitoring agent makes, newest first,
 * across all calls. This is the backend's reasoning made visible.
 */

import React, { useEffect, useState } from 'react';
import { Panel, PanelHead } from './ui';

const KIND = {
  deviation: 'Deviation',
  blocked_ahead: 'Blocked ahead',
  decision: 'Dispatcher',
  demo: 'Demo',
};

export default function ControlRoomLog() {
  const [entries, setEntries] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch('/api/monitoring/log?limit=12', { signal: AbortSignal.timeout(5000) });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setEntries(data.decisions ?? []);
      } catch {
        // keep the last list
      }
    };
    load();
    const timer = setInterval(load, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <Panel>
      <PanelHead label={`Control-room log · ${entries.length}`} />
      {entries.length === 0 ? (
        <p className="px-4 py-3 text-[12.5px] text-text-muted">No agent decisions yet.</p>
      ) : (
        <ol className="max-h-[260px] divide-y divide-rule overflow-y-auto" aria-live="polite">
          {entries.map((e) => (
            <li key={`${e.at}-${e.kind}-${e.unit_id}`} className="px-4 py-2 text-[12px] leading-snug">
              <div className="flex items-center gap-2 t-meta text-text-faint">
                <span className="font-mono">{new Date(e.at * 1000).toLocaleTimeString()}</span>
                <span className="font-semibold text-text">{KIND[e.kind] ?? e.kind}</span>
                <span className="font-mono">{e.unit_id}</span>
              </div>
              <p className="mt-0.5 text-text">{e.text}</p>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
