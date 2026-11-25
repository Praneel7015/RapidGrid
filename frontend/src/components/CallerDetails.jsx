/*
 * Caller details stay hidden until a staff member asks for them. The server
 * checks who is asking (dispatch, or the unit on this call) and records each
 * reveal on the incident.
 */

import React, { useState } from 'react';
import { Eye, Phone } from 'lucide-react';
import { Button } from './ui';
import CallNumber from './CallNumber';

export default function CallerDetails({ incidentId }) {
  const [caller, setCaller] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (!incidentId) return null;

  const reveal = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/caller`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Not allowed (HTTP ${res.status})`);
      setCaller(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!caller) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="quiet" size="sm" onClick={reveal} disabled={busy}>
          <Eye size={13} /> {busy ? 'Checking…' : 'Show caller details'}
        </Button>
        <span className="t-meta text-text-faint">Hidden by default; each reveal is recorded.</span>
        {error && <span role="alert" className="text-[12px] text-critical">{error}</span>}
      </div>
    );
  }
  const dialable = caller.phone && /^\+?[0-9 ()-]{6,}$/.test(caller.phone);
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <span className="font-semibold text-text">{caller.name || 'Unknown caller'}</span>
      {dialable ? (
        <CallNumber phone={caller.phone} label={caller.phone} iconSize={12}
          className="inline-flex items-center gap-1 font-mono text-signal-hover underline" />
      ) : (
        <span className="font-mono text-text-faint">{caller.phone || 'no number'}</span>
      )}
      <span className="t-meta text-text-faint">{caller.registered ? 'Registered account' : 'Unverified, typed by the caller'}</span>
    </div>
  );
}
