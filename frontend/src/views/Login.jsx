/**
 * Role selection.
 *
 * Demonstration sign-in: choosing a role grants it, with no credential check.
 * Profiles carry demo phone numbers for click-to-call.
 */

import React, { useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { useNavigate } from 'react-router-dom';
import { Radio, Truck, PlusSquare, Heart, ChevronRight, ChevronLeft, Info } from 'lucide-react';
import { Panel, PanelHead } from '../components/ui';

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

const UNITS = [
  { unitId: 'AMB-UNIT-04', vehicleType: 'Ambulance', driverName: 'Suresh Kumar', hubName: 'Aster CMI Emergency Ambulance Hub', phone: '+91 98001 10004' },
  { unitId: 'AMB-UNIT-01', vehicleType: 'Ambulance', driverName: 'Ramesh Gowda', hubName: 'Manipal Ambulance Base Depot', phone: '+91 98001 10001' },
  { unitId: 'AMB-UNIT-02', vehicleType: 'Ambulance', driverName: 'Vijay Naik', hubName: 'Victoria Hospital Paramedic Base', phone: '+91 98001 10002' },
  { unitId: 'FIRE-UNIT-09', vehicleType: 'Fire Engine', driverName: 'Capt. Rajesh Rao', hubName: 'Hebbal Fire & Rescue Station #4', phone: '+91 98001 20009' },
  { unitId: 'POLICE-UNIT-02', vehicleType: 'Police Cruiser', driverName: 'Insp. Vikram Singh', hubName: 'Hebbal Police Patrol Base', phone: '+91 98001 30002' },
  { unitId: 'RESCUE-UNIT-01', vehicleType: 'Disaster Rescue', driverName: 'Cmdr. Arjun Reddy', hubName: 'NDRF Disaster Rescue Hub North', phone: '+91 98001 40001' },
];

const CITIZENS = [
  { name: 'Ananya Sharma', phone: '+91 98765 43210' },
  { name: 'Rahul Verma', phone: '+91 91234 56789' },
  { name: 'Priya Patel', phone: '+91 99887 76655' },
];

const HOSPITALS = [
  { id: 'all', name: 'All receiving facilities', phone: null },
  { id: 'blr-007', name: 'Aster CMI Hospital (Hebbal)', phone: '+91 80 4342 0107' },
  { id: 'blr-001', name: 'Manipal Hospital (Old Airport Road)', phone: '+91 80 2502 4444' },
  { id: 'blr-002', name: 'Fortis Hospital (Bannerghatta Road)', phone: '+91 80 6621 4444' },
  { id: 'blr-019', name: 'Sri Jayadeva Institute of Cardiovascular Sciences', phone: '+91 80 2297 7200' },
  { id: 'blr-010', name: 'NIMHANS', phone: '+91 80 2699 5000' },
  { id: 'blr-005', name: "St. John's Medical College Hospital", phone: '+91 80 2206 5000' },
  { id: 'blr-022', name: 'Victoria Hospital (BMCRI)', phone: '+91 80 2670 1150' },
];

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [picking, setPicking] = useState(null);

  const enter = (roleId, profile = null) => {
    login(roleId, profile);
    navigate(`/${roleId}`);
  };

  const choose = (role) => {
    if (role.needsProfile) setPicking(role.id);
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
              : 'Four roles share one incident. Open several tabs to watch a call move between them. No credential check.'}
          </p>
        </header>

        {!picking ? (
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
                <span className="font-semibold text-text">Demonstration sign-in.</span> Selecting a
                role grants it — there is no password check, and no identity is verified. A
                production deployment would put real authentication and authorisation here.
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
                  <PanelHead label="Citizen profile" />
                  <OptionList
                    items={CITIZENS}
                    onPick={(c) => enter('citizen', c)}
                    getKey={(c) => c.phone}
                    render={(c) => (
                      <span className="min-w-0">
                        <span className="block text-[13.5px] font-semibold text-text">
                          {c.name}
                        </span>
                        <span className="mt-0.5 block font-mono text-[11px] text-text-faint">
                          {c.phone}
                        </span>
                      </span>
                    )}
                  />
                </>
              )}

              {picking === 'driver' && (
                <>
                  <PanelHead label="Responding unit" />
                  <OptionList
                    items={UNITS}
                    onPick={(u) => enter('driver', u)}
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
                        <span className="mt-1 block text-[12px] text-text-muted">
                          {u.driverName} · {u.hubName}
                        </span>
                        <span className="mt-0.5 block font-mono text-[11px] text-text-faint">
                          {u.phone}
                        </span>
                      </span>
                    )}
                  />
                </>
              )}

              {picking === 'hospital' && (
                <>
                  <PanelHead label="Emergency department" />
                  <OptionList
                    items={HOSPITALS}
                    onPick={(h) => enter('hospital', h)}
                    getKey={(h) => h.id}
                    render={(h) => (
                      <span className="min-w-0">
                        <span className="block text-[13px] font-semibold text-text">{h.name}</span>
                        {h.phone && (
                          <span className="mt-0.5 block font-mono text-[11px] text-text-faint">
                            {h.phone}
                          </span>
                        )}
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
