/**
 * Role selection and staff sign-in.
 *
 * Units and hospitals come from the server's contacts directory (never phone
 * numbers or PINs). When the directory holds staff PINs, the dispatcher, each
 * unit and each hospital sign in with their own 6-digit PIN; the server then
 * scopes every staff action to that sign-in. Without PINs (a local demo),
 * choosing a role grants it, as before.
 */

import React, { useEffect, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { useNavigate } from 'react-router-dom';
import { Radio, Truck, PlusSquare, Heart, ChevronRight, ChevronLeft, Info } from 'lucide-react';
import { Panel, PanelHead } from '../components/ui';
import CitizenAccess from '../components/CitizenAccess';

const ROLES = [
  {
    id: 'citizen',
    Icon: Heart,
    title: 'Citizen',
    glyph: 'C',
    blurb: 'Report an emergency and follow the response.',
    needsProfile: true,
  },
  {
    id: 'dispatcher',
    Icon: Radio,
    title: 'City dispatch',
    glyph: 'D',
    blurb: 'Review recommendations, approve or override, dispatch units.',
  },
  {
    id: 'driver',
    Icon: Truck,
    title: 'Field unit',
    glyph: 'F',
    blurb: 'Navigate to the patient, then to the receiving hospital.',
    needsProfile: true,
  },
  {
    id: 'hospital',
    Icon: PlusSquare,
    title: 'Emergency department',
    glyph: 'E',
    blurb: 'See inbound patients and set ICU capacity.',
    needsProfile: true,
  },
];


const TYPE_LABEL = {
  Ambulance: 'Ambulance',
  'Fire Engine': 'Fire engine',
  'Police Cruiser': 'Police unit',
  'Disaster Rescue': 'Rescue unit',
};

function PinForm({ label, onSubmit, onCancel }) {
  const [pin, setPin] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(pin);
    } catch (err) {
      setError(err.message || 'Sign-in failed.');
      setPin('');
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="space-y-3 px-4 py-4">
      <label htmlFor="staff-pin" className="eyebrow block">
        {label}: PIN (6 to 12 digits)
      </label>
      <input
        id="staff-pin"
        type="password"
        inputMode="numeric"
        autoComplete="current-password"
        pattern="[0-9]{6,12}"
        maxLength={12}
        required
        value={pin}
        onChange={(e) => setPin(e.target.value.replace(/[^0-9]/g, ''))}
        className="w-full rounded-sm border border-rule bg-paper px-3 py-2.5 font-mono text-[16px] tracking-[0.3em] text-text"
      />
      {error && (
        <p role="alert" className="text-[12.5px] text-critical">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy || pin.length !== 6}
          className="flex-1 rounded-sm bg-ink px-3 py-2.5 text-[13px] font-semibold text-on-ink disabled:opacity-50"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="rounded-sm border border-rule px-3 py-2.5 text-[13px] text-text disabled:opacity-50"
        >
          Back
        </button>
      </div>
    </form>
  );
}

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [picking, setPicking] = useState(null);
  const [directory, setDirectory] = useState({ enabled: false, units: [], hospitals: [] });
  // Staff roles open without a PIN only once the server has said sign-in is
  // off; while loading, or if the check failed, they stay closed.
  const [directoryState, setDirectoryState] = useState('loading'); // loading | ready | error
  const [needsPin, setNeedsPin] = useState(null); // { role, subject, profile, label }

  useEffect(() => {
    let cancelled = false;
    fetch('/api/staff/directory')
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error())))
      .then((data) => {
        if (cancelled) return;
        setDirectory(data);
        setDirectoryState('ready');
      })
      .catch(() => { if (!cancelled) setDirectoryState('error'); });
    return () => { cancelled = true; };
  }, []);

  const units = directory.units.map((u) => ({
    unitId: u.unit_id,
    vehicleType: u.type,
    driverName: TYPE_LABEL[u.type] ?? u.type,
    hubName: u.station,
  }));
  const hospitals = directory.enabled
    ? directory.hospitals.map((h) => ({ id: h.hospital_id, name: h.name }))
    : [{ id: 'all', name: 'All receiving facilities' },
       ...directory.hospitals.map((h) => ({ id: h.hospital_id, name: h.name }))];

  const signIn = async (role, subject, pin) => {
    const res = await fetch('/api/staff/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, subject, pin }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Sign-in failed (HTTP ${res.status})`);
  };

  // Staff roles need a PIN once the server has staff sign-in on.
  const enterStaff = (role, subject, profile, label) => {
    if (directoryState !== 'ready') return;
    if (directory.enabled) setNeedsPin({ role, subject, profile, label });
    else enter(role === 'unit' ? 'driver' : role, profile);
  };

  const enter = (roleId, profile = null) => {
    login(roleId, profile);
    navigate(`/${roleId}`);
  };

  const choose = (role) => {
    if (role.needsProfile) setPicking(role.id);
    else if (role.id === 'dispatcher') enterStaff('dispatcher', 'dispatch', null, 'City dispatch');
    else enter(role.id);
  };

  const OptionList = ({ items, render, onPick, getKey }) => (
    <div className="divide-y divide-rule">
      {items.map((item, i) => (
        <button
          key={getKey ? getKey(item) : i}
          onClick={() => onPick(item)}
          className="flex w-full items-center justify-between gap-3 px-4 py-3.5 text-left transition-colors hover:bg-paper-hover"
        >
          {render(item)}
          <ChevronRight size={15} className="shrink-0 text-text-faint" />
        </button>
      ))}
    </div>
  );

  return (
    <div className="min-h-screen bg-field">
      <div className="mx-auto max-w-[640px] px-4 py-10">
        <header className="mb-7">
          <div className="flex items-center gap-2.5">
            <div className="grid h-9 w-9 place-items-center rounded-sm bg-signal font-mono text-[13px] font-bold text-ink">
              R
            </div>
            <span className="font-display text-[17px] font-semibold tracking-[0.08em] text-text uppercase">
              RapidGrid
            </span>
          </div>
          <h1 className="mt-5 font-display text-[clamp(24px,6vw,32px)] font-semibold leading-tight text-text">
            {picking ? 'Choose a profile' : 'Choose a portal'}
          </h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-text-muted">
            {picking
              ? 'Pick who you are signing in as for this session.'
              : directory.enabled
                ? 'Four roles share one incident. Staff sign in with their own PIN.'
                : 'Four roles share one incident. Open several tabs to watch a call move between them. No credential check.'}
          </p>
        </header>

        {directoryState === 'error' && (
          <p role="alert" className="mb-3 rounded-sm border border-critical bg-critical-wash px-3 py-2 text-[12.5px] text-critical">
            Could not reach the server to check staff sign-in. Staff portals stay closed; reload to try again.
          </p>
        )}
        {needsPin ? (
          <Panel className="overflow-hidden">
            <PanelHead label="Staff sign-in" />
            <PinForm
              label={needsPin.label}
              onCancel={() => setNeedsPin(null)}
              onSubmit={async (pin) => {
                // Use the selection captured now; Back is disabled while busy.
                const chosen = needsPin;
                await signIn(chosen.role, chosen.subject, pin);
                setNeedsPin(null);
                enter(chosen.role === 'unit' ? 'driver' : chosen.role, chosen.profile);
              }}
            />
          </Panel>
        ) : !picking ? (
          <>
            <div className="space-y-2">
              {ROLES.map((role) => (
                <button
                  key={role.id}
                  onClick={() => choose(role)}
                  className="flex w-full items-center gap-4 rounded-md border border-rule bg-paper p-4 text-left transition-colors hover:border-ink hover:bg-paper-hover"
                >
                  <div className="grid h-10 w-10 shrink-0 place-items-center rounded-sm bg-ink font-mono text-[13px] font-bold text-signal">
                    {role.glyph}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-[14px] font-semibold text-text">{role.title}</div>
                    <p className="mt-0.5 text-[12px] leading-relaxed text-text-muted">
                      {role.blurb}
                    </p>
                  </div>
                  <ChevronRight size={16} className="shrink-0 text-text-faint" />
                </button>
              ))}
            </div>

            <div className="mt-5 flex items-start gap-2.5 rounded-sm border border-rule bg-paper-sunk px-3.5 py-3">
              <Info size={14} className="mt-px shrink-0 text-text-faint" />
              <p className="text-[11.5px] leading-relaxed text-text-muted">
                {directory.enabled ? (
                  <>
                    <span className="font-semibold text-text">Staff sign-in.</span> Dispatch, each unit
                    and each hospital use their own PIN; the server limits every action to that
                    role.
                  </>
                ) : (
                  <>
                    <span className="font-semibold text-text">Demonstration sign-in.</span> Selecting a
                    role grants it; there is no password check. Staff PINs switch this on.
                  </>
                )}
              </p>
            </div>
          </>
        ) : (
          <>
            <button
              onClick={() => setPicking(null)}
              className="tap mb-3 inline-flex items-center gap-1 py-1 t-tag text-text-muted hover:text-text"
            >
              <ChevronLeft size={14} /> All portals
            </button>

            <Panel className="overflow-hidden">
              {picking === 'citizen' && (
                <>
                  <PanelHead label="Citizen" />
                  <CitizenAccess
                    onSignedIn={(profile) => enter('citizen', profile)}
                    onAnonymous={async () => {
                      // A shared phone: end any earlier caller's session first,
                      // and go on only if that worked.
                      const res = await fetch('/api/citizens/session', { method: 'DELETE' });
                      if (!res.ok) throw new Error('Could not end the earlier sign-in on this phone. Try again.');
                      try { sessionStorage.removeItem('rapidgrid_my_incident'); } catch { /* storage off */ }
                      enter('citizen', { name: 'Anonymous', phone: null, registered: false, anonymousChoice: true });
                    }}
                  />
                </>
              )}

              {picking === 'driver' && (
                <>
                  <PanelHead label="Responding unit" />
                  <OptionList
                    items={units}
                    onPick={(u) => enterStaff('unit', u.unitId, u, u.unitId)}
                    getKey={(u) => u.unitId}
                    render={(u) => (
                      <span className="min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="font-mono text-[12px] font-bold text-text">
                            {u.unitId}
                          </span>
                          <span className="rounded-xs border border-rule px-1.5 py-[3px] t-tag text-text-muted">
                            {u.vehicleType}
                          </span>
                        </span>
                        <span className="mt-1 block text-[12px] text-text-muted">{u.hubName}</span>
                      </span>
                    )}
                  />
                </>
              )}

              {picking === 'hospital' && (
                <>
                  <PanelHead label="Emergency department" />
                  <OptionList
                    items={hospitals}
                    onPick={(h) => (h.id === 'all' ? enter('hospital', h) : enterStaff('hospital', h.id, h, h.name))}
                    getKey={(h) => h.id}
                    render={(h) => (
                      <span className="min-w-0">
                        <span className="block text-[13px] font-semibold text-text">{h.name}</span>
                      </span>
                    )}
                  />
                </>
              )}
            </Panel>
          </>
        )}
      </div>
    </div>
  );
}
