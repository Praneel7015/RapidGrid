/**
 * Responder field app.
 *
 * Read from a cradle in a moving vehicle: one primary action per stage, large
 * targets, and the next instruction always above the fold. Stage 1 runs the
 * unit to the patient; stage 2 runs the patient to the receiving ER, and the
 * destination can change mid-transport if the ER diverts.
 */

import React, { useState, useEffect } from 'react';
import { Check, ChevronLeft, Building2 } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import MapOverlay from '../components/MapOverlay';
import LiveEmergencyChat from '../components/LiveEmergencyChat';
import {
  Panel,
  PanelHead,
  Button,
  Stat,
  SeverityTag,
  StatusTag,
  ConnectionState,
  Empty,
  fmt,
} from '../components/ui';

export default function DriverDashboard() {
  const { driverInfo } = useAuth();
  const [dispatches, setDispatches] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [stage, setStage] = useState('pickup'); // pickup | transport
  const [conn, setConn] = useState('ok');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch('/api/driver/active');
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (!cancelled) {
          setDispatches(data.dispatches ?? data.active ?? []);
          setConn('ok');
        }
      } catch {
        if (!cancelled) setConn('down');
      }
    };
    load();
    const timer = setInterval(load, 2500);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const active = dispatches.find((d) => d.incident_id === activeId) ?? null;
  const view = active?.citizen_view ?? {};

  const patch = (data) => {
    if (!data?.citizen_view) return;
    setDispatches((prev) =>
      prev.map((d) =>
        d.incident_id === active.incident_id ? { ...d, citizen_view: data.citizen_view } : d,
      ),
    );
  };

  const arriveAtPatient = async () => {
    setBusy(true);
    try {
      const res = await fetch('/api/driver/arrived_pickup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          incident_id: active.incident_id,
          driver_id: driverInfo?.unitId ?? 'drv-11',
        }),
      });
      patch(await res.json());
      setStage('transport');
    } catch {
      setConn('down');
    } finally {
      setBusy(false);
    }
  };

  const completeHandover = async () => {
    setBusy(true);
    try {
      await fetch(`/api/incidents/${active.incident_id}/arrived`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      setActiveId(null);
      setStage('pickup');
    } catch {
      setConn('down');
    } finally {
      setBusy(false);
    }
  };

  const divertTo = async (hospitalName) => {
    setBusy(true);
    try {
      const res = await fetch('/api/driver/change_hospital', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          incident_id: active.incident_id,
          hospital_name: hospitalName,
        }),
      });
      patch(await res.json());
    } catch {
      setConn('down');
    } finally {
      setBusy(false);
    }
  };

  /* ---- Queue ----------------------------------------------------------- */
  if (!active) {
    return (
      <div className="mx-auto w-full max-w-[560px] px-4 py-5 lg:max-w-[880px]">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h1 className="font-display text-[20px] font-extrabold leading-tight text-text">
              Assigned calls
            </h1>
            <p className="mt-1 t-meta uppercase tracking-[0.1em] text-text-faint">
              {driverInfo?.unitId ?? 'Unit'} · {driverInfo?.hubName ?? 'Base'}
            </p>
          </div>
          <ConnectionState status={conn} />
        </div>

        {dispatches.length === 0 ? (
          <Panel>
            <Empty
              title="No calls assigned"
              hint="When dispatch approves a plan for your service and hub, it appears here."
            />
          </Panel>
        ) : (
          <div className="space-y-2">
            {dispatches.map((d) => (
              <button
                key={d.incident_id}
                onClick={() => {
                  setActiveId(d.incident_id);
                  setStage('pickup');
                }}
                className="block w-full rounded-md border border-rule bg-paper p-4 text-left transition-colors hover:border-signal"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[12px] font-bold text-text">
                    {d.incident_id}
                  </span>
                  <StatusTag status={d.status} />
                </div>
                <div className="mt-1.5 text-[14px] font-bold capitalize text-text">
                  {d.emergency_type ?? 'Emergency'}
                </div>
                <p className="mt-0.5 line-clamp-2 text-[12px] leading-relaxed text-text-muted">
                  {d.citizen_text}
                </p>
                <div className="mt-2.5 flex items-center justify-between gap-2">
                  <SeverityTag severity={d.severity ?? 0} />
                  <span className="t-tag text-signal">
                    {d.citizen_view?.phase1_eta_minutes ?? '--'} min out
                  </span>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  /* ---- Navigating ------------------------------------------------------ */
  const heading =
    stage === 'pickup'
      ? { eyebrow: 'Stage 1 of 2', title: 'Drive to the patient' }
      : { eyebrow: 'Stage 2 of 2', title: 'Transport to hospital' };

  const destination =
    stage === 'pickup'
      ? { label: 'Patient location', name: active.citizen_name ?? 'Reported location' }
      : { label: 'Receiving ER', name: view.hospital_name ?? 'Destination' };

  return (
    <div className="mx-auto w-full max-w-[560px] space-y-3 px-4 py-4 lg:max-w-[880px]">
      <button
        onClick={() => setActiveId(null)}
        className="tap inline-flex items-center gap-1 py-1 t-tag text-text-muted hover:text-text"
      >
        <ChevronLeft size={14} /> All calls
      </button>

      {/* The next instruction, always first. */}
      <Panel ink>
        <div className="px-4 py-4">
          <div className="eyebrow eyebrow-ink">{heading.eyebrow}</div>
          <h1 className="mt-1.5 font-display text-[clamp(20px,5vw,26px)] font-extrabold leading-tight text-on-ink">
            {heading.title}
          </h1>
          <div className="mt-3 border-t border-rule-ink pt-3">
            <div className="eyebrow eyebrow-ink">{destination.label}</div>
            <p className="mt-1 text-[15px] font-semibold leading-snug text-on-ink">
              {destination.name}
            </p>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-4 border-t border-rule-ink pt-3.5 sm:gap-x-8">
            <Stat
              ink
              label="Time to destination"
              value={stage === 'pickup' ? (view.phase1_eta_minutes ?? '--') : (view.eta_minutes ?? '--')}
              unit="min"
              tone="signal"
            />
            {stage === 'transport' && (
              <Stat ink label="Distance" value={fmt.km(view.distance_meters)} unit="km" />
            )}
            <Stat ink label="Call" value={active.incident_id} tone="muted" />
          </div>
        </div>
      </Panel>

      <Panel className="overflow-hidden">
        <MapOverlay
          height="min(52vh, 440px)"
          routeCoordinates={view.route_coordinates}
          phase1Coordinates={view.phase1_route_coordinates}
          origin={view.origin ?? active.location}
          hospital={
            view.hospital_location
              ? { ...view.hospital_location, name: view.hospital_name }
              : null
          }
          hub={view.phase1_hub}
          activePhase={stage === 'pickup' ? 1 : 2}
          className="!rounded-none !border-0"
        />
      </Panel>

      {stage === 'pickup' ? (
        <Button onClick={arriveAtPatient} disabled={busy} size="lg" className="w-full">
          <Check size={17} />
          {busy ? 'Confirming…' : 'I have reached the patient'}
        </Button>
      ) : (
        <Button
          variant="ink"
          onClick={completeHandover}
          disabled={busy}
          size="lg"
          className="w-full"
        >
          <Check size={17} />
          {busy ? 'Confirming…' : 'Patient handed over at ER'}
        </Button>
      )}

      {/* Divert: only meaningful once the patient is aboard. */}
      {stage === 'transport' && active.action_plan?.all_hospitals?.length > 0 && (
        <Panel>
          <PanelHead label="Change destination" />
          <div className="p-2.5">
            <p className="px-1.5 pb-2 text-[11px] leading-relaxed text-text-muted">
              If the assigned ER diverts, pick another. The route and the caller's screen update
              immediately.
            </p>
            <div className="space-y-1.5">
              {active.action_plan.all_hospitals.slice(0, 4).map((h, i) => {
                const current = view.hospital_name === h.name;
                return (
                  <button
                    key={h.hospital_id ?? i}
                    onClick={() => divertTo(h.name)}
                    disabled={busy || current}
                    className={`flex w-full items-center justify-between gap-3 rounded-sm border px-3 py-2.5 text-left transition-colors ${
                      current
                        ? 'border-signal bg-signal-wash'
                        : 'border-rule bg-paper hover:border-signal disabled:opacity-50'
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-[12.5px] font-semibold text-text">
                        {h.name}
                      </span>
                      <span className="mt-1 block t-meta text-text-faint">
                        {h.road_eta_seconds ? `${fmt.minutes(h.road_eta_seconds)} min by road` : '—'}
                      </span>
                    </span>
                    {current ? (
                      <span className="shrink-0 t-tag text-signal-hover">
                        Current
                      </span>
                    ) : (
                      <Building2 size={15} className="shrink-0 text-text-faint" />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        </Panel>
      )}

      <LiveEmergencyChat
        incidentId={active.incident_id}
        senderRole="driver"
        senderName={driverInfo?.unitId ?? 'Responder'}
        height="min(32vh, 260px)"
        contacts={{
          citizen: active.citizen_phone,
          hospital: active.hospital_phone ?? view?.hospital_phone,
          unit: driverInfo?.phone ?? active.unit_phone ?? view?.unit_phone,
        }}
      />
    </div>
  );
}
