/*
 * Route monitoring: the agent's alert for the selected call, and the
 * dispatcher's decision on it.
 *
 * The monitoring agent watches the unit on its planned route. When the unit
 * leaves it, or a closure or accident blocks the road ahead, the agent
 * explains the cause, predicts the delay, proposes routes and says whether
 * to keep the unit or send a backup. Nothing happens until a person decides.
 *
 * Demo controls stage a disruption ahead of the moving unit, so the
 * official scenario can be shown live.
 */

import React, { useState } from 'react';
import { AlertTriangle, CornerUpRight, Truck, X, Route } from 'lucide-react';
import { Button, Panel, PanelHead, Provenance } from './ui';

const TRIGGER = {
  deviation: 'Left the planned route',
  blocked_ahead: 'Route blocked ahead',
};

const DECIDED = {
  rerouted: 'Rerouted',
  backup_dispatched: 'Backup dispatched',
  dismissed: 'Dismissed',
  superseded: 'Expired (the call moved on)',
};

const minutes = (s) => (Number.isFinite(s) ? `${(s / 60).toFixed(1)} min` : '--');

async function post(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      // A stalled request must not leave every control disabled.
      signal: AbortSignal.timeout(20000),
    });
  } catch (err) {
    throw new Error(err?.name === 'TimeoutError' || err?.name === 'AbortError'
      ? 'The server did not answer in time; try again.' : 'Cannot reach the server; try again.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (HTTP ${res.status})`);
  return data;
}

/** What the agent sees this second, in one line (the server's live status). */
function Watching({ live, simSpeed }) {
  if (!live) return null;
  const km = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
  const source = live.fix_source === 'gps' ? 'live GPS' : live.fix_source === 'simulated' ? 'simulated position' : 'station';
  const age = Number.isFinite(live.fix_age_s) ? `, ${Math.round(live.fix_age_s)} s old` : '';
  const road = Number.isFinite(live.remaining_s) ? `${(live.remaining_s / 60).toFixed(1)} min road time` : 'time unknown';
  return (
    <dl data-testid="agent-watching" className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-sm border border-rule bg-paper-sunk px-3 py-2 text-[12px]">
      <dt className="text-text-muted">Leg</dt>
      <dd className="font-medium">{live.leg === 'to_scene' ? 'To the patient' : 'To the hospital'}, {Math.round(live.progress * 100)}% done</dd>
      <dt className="text-text-muted">Still to go</dt>
      <dd className="font-medium">{km(live.remaining_m)}, {road}{simSpeed && live.fix_source === 'simulated' ? ` (shown ×${simSpeed} faster)` : ''}</dd>
      <dt className="text-text-muted">Off the route</dt>
      <dd className="font-medium">{km(live.off_route_m)}</dd>
      <dt className="text-text-muted">Position</dt>
      <dd className="font-medium">{source}{age}</dd>
    </dl>
  );
}

export default function MonitoringPanel({ incident, onChanged, live = null, simSpeed = null }) {
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  if (!incident) return null;
  const alerts = incident.monitoring?.alerts ?? [];
  const open = alerts.find((a) => a.status === 'open');
  const decided = alerts.filter((a) => a.status !== 'open').slice(0, 3);
  const moving = ['dispatched', 'patient_picked_up'].includes(incident.status);
  const base = `/api/incidents/${encodeURIComponent(incident.incident_id)}`;

  const act = async (key, url, body, done) => {
    if (busy) return;
    setBusy(key);
    setError(null);
    setMessage(null);
    try {
      const data = await post(url, body);
      setMessage(done(data));
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel>
      <PanelHead
        label="Route monitoring"
        right={<Provenance kind="modelled" title="Delays and ETAs come from the modelled city, not a live traffic feed." />}
      />
      <div className="space-y-3 px-4 py-3.5 text-[13px] leading-relaxed text-text">
        {!moving && !open && (
          <p className="text-text-muted">Monitoring starts when a unit is dispatched.</p>
        )}
        {moving && !open && (
          <>
            <p className="text-text-muted">
              The agent is watching the unit on its planned route. No deviation or blockage.
            </p>
            <Watching live={live} simSpeed={simSpeed} />
          </>
        )}

        {open && (
          <div role="alert" className="space-y-3 rounded-sm border border-critical bg-critical-wash p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle size={16} className="mt-0.5 shrink-0 text-critical" aria-hidden="true" />
              <div className="min-w-0">
                <div className="font-semibold text-critical">{TRIGGER[open.trigger] ?? open.trigger}</div>
                <p className="mt-1 text-text">{open.explanation}</p>
              </div>
            </div>
            <dl className="grid grid-cols-3 gap-2 text-[12px]">
              <div>
                <dt className="t-meta text-text-faint">Delay</dt>
                <dd className="font-mono font-semibold">
                  {Number.isFinite(open.delay_s) ? `${open.delay_s >= 0 ? '+' : ''}${minutes(open.delay_s)}` : '--'}
                </dd>
              </div>
              <div>
                <dt className="t-meta text-text-faint">New ETA</dt>
                <dd className="font-mono font-semibold">{minutes(open.new_eta_s)}</dd>
              </div>
              <div>
                <dt className="t-meta text-text-faint">Cause</dt>
                <dd className="font-semibold capitalize">
                  {open.cause?.kind === 'unexplained' ? 'Unknown' : open.cause?.kind}
                  {open.cause?.likely ? ' (likely)' : ''}
                </dd>
              </div>
            </dl>

            <div className="space-y-1.5">
              {open.routes.map((r, i) => (
                <Button
                  key={r.label}
                  variant={i === 0 && open.recommendation === 'reroute' ? 'primary' : 'quiet'}
                  size="sm"
                  className="w-full !justify-between"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    act(`route-${i}`, `${base}/alerts/${open.alert_id}/accept`, { route_index: i },
                      () => `${r.label} sent to ${open.unit_id}.`)
                  }
                >
                  <span className="inline-flex items-center gap-1.5">
                    <Route size={13} /> {i === 0 ? 'Accept recommended route' : `Use ${r.label.toLowerCase()}`}
                  </span>
                  <span className="font-mono">{minutes(r.eta_s)}</span>
                </Button>
              ))}
              {open.backup && open.leg === 'to_scene' && (
                <Button
                  variant={open.recommendation === 'dispatch_backup' ? 'primary' : 'quiet'}
                  size="sm"
                  className="w-full !justify-between"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    act('backup', `${base}/alerts/${open.alert_id}/backup`, null,
                      (d) => `${d.unit_id} dispatched; ${open.unit_id} released.`)
                  }
                >
                  <span className="inline-flex items-center gap-1.5">
                    <Truck size={13} /> Send {open.backup.unit_id}
                  </span>
                  <span className="font-mono">{minutes(open.backup.eta_s)}</span>
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                className="w-full"
                disabled={Boolean(busy)}
                onClick={() => act('dismiss', `${base}/alerts/${open.alert_id}/dismiss`, null, () => 'Alert dismissed.')}
              >
                <X size={13} /> Dismiss
              </Button>
            </div>
          </div>
        )}

        {incident.pickup_ranking && (
          <div className="rounded-sm border border-rule bg-paper-sunk p-3 text-[12.5px]">
            <div className="t-meta mb-1 font-bold uppercase tracking-[0.1em] text-text-faint">Hospitals from the scene, at pickup</div>
            <p className="text-text">{incident.pickup_ranking.summary}</p>
            <ol className="mt-1.5 space-y-0.5 text-text-muted">
              {incident.pickup_ranking.hospitals.slice(0, 3).map((h) => (
                <li key={h.hospital_id}>
                  {h.name}{Number.isFinite(h.road_eta_min) ? ` · ${h.road_eta_min} min` : ''}{h.current ? ' · current' : ''}
                </li>
              ))}
            </ol>
          </div>
        )}

        {decided.length > 0 && (
          <ul className="space-y-1 text-[12px] text-text-muted">
            {decided.map((a) => (
              <li key={a.alert_id}>
                <span className="font-semibold text-text">{DECIDED[a.status] ?? a.status}</span>
                {' · '}
                {TRIGGER[a.trigger] ?? a.trigger}, {a.cause?.kind}
              </li>
            ))}
          </ul>
        )}

        {moving && (
          <div className="border-t border-rule pt-3">
            <div className="t-meta mb-1.5 text-text-faint">Demo: stage a disruption ahead of the unit</div>
            <div className="flex flex-wrap gap-1.5">
              {[
                ['close_road_ahead', 'Close road ahead'],
                ['accident_ahead', 'Accident ahead'],
                ['wrong_turn', 'Wrong turn'],
              ].map(([action, label]) => (
                <Button
                  key={action}
                  variant="quiet"
                  size="sm"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    act(action, `/api/monitoring/demo/${encodeURIComponent(incident.incident_id)}`, { action },
                      (d) => (d.road ? `${label}: ${d.road}.` : `${label}: staged.`))
                  }
                >
                  <CornerUpRight size={13} /> {label}
                </Button>
              ))}
            </div>
          </div>
        )}

        {message && <p role="status" className="text-[12px] text-text-muted">{message}</p>}
        {error && <p role="alert" className="text-[12px] text-critical">{error}</p>}
      </div>
    </Panel>
  );
}
