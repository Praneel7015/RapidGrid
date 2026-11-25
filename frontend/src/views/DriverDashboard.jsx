/**
 * Responder field app.
 *
 * Read from a cradle in a moving vehicle: one primary action per stage, large
 * targets, and the next instruction always above the fold. Stage 1 runs the
 * unit to the patient; stage 2 runs the patient to the receiving ER, and the
 * destination can change mid-transport if the ER diverts.
 */

import React, { useState, useEffect } from 'react';
import shortId from '../utils/shortId';
import { Check, ChevronLeft, Building2 } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import MapOverlay from '../components/MapOverlay';
import { useHospitals, useStations, useOsmHospitals } from '../hooks/useCityLayers';
import CallerDetails from '../components/CallerDetails';
import useUnitTrack, { liveEtaMinutes } from '../hooks/useUnitTrack';
import useShareLocation from '../hooks/useShareLocation';
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
  const hospitals = useHospitals();
  const osmHospitals = useOsmHospitals();
  const stations = useStations();
  // On or off duty (an off-duty unit is never dispatched).
  const [onDuty, setOnDuty] = useState(null);
  const [dutyError, setDutyError] = useState(null);
  useEffect(() => {
    if (!driverInfo?.unitId) return undefined;
    let cancelled = false;
    fetch(`/api/units/${encodeURIComponent(driverInfo.unitId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((u) => { if (!cancelled && u) setOnDuty(u.on_duty !== false); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [driverInfo?.unitId]);
  const changeDuty = async (on) => {
    setDutyError(null);
    try {
      const res = await fetch(`/api/units/${encodeURIComponent(driverInfo.unitId)}/duty`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on_duty: on }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
      setOnDuty(data.on_duty !== false);
    } catch (err) {
      setDutyError(err.message);
    }
  };
  const [dispatches, setDispatches] = useState([]);
  const [activeId, setActiveId] = useState(() => sessionStorage.getItem('rapidgrid_driver_incident'));
  const [actionError, setActionError] = useState(null);
  useEffect(() => {
    if (activeId) sessionStorage.setItem('rapidgrid_driver_incident', activeId);
    else sessionStorage.removeItem('rapidgrid_driver_incident');
  }, [activeId]);
  const [conn, setConn] = useState('ok');
  const [busy, setBusy] = useState(false);
  // Remembered on this phone, so a reload (or the browser reopening the
  // page) does not silently stop the unit's live position.
  const shareKey = driverInfo?.unitId ? `rapidgrid.share.${driverInfo.unitId}` : null;
  const [shareLocation, setShareLocationState] = useState(false);
  useEffect(() => {
    if (!shareKey) return;
    try { setShareLocationState(window.localStorage.getItem(shareKey) === '1'); } catch { /* storage blocked */ }
  }, [shareKey]);
  const setShareLocation = (on) => {
    setShareLocationState(on);
    try {
      if (shareKey) window.localStorage.setItem(shareKey, on ? '1' : '0');
    } catch { /* storage blocked: still shares for this visit */ }
  };
  const sharing = useShareLocation(driverInfo?.unitId, shareLocation);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        // A stalled request must show as an outage, not hang silently.
        const res = await fetch('/api/driver/active', { signal: AbortSignal.timeout(5000) });
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (!cancelled) {
          const list = data.dispatches ?? data.active ?? [];
          const vehicle = driverInfo?.vehicleType;
          setDispatches(list.filter((d) => {
            const primaryMedical = ['Ambulance', 'Medical'].includes(d.vehicle_required ?? 'Ambulance');
            const backup = vehicle === 'Ambulance' && !primaryMedical && d.include_ambulance_backup;
            const matchesProfile = !vehicle || d.vehicle_required === vehicle || (vehicle === 'Ambulance' && primaryMedical) || backup;
            return matchesProfile && d.unit_statuses?.[backup ? 'ambulance' : 'primary'] !== 'arrived';
          }));
          setConn('ok');
        }
      } catch {
        if (!cancelled) setConn('down');
      }
    };
    load();
    const timer = setInterval(load, 2500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [driverInfo]);

  const active = dispatches.find((d) => d.incident_id === activeId) ?? null;
  const callTrack = useUnitTrack(active?.incident_id, ['dispatched', 'patient_picked_up'].includes(active?.status));
  // The track is the call's fleet unit. A backup ambulance crew is not that
  // unit: never show them another vehicle's position or time as their own.
  const track = !driverInfo?.unitId || callTrack.unit?.unit_id === driverInfo.unitId
    ? callTrack
    : { ...callTrack, unit: null, trail: [], live: null };
  const view = active?.citizen_view ?? {};
  const primaryMedical = ['Ambulance', 'Medical'].includes(active?.vehicle_required ?? 'Ambulance');
  const unitType = driverInfo?.vehicleType === 'Ambulance' && !primaryMedical && active?.include_ambulance_backup ? 'ambulance' : 'primary';
  const sceneResponse = !primaryMedical && unitType === 'primary';
  const stage = (active?.unit_statuses?.[unitType] ?? active?.status) === 'patient_picked_up' ? 'transport' : 'pickup';

  const patch = (data) => {
    if (!data) return;
    setDispatches((prev) =>
      prev.map((d) =>
        d.incident_id === active.incident_id ? { ...d, ...data, status: data.status === 'rerouted' ? d.status : (data.status ?? d.status) } : d,
      ),
    );
  };

  const arriveAtPatient = async () => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch('/api/driver/arrived_pickup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          incident_id: active.incident_id,
          // Without a unit profile (local open demo), act as the unit dispatch assigned.
          driver_id: driverInfo?.unitId ?? active?.assigned_unit ?? 'drv-11',
          unit_type: unitType,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Pickup failed (HTTP ${res.status})`);
      patch(await res.json());
    } catch (err) {
      setActionError(typeof err.message === 'string' ? err.message : 'The action failed. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const completeHandover = async () => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch(`/api/incidents/${active.incident_id}/arrived?unit_type=${unitType}`,  {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Handover failed (HTTP ${res.status})`);
      setDispatches((prev) => prev.filter((d) => d.incident_id !== active.incident_id));
      setActiveId(null);
    } catch (err) {
      setActionError(typeof err.message === 'string' ? err.message : 'The action failed. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const divertTo = async (hospitalName, hospitalId) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch('/api/driver/change_hospital', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          incident_id: active.incident_id,
          hospital_name: hospitalName,
          hospital_id: hospitalId ?? undefined,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Destination change failed (HTTP ${res.status})`);
      patch(await res.json());
    } catch (err) {
      setActionError(typeof err.message === 'string' ? err.message : 'The action failed. Try again.');
    } finally {
      setBusy(false);
    }
  };

  // On both the call list and the call itself: a crew must be able to turn
  // it on (or see that it stopped) without leaving the call.
  const sharePanel = driverInfo?.unitId ? (
    <Panel className="mb-3" data-testid="share-location">
      <label className="flex items-center justify-between gap-3 px-4 py-3">
        <span className="min-w-0">
          <span className="block text-[13.5px] font-semibold text-text">Share my location</span>
          <span className="mt-0.5 block text-[12px] text-text-muted">
            {sharing.status === 'sharing'
              ? `Live GPS${Number.isFinite(sharing.accuracy) ? ` ±${Math.round(sharing.accuracy)} m` : ''} · keep this screen open`
              : sharing.status === 'waiting'
                ? 'Waiting for a GPS fix…'
                : sharing.error ?? 'Sends this phone\'s GPS as the unit position. Turning it off leaves the unit at its last GPS position; simulation does not resume.'}
          </span>
        </span>
        <input
          type="checkbox"
          role="switch"
          checked={shareLocation}
          onChange={(e) => setShareLocation(e.target.checked)}
          className="h-5 w-5 shrink-0 accent-[var(--color-signal)]"
        />
      </label>
      {onDuty !== null && (
        <label className="flex items-center justify-between gap-3 border-t border-rule px-4 py-3" data-testid="duty-switch">
          <span className="min-w-0">
            <span className="block text-[13.5px] font-semibold text-text">{onDuty ? 'On duty' : 'Off duty'}</span>
            <span className="mt-0.5 block text-[12px] text-text-muted">
              {dutyError ?? (onDuty ? 'Dispatch can send this unit to calls.' : 'Shown on the map, never sent to a call.')}
            </span>
          </span>
          <input
            type="checkbox"
            role="switch"
            aria-label="On duty"
            checked={onDuty}
            onChange={(e) => changeDuty(e.target.checked)}
            className="h-5 w-5 shrink-0 accent-[var(--color-signal)]"
          />
        </label>
      )}
    </Panel>
  ) : null;

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

        {sharePanel}

        {dispatches.length === 0 ? (
          <Panel>
            {conn === 'down' ? (
              // An outage must not read as "nothing to do".
              <Empty
                title="Cannot reach dispatch"
                hint="Retrying every few seconds. Assigned calls appear as soon as the connection returns."
              />
            ) : (
              <Empty
                title="No calls assigned"
                hint="Approved calls for this demo service profile appear here. Profile filtering is a viewing convenience; shared showcase access covers every role."
              />
            )}
          </Panel>
        ) : (
          <div className="space-y-2">
            {dispatches.map((d) => (
              <button
                key={d.incident_id}
                onClick={() => {
                  setActiveId(d.incident_id);
                  setActionError(null);
                }}
                className="block w-full rounded-md border border-rule bg-paper p-4 text-left transition-colors hover:border-signal"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[12px] font-bold text-text" title={d.incident_id}>
                    {shortId(d.incident_id)}
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
                  <span className="t-tag text-signal-hover">
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
    sceneResponse
      ? { eyebrow: 'Scene response', title: 'Drive to the incident' }
      : stage === 'pickup'
      ? { eyebrow: unitType === 'ambulance' ? 'Backup ambulance · Stage 1 of 2' : 'Stage 1 of 2', title: 'Drive to the patient' }
      : { eyebrow: 'Stage 2 of 2', title: 'Transport to hospital' };

  const destination =
    stage === 'pickup'
      ? { label: sceneResponse ? 'Incident location' : 'Patient location', name: 'Reported location' }
      : { label: 'Receiving ER', name: view.hospital_name ?? 'Destination' };

  return (
    <div className="mx-auto w-full max-w-[560px] space-y-3 px-4 py-4 lg:max-w-[880px]">
      <button
        onClick={() => { setActiveId(null); setActionError(null); }}
        className="tap inline-flex items-center gap-1 py-1 t-tag text-text-muted hover:text-text"
      >
        <ChevronLeft size={14} /> All calls
      </button>

      {actionError && <p role="alert" className="rounded-sm border border-critical bg-critical-wash p-3 text-sm text-critical">{actionError}</p>}
      {active?.destination_on_divert && !sceneResponse && (
        <p role="alert" className="rounded-sm border border-critical bg-critical-wash p-3 text-sm text-critical">
          <strong>{active.hospital_name ?? 'The destination hospital'}</strong> has stopped accepting
          patients. Choose another hospital once the patient is on board.
        </p>
      )}
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
          <div data-tour="crew-eta" className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-4 border-t border-rule-ink pt-3.5 sm:gap-x-8">
            <Stat
              ink
              label="Time to destination"
              value={stage === 'pickup'
                ? (liveEtaMinutes(track, 'to_scene') ?? view.phase1_eta_minutes ?? '--')
                : (liveEtaMinutes(track, 'to_hospital') ?? view.eta_minutes ?? '--')}
              unit="min"
              tone="signal"
              modelled
            />
            {stage === 'transport' && (
              <Stat ink label="Distance" value={fmt.km(view.distance_meters)} unit="km" />
            )}
            <Stat ink label="Call" value={shortId(active.incident_id)} tone="muted" />
          </div>
        </div>
      </Panel>

      {sharePanel}

      <Panel className="p-3">
        <CallerDetails key={active.incident_id} incidentId={active.incident_id} />
      </Panel>

      <Panel className="overflow-hidden">
        <MapOverlay
          hospitals={hospitals}
          osmHospitals={osmHospitals}
          stations={stations}
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
          unit={track.unit}
          trail={track.trail}
          simSpeed={track.simSpeed}
          className="!rounded-none !border-0"
        />
      </Panel>

      {sceneResponse ? (
        <Button onClick={completeHandover} disabled={busy} size="lg" className="w-full">
          <Check size={17} />{busy ? 'Confirming…' : 'Unit arrived at incident'}
        </Button>
      ) : stage === 'pickup' ? (
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
      {!sceneResponse && stage === 'transport' && (active.pickup_ranking?.hospitals?.length > 0 || active.action_plan?.all_hospitals?.length > 0) && (
        <Panel data-tour="pickup-ranking">
          <PanelHead label={active.pickup_ranking ? 'Hospitals from the scene now' : 'Change destination'} />
          <div className="p-2.5">
            <p className="px-1.5 pb-2 text-[11px] leading-relaxed text-text-muted">
              {active.pickup_ranking
                ? `${active.pickup_ranking.summary} Ranked again at pickup on capability and modelled road time. Pick another to change; the route and the caller's screen update immediately.`
                : "If the assigned ER diverts, pick another. The route and the caller's screen update immediately."}
            </p>
            <div className="space-y-1.5">
              {(active.pickup_ranking?.hospitals ?? active.action_plan.all_hospitals.slice(0, 4)).map((h, i) => {
                // The ranking marks the current destination by id; names repeat (branches).
                const current = typeof h.current === 'boolean' ? h.current : view.hospital_name === h.name;
                return (
                  <button
                    key={h.hospital_id ?? i}
                    onClick={() => divertTo(h.name, h.hospital_id)}
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
                        {Number.isFinite(h.road_eta_min)
                          ? `${h.road_eta_min} min by road · score ${Number(h.score).toFixed(2)}`
                          : h.road_eta_seconds ? `${fmt.minutes(h.road_eta_seconds)} min by road` : '—'}
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
      />
    </div>
  );
}
