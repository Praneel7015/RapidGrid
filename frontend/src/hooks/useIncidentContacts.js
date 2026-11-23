import { useEffect, useState } from 'react';

/**
 * Who this viewer may call about an incident, from the server's private
 * contacts directory: the 112 line for everyone; the unit, dispatch, and the
 * receiving hospital's ER and ICU only for people involved in the call.
 * Refreshed every 15 s (a backup or a diversion changes who to call).
 */
export default function useIncidentContacts(incidentId) {
  const [state, setState] = useState({ incidentId: null, contacts: null });

  useEffect(() => {
    if (!incidentId) return undefined;
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/contacts`, {
          signal: AbortSignal.timeout(5000),
        });
        if ([401, 403, 404].includes(res.status)) {
          // An authoritative "no" (signed out, no access, call gone): drop
          // the old numbers. Other failures keep the last answer.
          if (!cancelled) setState({ incidentId, contacts: null });
          return;
        }
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setState({ incidentId, contacts: data });
      } catch {
        // keep the last answer for this call
      }
    };
    load();
    const timer = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [incidentId]);

  return state.incidentId === incidentId ? state.contacts : null;
}

/** Call buttons for a viewer, excluding their own role's line. */
export function callTargets(contacts, viewerRole) {
  // The caller's screen shows its call buttons above the map; not twice.
  if (!contacts || viewerRole === 'citizen') return [];
  const out = [];
  if (viewerRole !== 'driver' && contacts.unit?.phone) {
    out.push({ phone: contacts.unit.phone, label: `Unit ${contacts.unit.unit_id}` });
  }
  if (viewerRole !== 'hospital' && contacts.hospital?.emergency_phone) {
    out.push({ phone: contacts.hospital.emergency_phone, label: 'ER' });
  }
  if (viewerRole !== 'hospital' && contacts.hospital?.icu_phone) {
    out.push({ phone: contacts.hospital.icu_phone, label: 'ICU' });
  }
  if (viewerRole !== 'dispatcher' && contacts.dispatch?.phone) {
    out.push({ phone: contacts.dispatch.phone, label: 'Dispatch' });
  }
  return out;
}
