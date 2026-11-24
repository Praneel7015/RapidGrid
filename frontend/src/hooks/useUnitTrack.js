import { useEffect, useRef, useState } from 'react';

const EMPTY = { incidentId: null, unit: null, trail: [], simSpeed: null, live: null };

/**
 * Minutes still to drive on this leg, from where the unit is now (the
 * server's live status), or null when the unit is not on that leg. Every
 * screen uses this, so they all show the same, counting-down figure.
 */
export function liveEtaMinutes(track, leg) {
  const live = track?.live;
  if (!live || live.leg !== leg || !Number.isFinite(live.remaining_s)) return null;
  return Math.max(1, Math.round(live.remaining_s / 60));
}

/**
 * Live position of the unit on a call: polls /api/incidents/{id}/track every
 * 2 s while the call is active. Returns { unit, trail, simSpeed }.
 *
 * The snapshot is tied to its incident, so switching calls never shows the
 * previous call's unit, and only the newest request may update it (a slow
 * older response cannot overwrite a newer one).
 */
export default function useUnitTrack(incidentId, active) {
  const [track, setTrack] = useState(EMPTY);
  const latest = useRef(0);

  useEffect(() => {
    if (!incidentId || !active) {
      setTrack(EMPTY);
      return undefined;
    }
    let cancelled = false;
    let inFlight = false;
    const load = async () => {
      // One request at a time: on a slow link a new poll would otherwise
      // supersede every response before it arrived, freezing the unit.
      if (inFlight) return;
      inFlight = true;
      const request = ++latest.current;
      try {
        const res = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/track`, {
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled || request !== latest.current) return;
        setTrack({ incidentId, unit: data.unit, trail: data.trail ?? [], simSpeed: data.sim_speed ?? null,
          live: data.live ?? null });
      } catch {
        // Keep the last known position for this same call, but not its live
        // time to go: a figure that has stopped updating must not read as
        // "now". The next successful poll restores it.
        if (!cancelled && request === latest.current) {
          setTrack((t) => (t.incidentId === incidentId ? { ...t, live: null } : t));
        }
      } finally {
        inFlight = false;
      }
    };
    load();
    const timer = setInterval(load, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [incidentId, active]);

  return track.incidentId === incidentId ? track : EMPTY;
}
