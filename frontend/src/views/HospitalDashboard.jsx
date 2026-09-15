/**
 * Emergency department terminal.
 *
 * Answers one question for the charge nurse: what is arriving, how bad, and
 * how long have we got. Everything else is secondary.
 *
 * The capacity toggle is a real control - closing ICU beds removes this
 * facility from the routing engine's candidate list for new calls - so it is
 * treated as a consequential action, not a decorative switch.
 */

import React, { useState, useEffect } from 'react';
import { Check, BedDouble } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
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
} from '../components/ui';

export default function HospitalDashboard() {
  const { hospitalInfo } = useAuth();
  const [incoming, setIncoming] = useState([]);
  const [icuOpen, setIcuOpen] = useState(true);
  const [acknowledged, setAcknowledged] = useState({});
  const [openThread, setOpenThread] = useState(null);
  const [conn, setConn] = useState('ok');

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const id = hospitalInfo?.id || 'all';
        const name = encodeURIComponent(hospitalInfo?.name ?? '');
        const res = await fetch(`/api/hospital/${id}/incoming?name=${name}`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (!cancelled) {
          setIncoming(data.incoming ?? []);
          setConn('ok');
        }
      } catch {
        if (!cancelled) setConn('down');
      }
    };
    load();
    const timer = setInterval(load, 2500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [hospitalInfo]);

  const setCapacity = async (open) => {
    setIcuOpen(open);
    try {
      const id = hospitalInfo?.id || 'all';
      await fetch(`/api/hospital/${id}/resources`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ icu_available: open }),
      });
    } catch {
      setConn('down');
    }
  };

  const critical = incoming.filter((c) => (c.severity ?? 0) >= 0.8).length;
  const soonest = incoming.reduce(
    (min, c) => (c.eta_minutes != null && c.eta_minutes < min ? c.eta_minutes : min),
    Infinity,
  );

  return (
    <div className="mx-auto flex max-w-[1280px] flex-col gap-3 p-3 lg:p-4">
      <Panel ink>
        <div className="flex flex-wrap items-center justify-between gap-x-8 gap-y-4 px-4 py-4 sm:px-5">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-4 sm:gap-x-9">
            <Stat ink label="Inbound patients" value={incoming.length} />
            <Stat
              ink
              label="Critical"
              value={critical}
              tone={critical ? 'critical' : 'default'}
            />
            <Stat
              ink
              label="Next arrival"
              value={Number.isFinite(soonest) ? soonest : '--'}
              unit="min"
              tone="signal"
            />
          </div>

          <div className="flex items-center gap-3">
            <ConnectionState status={conn} />
            <div className="flex items-center gap-2 rounded-sm border border-rule-ink bg-ink-raised px-3 py-2">
              <BedDouble size={14} className="text-on-ink-muted" />
              <span className="eyebrow eyebrow-ink">ICU capacity</span>
              <div className="flex overflow-hidden rounded-xs border border-rule-ink">
                <button
                  onClick={() => setCapacity(true)}
                  className={`tap px-3 py-2.5 t-tag transition-colors lg:px-2.5 lg:py-1 ${
                    icuOpen ? 'bg-signal text-ink' : 'bg-transparent text-on-ink-muted'
                  }`}
                >
                  Accepting
                </button>
                <button
                  onClick={() => setCapacity(false)}
                  className={`tap px-3 py-2.5 t-tag transition-colors lg:px-2.5 lg:py-1 ${
                    !icuOpen ? 'bg-critical text-white' : 'bg-transparent text-on-ink-muted'
                  }`}
                >
                  On divert
                </button>
              </div>
            </div>
          </div>
        </div>
        {!icuOpen && (
          <div className="border-t border-rule-ink bg-critical px-5 py-2">
            <p className="t-tag text-white">
              On divert — routing will send new critical cases elsewhere
            </p>
          </div>
        )}
      </Panel>

      <Panel>
        <PanelHead
          label={`Inbound · ${incoming.length}`}
          right={
            <span className="hidden t-meta text-text-faint sm:inline">
              {hospitalInfo?.name ?? 'All receiving facilities'}
            </span>
          }
        />
        {incoming.length === 0 ? (
          <Empty
            title="No inbound patients"
            hint="Patients appear here as soon as dispatch approves a plan that names this hospital."
          />
        ) : (
          <div>
            {incoming
              .slice()
              .sort((a, b) => (b.severity ?? 0) - (a.severity ?? 0))
              .map((c) => {
                const band = severityBand(c.severity ?? 0);
                const ack = acknowledged[c.incident_id];
                return (
                  <article
                    key={c.incident_id}
                    className={`border-b border-rule px-4 py-4 last:border-0 ${
                      band.key === 'critical' ? 'bg-critical-wash/40' : ''
                    }`}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] font-bold text-text">
                            {c.incident_id}
                          </span>
                          <SeverityTag severity={c.severity ?? 0} />
                          <span className="t-meta uppercase tracking-[0.1em] text-text-faint">
                            {c.emergency_type}
                          </span>
                        </div>
                        <p className="mt-2 text-[13px] leading-relaxed text-text">
                          {c.patient_summary}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 t-meta text-text-faint">
                          <span>Unit {c.assigned_driver}</span>
                          <StatusTag status={c.status} />
                        </div>
                      </div>

                      <div className="flex w-full shrink-0 items-center justify-between gap-4 border-t border-rule pt-3 sm:w-auto sm:justify-end sm:border-0 sm:pt-0">
                        <div className="text-right">
                          <div className="eyebrow">Arrives in</div>
                          <div className="font-mono text-[26px] font-bold leading-none text-signal">
                            {c.eta_minutes ?? '--'}
                            <span className="ml-1 text-[13px] font-medium opacity-60">min</span>
                          </div>
                        </div>
                        {ack ? (
                          <span className="inline-flex items-center gap-1.5 rounded-xs border border-ink px-2.5 py-1.5 t-tag text-text">
                            <Check size={12} /> Bay ready
                          </span>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() =>
                              setAcknowledged((p) => ({ ...p, [c.incident_id]: true }))
                            }
                          >
                            Acknowledge
                          </Button>
                        )}
                      </div>
                    </div>

                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <button
                        onClick={() =>
                          setOpenThread(openThread === c.incident_id ? null : c.incident_id)
                        }
                        className="tap inline-flex items-center py-2.5 t-tag text-text-muted underline underline-offset-[3px] hover:text-text lg:py-1"
                      >
                        {openThread === c.incident_id ? 'Hide channel' : 'Message the crew'}
                      </button>
                    </div>

                    {openThread === c.incident_id && (
                      <div className="rise mt-3">
                        <LiveEmergencyChat
                          incidentId={c.incident_id}
                          senderRole="hospital"
                          senderName={hospitalInfo?.name ?? 'ER desk'}
                          height="180px"
                          contacts={{
                            citizen: c.citizen_phone,
                            unit: c.unit_phone ?? c.citizen_view?.unit_phone,
                            hospital: hospitalInfo?.phone ?? c.hospital_phone,
                          }}
                        />
                      </div>
                    )}
                  </article>
                );
              })}
          </div>
        )}
        <p className="border-t border-rule bg-paper-sunk px-4 py-3 t-micro leading-relaxed text-text-faint">
          <Provenance kind="simulated" className="mr-1.5" />
          Bed and blood availability shown across the platform are modelled. Connecting a hospital
          HMIS or HL7 feed would replace them with live counts.
        </p>
      </Panel>
    </div>
  );
}
