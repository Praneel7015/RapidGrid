/**
 * City dispatch console.
 *
 * The judge-facing screen, and the one under real time pressure: a person
 * decides whether to accept the system's recommendation about where a dying
 * patient goes. Everything here serves "decide fast, and be able to defend it".
 *
 * Nothing on this screen is invented. The previous version displayed a
 * hardcoded "96.4% AI Fusion Accuracy", a fallback hospital score of 0.92, and
 * default ICU bed counts - all presented as measurements. Values that are not
 * measured are either omitted or labelled.
 */

import React, { useState, useEffect, useRef } from 'react';
import { Check, CornerUpRight, X } from 'lucide-react';
import MapOverlay from '../components/MapOverlay';
import AIRationale from '../components/AIRationale';
import CityControls from '../components/CityControls';
import LiveEmergencyChat from '../components/LiveEmergencyChat';
import {
  Panel,
  PanelHead,
  Button,
  Stat,
  SeverityTag,
  StatusTag,
  Provenance,
  ConnectionState,
  Empty,
  severityBand,
  fmt,
} from '../components/ui';

export default function DispatcherDashboard() {
  const [incidents, setIncidents] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [autoDispatch, setAutoDispatch] = useState(false);
  const [showOverride, setShowOverride] = useState(false);
  const [conn, setConn] = useState('ok');
  const [lastOk, setLastOk] = useState(null);
  const [busy, setBusy] = useState(false);
  const [overlay, setOverlay] = useState({ closures: [], congestion: [] });
  const failures = useRef(0);

  useEffect(() => {
    let cancelled = false;

    const fetchIncidents = async () => {
      try {
        const res = await fetch('/api/incidents/');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;

        failures.current = 0;
        setConn('ok');
        setLastOk(new Date().toLocaleTimeString([], { hour12: false }));

        const list = data.incidents || [];
        setIncidents(list);

        if (autoDispatch) {
          for (const inc of list) {
            if (inc.status === 'awaiting_dispatcher_approval' && inc.action_plan) {
              await fetch(`/api/incidents/${inc.incident_id}/approve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  dispatcher_id: 'AUTO-DISPATCHER',
                  approved_hospital:
                    inc.action_plan?.recommended_hospital?.hospital_id ?? '',
                  approved_route: inc.action_plan?.recommended_route?.route_id ?? '',
                }),
              }).catch(() => {});
            }
          }
        }
      } catch {
        if (cancelled) return;
        failures.current += 1;
        setConn(failures.current > 2 ? 'down' : 'retry');
      }
    };

    fetchIncidents();
    const timer = setInterval(fetchIncidents, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [autoDispatch]);

  const newestActionable =
    [...incidents].reverse().find((i) => i.status === 'awaiting_dispatcher_approval') ??
    [...incidents].reverse().find((i) => i.action_plan) ??
    incidents[incidents.length - 1] ??
    null;
  const selected =
    incidents.find((i) => i.incident_id === selectedId) || newestActionable;

  const loadOverlay = React.useCallback(async () => {
    try {
      const res = await fetch('/api/city/overlay?limit=300');
      if (res.ok) setOverlay(await res.json());
    } catch { /* the map still renders without the city overlay */ }
  }, []);

  useEffect(() => {
    loadOverlay();
    const timer = setInterval(loadOverlay, 8000);
    return () => clearInterval(timer);
  }, [loadOverlay]);

  const approve = async (hospital = null) => {
    if (!selected?.action_plan) return;
    const chosen = hospital || selected.action_plan.recommended_hospital;
    if (!chosen?.hospital_id) return;

    setBusy(true);
    try {
      const res = await fetch(`/api/incidents/${selected.incident_id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          dispatcher_id: 'DISPATCHER-01',
          approved_hospital: chosen.hospital_id,
          approved_route: selected.action_plan?.recommended_route?.route_id ?? '',
        }),
      });
      if (res.ok) {
        const updated = await res.json();
        setIncidents((prev) =>
          prev.map((i) => (i.incident_id === updated.incident_id ? updated : i)),
        );
        setShowOverride(false);
      }
    } catch {
      setConn('retry');
    } finally {
      setBusy(false);
    }
  };

  const view = selected?.citizen_view ?? {};
  const plan = selected?.action_plan ?? null;
  const routeCoords = view.route_coordinates ?? plan?.recommended_route?.coordinates ?? [];

  const queue = incidents.filter(
    (i) => i.status === 'processing' || i.status === 'awaiting_dispatcher_approval',
  );
  const active = incidents.filter((i) => i.status === 'dispatched');
  const queueVisible = incidents.filter(
    (i) => !['failed', 'completed'].includes(i.status),
  );
  const needsAction = incidents.filter(
    (i) => i.status === 'awaiting_dispatcher_approval',
  ).length;
  const peakSeverity = incidents.reduce((m, i) => Math.max(m, i.severity ?? 0), 0);
  const hot = peakSeverity >= 0.8 && needsAction > 0;

  return (
    <div className="mx-auto flex min-h-[calc(100vh-56px)] max-w-[1600px] flex-col gap-3 p-3 lg:p-4">
      {/* ---- Command rail ------------------------------------------------ */}
      <Panel ink className="relative overflow-hidden">
        {hot && <div className="sweep absolute inset-x-0 top-0 h-[2px] overflow-hidden" />}
        <div className="flex flex-wrap items-center justify-between gap-x-8 gap-y-4 px-4 py-4 sm:px-5">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-4 sm:gap-x-9">
            <Stat ink label="Awaiting approval" value={needsAction} tone={needsAction ? 'signal' : 'default'} />
            <Stat ink label="In queue" value={queue.length} />
            <Stat ink label="Units dispatched" value={active.length} />
            <Stat
              ink
              label="Peak severity"
              value={peakSeverity ? `${fmt.pct(peakSeverity)}%` : '--'}
              tone={peakSeverity >= 0.8 ? 'critical' : 'default'}
              sub={peakSeverity ? severityBand(peakSeverity).label : 'No active calls'}
            />
          </div>

          <div className="flex items-center gap-3">
            <ConnectionState status={conn} lastOk={lastOk} />
            <label className="flex cursor-pointer items-center gap-2.5 rounded-sm border border-rule-ink bg-ink-raised px-3 py-2">
              <span className="eyebrow eyebrow-ink">Auto-approve</span>
              <button
                role="switch"
                aria-checked={autoDispatch}
                onClick={() => setAutoDispatch((v) => !v)}
                className={`tap relative h-7 w-12 rounded-full transition-colors lg:h-[18px] lg:w-8 ${
                  autoDispatch ? 'bg-signal' : 'bg-ink-sunk'
                }`}
              >
                <span
                  className={`absolute top-[3px] h-[22px] w-[22px] rounded-full bg-white transition-all lg:top-[2px] lg:h-[14px] lg:w-[14px] ${
                    autoDispatch ? 'left-[23px] lg:left-[16px]' : 'left-[3px] lg:left-[2px]'
                  }`}
                />
              </button>
            </label>
          </div>
        </div>
      </Panel>

      <div className="grid flex-1 grid-cols-1 gap-3 md:grid-cols-[240px_minmax(0,1fr)] lg:grid-cols-[270px_minmax(0,1fr)] xl:grid-cols-[290px_minmax(0,1fr)_380px]">
        {/* ---- Queue ----------------------------------------------------- */}
        <Panel className="flex flex-col overflow-hidden md:max-h-[calc(100vh-150px)]">
          <PanelHead
            label={`Call queue · ${queueVisible.length}`}
            right={
              needsAction > 0 && (
                <span className="t-tag text-signal-hover">
                  {needsAction} need approval
                </span>
              )
            }
          />
          {queueVisible.length === 0 ? (
            <Empty
              title="No active calls"
              hint="Incidents raised from the citizen app appear here the moment the agents finish assessing them."
            />
          ) : (
            <div className="min-h-0 flex-1 md:overflow-y-auto">
              {queueVisible
                .slice()
                .reverse()
                .map((inc) => {
                  const isSel = selected?.incident_id === inc.incident_id;
                  const band = severityBand(inc.severity ?? 0);
                  return (
                    <button
                      key={inc.incident_id}
                      onClick={() => setSelectedId(inc.incident_id)}
                      className={`block w-full border-b border-rule/70 px-4 py-3 text-left transition-colors ${
                        isSel ? 'bg-signal-wash' : 'bg-paper hover:bg-paper-hover'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span
                          className={`font-mono text-[11px] font-bold ${
                            isSel ? 'text-signal-hover' : 'text-text'
                          }`}
                        >
                          {inc.incident_id}
                        </span>
                        <StatusTag status={inc.status} />
                      </div>
                      <div className="mt-1.5 truncate text-[12px] font-semibold capitalize text-text">
                        {inc.emergency_type ?? 'Assessing…'}
                      </div>
                      <p className="mt-0.5 truncate text-[11px] text-text-muted">
                        {inc.citizen_text}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                        <SeverityTag severity={inc.severity ?? 0} />
                        <span className="min-w-0 truncate t-meta text-text-faint">
                          {inc.citizen_view?.hospital_name ?? '—'}
                        </span>
                      </div>
                    </button>
                  );
                })}
            </div>
          )}
        </Panel>

        {/* ---- Map + actions --------------------------------------------- */}
        {selected ? (
          <div className="flex min-h-[520px] flex-col gap-3">
            <Panel className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <PanelHead
                label="Tactical map"
                right={
                  <div className="hidden items-center gap-3 t-meta sm:flex">
                    <span className="flex items-center gap-1.5 text-text-muted">
                      <span className="inline-block h-0.5 w-4 bg-ink-raised" style={{ backgroundImage: 'repeating-linear-gradient(90deg,#003b36 0 4px,transparent 4px 7px)' }} />
                      Phase 1 · to patient
                    </span>
                    <span className="flex items-center gap-1.5 text-text-muted">
                      <span className="inline-block h-[3px] w-4 rounded-full bg-signal" />
                      Phase 2 · to ER
                    </span>
                    {overlay.closures?.length > 0 && (
                      <span className="flex items-center gap-1.5 text-critical">
                        <span
                          className="inline-block h-[3px] w-4"
                          style={{ backgroundImage: 'repeating-linear-gradient(90deg,#c23a2b 0 2px,transparent 2px 5px)' }}
                        />
                        Closed
                      </span>
                    )}
                    {overlay.congestion?.length > 0 && (
                      <span className="flex items-center gap-1.5 text-text-muted">
                        <span className="inline-block h-[4px] w-4 rounded-full" style={{ background: '#c1121f', opacity: 0.55 }} />
                        Displaced
                      </span>
                    )}
                  </div>
                }
              />
              <div className="relative min-h-[300px] flex-1 sm:min-h-[360px] lg:min-h-[420px]">
                {routeCoords.length > 0 || view.phase1_route_coordinates?.length > 0 ? (
                  <MapOverlay
                    height="100%"
                    routeCoordinates={routeCoords}
                    phase1Coordinates={view.phase1_route_coordinates}
                    origin={view.origin ?? selected.location}
                    hospital={
                      view.hospital_location
                        ? { ...view.hospital_location, name: view.hospital_name }
                        : null
                    }
                    hub={view.phase1_hub}
                    closures={overlay.closures}
                    congestion={overlay.congestion}
                    activePhase={selected.status === 'dispatched' ? 1 : 2}
                    className="!rounded-none !border-0"
                  />
                ) : (
                  <div className="absolute inset-0 grid place-items-center bg-paper-sunk">
                    <div className="text-center">
                      <div className="live-dot mx-auto mb-3 h-2 w-2 rounded-full bg-signal" />
                      <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-text-faint">
                        Agents computing route
                      </p>
                    </div>
                  </div>
                )}
              </div>
              {(view.eta_minutes != null || view.distance_meters != null) && (
                <div className="flex flex-wrap items-center gap-x-8 gap-y-3 border-t border-rule px-5 py-3">
                  <Stat
                    label="Hub to patient"
                    value={view.phase1_eta_minutes ?? '--'}
                    unit="min"
                    sub={view.phase1_hub?.name}
                  />
                  <Stat
                    label="Patient to ER"
                    value={view.eta_minutes ?? '--'}
                    unit="min"
                    tone="signal"
                    sub={view.hospital_name}
                  />
                  <Stat
                    label="Transport distance"
                    value={fmt.km(view.distance_meters)}
                    unit="km"
                  />
                </div>
              )}
            </Panel>

            {/* Actions */}
            <Panel className="p-3">
              {autoDispatch && (
                <div className="mb-3 flex items-center justify-between gap-3 rounded-sm border border-signal-edge bg-signal-wash px-3 py-2">
                  <span className="t-tag text-signal-hover">
                    Auto-approve is on — plans dispatch without review
                  </span>
                  <StatusTag status={selected.status} />
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                {!autoDispatch && selected.status === 'awaiting_dispatcher_approval' && (
                  <Button onClick={() => approve()} disabled={busy || !plan} className="flex-1">
                    <Check size={15} />
                    {busy ? 'Dispatching…' : 'Approve and dispatch'}
                  </Button>
                )}
                <Button
                  variant="quiet"
                  onClick={() => setShowOverride((v) => !v)}
                  disabled={!plan}
                  className="flex-1"
                >
                  {showOverride ? <X size={15} /> : <CornerUpRight size={15} />}
                  {showOverride ? 'Close' : 'Send to a different hospital'}
                </Button>
              </div>

              {showOverride && plan?.all_hospitals?.length > 0 && (
                <div className="rise mt-3 border-t border-rule pt-3">
                  <div className="eyebrow mb-2">Override destination</div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {plan.all_hospitals.map((h, i) => (
                      <button
                        key={h.hospital_id ?? i}
                        onClick={() => approve(h)}
                        disabled={busy}
                        className="rounded-sm border border-rule bg-paper-sunk p-3 text-left transition-colors hover:border-signal hover:bg-signal-wash disabled:opacity-50"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <span className="text-[12px] font-semibold leading-snug text-text">
                            {h.name}
                          </span>
                          <span className="shrink-0 font-mono text-[11px] font-bold text-text">
                            {fmt.score(h.score)}
                          </span>
                        </div>
                        <div className="mt-2 flex items-center justify-between gap-2 t-meta text-text-faint">
                          <span>
                            {h.road_eta_seconds
                              ? `${fmt.minutes(h.road_eta_seconds)} min by road`
                              : 'ETA pending'}
                          </span>
                          <Provenance kind={h.capability_provenance}>
                            {h.capability_provenance === 'curated' ? 'Verified' : 'Modelled'}
                          </Provenance>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </Panel>
          </div>
        ) : (
          <Panel className="grid place-items-center">
            <Empty
              title="Select a call to review"
              hint="Pick an incident from the queue to see its route, the ranked destinations, and why the system chose one over the others."
            />
          </Panel>
        )}

        {/* ---- Rationale -------------------------------------------------- */}
        <div className="grid content-start gap-3 md:col-span-2 md:grid-cols-2 xl:col-span-1 xl:grid-cols-1 xl:max-h-[calc(100vh-150px)] xl:overflow-y-auto">
          <CityControls onChange={loadOverlay} />

          {plan ? (
            <>
              <AIRationale plan={plan} />
              <Panel>
                <PanelHead label={`Ranked destinations · ${plan.all_hospitals?.length ?? 0}`} />
                <div>
                  {plan.all_hospitals?.map((h, i) => {
                    const chosen = view.hospital_name
                      ? view.hospital_name === h.name
                      : i === 0;
                    return (
                      <button
                        key={h.hospital_id ?? i}
                        onClick={() => approve(h)}
                        disabled={busy}
                        className={`block w-full border-b border-rule/70 px-4 py-3 text-left transition-colors last:border-0 ${
                          chosen ? 'bg-signal-wash' : 'hover:bg-paper-hover'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex min-w-0 items-start gap-2">
                            <span className="mt-px t-meta font-bold text-text-faint">
                              {String(i + 1).padStart(2, '0')}
                            </span>
                            <span className="min-w-0 text-[12px] font-semibold leading-snug text-text">
                              {h.name}
                            </span>
                          </div>
                          <span className="shrink-0 font-mono text-[12px] font-bold text-text">
                            {fmt.score(h.score)}
                          </span>
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-2 pl-6">
                          <Provenance kind={h.capability_provenance} />
                          <span className="t-meta text-text-faint">
                            {h.road_eta_seconds
                              ? `${fmt.minutes(h.road_eta_seconds)} min`
                              : '—'}
                          </span>
                          <span className="t-meta text-text-faint">
                            ICU <span className="val-modelled">{h.icu_available ?? '—'}</span>
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>
                <p className="border-t border-rule bg-paper-sunk px-4 py-2.5 t-micro leading-relaxed text-text-faint">
                  Capability profiles are curated from published hospital data. Live bed counts are
                  modelled — no hospital HMIS feed is integrated.
                </p>
              </Panel>
            </>
          ) : selected ? (
            <Panel className="grid place-items-center">
              <Empty
                title="Assessing the call"
                hint="The accessibility, coordinator, traffic, hospital and routing agents are still working. The plan appears here when fusion completes."
              />
            </Panel>
          ) : null}

          {selected?.incident_id && (
            <LiveEmergencyChat
              incidentId={selected.incident_id}
              senderRole="dispatcher"
              senderName="Dispatch"
              height="220px"
              contacts={{
                citizen: selected.citizen_phone,
                unit: selected.unit_phone ?? view?.unit_phone,
                hospital: selected.hospital_phone ?? view?.hospital_phone,
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
