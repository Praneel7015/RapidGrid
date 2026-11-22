import { useEffect, useState } from 'react';

/**
 * Live position of the unit on a call: polls /api/incidents/{id}/track every
 * 2 s while the call is active. Returns { unit, trail, simSpeed } or nulls.
 */
export default function useUnitTrack(incidentId, active) {
  const [track, setTrack] = useState({ unit: null, trail: [], simSpeed: null });

  useEffect(() => {
    if (!incidentId || !active) {
      setTrack({ unit: null, trail: [], simSpeed: null });
      return undefined;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/track`, {
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setTrack({ unit: data.unit, trail: data.trail ?? [], simSpeed: data.sim_speed ?? null });
      } catch {
        // Keep the last known position; the next poll retries.
      }
    };
    load();
    const timer = setInterval(load, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [incidentId, active]);

  return track;
}
