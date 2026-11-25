/*
 * City control room: the whole of Bengaluru at once, for the projector or a
 * fifth laptop. Every hospital, station, vehicle and live emergency on one
 * map, the calls in progress, and what the monitoring agent is deciding.
 * Read-only, apart from making the city busy for a demo.
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Activity, Building2, Siren, Truck } from 'lucide-react';
import MapOverlay from '../components/MapOverlay';
import ControlRoomLog from '../components/ControlRoomLog';
import { Panel, PanelHead, StatusTag } from '../components/ui';
import { useFleet, useHospitals, useStations, useOsmHospitals } from '../hooks/useCityLayers';
import shortId from '../utils/shortId';

const ACTIVE = new Set(['processing', 'awaiting_dispatcher_approval', 'dispatched', 'patient_picked_up']);

function useIncidents() {
  const [incidents, setIncidents] = useState([]);
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const res = await fetch('/api/incidents/', { signal: AbortSignal.timeout(8000) });
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) setIncidents(Array.isArray(data) ? data : (data.incidents ?? []));
        }
      } catch {
        // keep the last list
      } finally {
        inFlight = false;
      }
    };
    load();
    const timer = setInterval(load, 3000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);
  return incidents;
}

/** "Busy city": other calls already under way, and a background caller. */
export function CityLife() {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const load = async () => {
    try {
      const res = await fetch('/api/monitoring/city-life');
      if (res.ok) setState(await res.json());
    } catch { /* keep the last */ }
  };
  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);
  const change = async (body) => {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch('/api/monitoring/city-life', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
      setState(data);
      if (data.started) setMessage(`${data.started} call(s) started across the city.`);
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel data-testid="city-life">
      <PanelHead label="Simulated city" />
      <div className="space-y-2 px-4 py-3 text-[13px]">
        <p className="text-text-muted">
          Other emergencies elsewhere in the city, on units the stage call does not use. Labelled as simulated everywhere.
        </p>
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={busy} onClick={() => change({ seed: 4 })}
            className="tap rounded-sm border border-ink bg-signal px-3 py-2 font-semibold disabled:opacity-50">
            Start a busy city
          </button>
          <button type="button" disabled={busy || !state}
            onClick={() => change({ background: !state?.background_caller })}
            aria-pressed={!!state?.background_caller}
            className="tap rounded-sm border border-rule px-3 py-2 font-semibold disabled:opacity-50">
            {state?.background_caller ? 'Stop new calls' : 'New call every 2 min'}
          </button>
        </div>
        {state && (
          <p className="t-meta text-text-muted">
            {state.active_calls} simulated call(s) running
            {state.background_caller && state.next_call_in_s != null ? ` · next in ${state.next_call_in_s} s` : ''}
          </p>
        )}
        {message && <p role="status" className="text-[12.5px]">{message}</p>}
      </div>
    </Panel>
  );
}

function Count({ Icon, label, value, tone = '' }) {
  return (
    <div className="flex items-center gap-2">
      <Icon size={18} className="text-signal" aria-hidden />
      <span className={`font-display text-[22px] font-extrabold leading-none ${tone}`}>{value}</span>
      <span className="t-meta uppercase tracking-[0.08em] text-on-ink-muted">{label}</span>
    </div>
  );
}

export default function ControlRoom() {
  const incidents = useIncidents();
  const fleet = useFleet();
  const hospitals = useHospitals();
  const osmHospitals = useOsmHospitals();
  const stations = useStations();
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  const active = useMemo(() => incidents.filter((i) => ACTIVE.has(i.status)), [incidents]);
  const calls = useMemo(() => active.map((i) => ({
    incident_id: i.incident_id, short_id: shortId(i.incident_id), location: i.location, status: i.status,
    emergency_type: i.emergency_type, assigned_unit: i.assigned_unit, background: i.background,
  })), [active]);
  const onCall = fleet.filter((u) => u.status !== 'available').length;
  const offDuty = fleet.filter((u) => u.on_duty === false).length;
  const free = fleet.length - onCall - offDuty;
  const diverted = hospitals.filter((h) => h.on_divert).length;

  return (
    <div className="min-h-screen bg-paper">
      <header className="flex flex-wrap items-center justify-between gap-4 bg-ink px-5 py-3 text-on-ink">
        <div>
          <h1 className="font-display text-[20px] font-extrabold tracking-wide text-on-ink">City control room</h1>
          <p className="t-meta uppercase tracking-[0.12em] text-on-ink-muted">
            Bengaluru · {clock.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit' })} IST
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2" data-testid="city-counts">
          <Count Icon={Siren} label="Live emergencies" value={active.length} />
          <Count Icon={Truck} label="Vehicles on calls" value={onCall} />
          <Count Icon={Truck} label="Free" value={free} />
          {offDuty > 0 && <Count Icon={Truck} label="Off duty" value={offDuty} />}
          <Count Icon={Building2} label={`Hospitals${diverted ? `, ${diverted} on divert` : ''}`} value={hospitals.length} />
        </div>
      </header>

      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
        <Panel className="overflow-hidden">
          <MapOverlay
            height="calc(100vh - 120px)"
            fleet={fleet}
            hospitals={hospitals}
            osmHospitals={osmHospitals}
            stations={stations}
            calls={calls}
          />
        </Panel>
        <div className="space-y-4 lg:max-h-[calc(100vh-120px)] lg:overflow-y-auto">
          <CityLife />
          <Panel>
            <PanelHead label={`Emergencies now · ${active.length}`} right={<Activity size={14} aria-hidden />} />
            {active.length === 0 ? (
              <p className="px-4 py-3 text-[13px] text-text-muted">No emergencies right now.</p>
            ) : (
              <ul className="divide-y divide-rule" data-testid="control-calls">
                {active.map((i) => (
                  <li key={i.incident_id} className="flex items-center justify-between gap-2 px-4 py-2.5">
                    <div className="min-w-0">
                      <div className="font-mono text-[12px] font-bold" title={i.incident_id}>
                        {shortId(i.incident_id)}
                        {i.background && <span className="ml-2 font-sans text-[11px] font-normal text-text-muted">simulated</span>}
                      </div>
                      <div className="truncate text-[13px] capitalize">
                        {i.emergency_type ?? 'Emergency'}{i.assigned_unit ? ` · ${i.assigned_unit}` : ''}
                        {i.citizen_view?.hospital_name ? ` → ${i.citizen_view.hospital_name}` : ''}
                      </div>
                    </div>
                    <StatusTag status={i.status} />
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <ControlRoomLog />
        </div>
      </div>
    </div>
  );
}
