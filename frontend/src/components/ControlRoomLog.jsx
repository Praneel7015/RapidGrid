/*
 * Control-room log: every decision the monitoring agent makes, newest first,
 * across all calls. This is the backend's reasoning made visible.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Panel, PanelHead } from './ui';

const KIND = {
  deviation: 'Deviation',
  blocked_ahead: 'Blocked ahead',
  decision: 'Dispatcher',
  demo: 'Demo',
};

export default function ControlRoomLog() {
  const [entries, setEntries] = useState([]);
  const [resetState, setResetState] = useState(null);
  // Bumped by a reset and by each poll: only the newest poll may write the
  // list, and none that started before a reset finished.
  const generation = useRef(0);

  const resetDemo = async () => {
    if (!window.confirm('Reset the demo? This clears every call, message, closure and divert, and returns all units to their stations. Citizen accounts stay.')) return;
    setResetState('busy');
    try {
      const res = await fetch('/api/monitoring/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
      generation.current += 1;
      setEntries([]);
      setResetState(`Reset: ${data.incidents_cleared} call(s) cleared.`);
    } catch (err) {
      setResetState(`Reset failed: ${err.message}`);
    }
  };

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return; // polls never overlap, so none lands out of order
      inFlight = true;
      const started = generation.current;
      try {
        const res = await fetch('/api/monitoring/log?limit=12', { signal: AbortSignal.timeout(5000) });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled && started === generation.current) setEntries(data.decisions ?? []);
      } catch {
        // keep the last list
      } finally {
        inFlight = false;
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
    <Panel data-tour="control-log">
      <PanelHead
        label={`Control-room log · ${entries.length}`}
        right={
          <button type="button" onClick={resetDemo} disabled={resetState === 'busy'}
            className="t-meta font-bold text-text-muted underline-offset-2 hover:text-critical hover:underline disabled:opacity-50">
            Reset demo
          </button>
        }
      />
      {resetState && resetState !== 'busy' && (
        <p role="status" className="border-b border-rule px-4 py-2 text-[12px] text-text-muted">{resetState}</p>
      )}
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
