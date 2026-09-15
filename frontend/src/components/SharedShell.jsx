/**
 * Portal frame.
 *
 * A slim command bar rather than a marketing header. Polls /health and tells
 * the truth about routing tier, including offline A* and backend down.
 */

import React, { useEffect, useState } from 'react';
import { useAuth } from '../context/AuthContext';
import { useNavigate } from 'react-router-dom';
import { LogOut, Radio, Truck, PlusSquare, Heart, Activity } from 'lucide-react';

const PORTALS = {
  dispatcher: { title: 'City dispatch', sub: 'Bengaluru Central Control', Icon: Radio, glyph: 'D' },
  driver: { title: 'Field unit', sub: 'Navigation and handover', Icon: Truck, glyph: 'F' },
  hospital: { title: 'Emergency department', sub: 'Incoming patients and capacity', Icon: PlusSquare, glyph: 'E' },
  citizen: { title: 'Emergency assistance', sub: 'Report and track', Icon: Heart, glyph: 'C' },
};

export default function SharedShell({ children }) {
  const { role, driverInfo, hospitalInfo, logout } = useAuth();
  const navigate = useNavigate();
  const [health, setHealth] = useState(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const ping = async () => {
      try {
        const res = await fetch('/health');
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (!cancelled) {
          setHealth(data);
          setReachable(true);
        }
      } catch {
        if (!cancelled) setReachable(false);
      }
    };
    ping();
    const timer = setInterval(ping, 10000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const meta = PORTALS[role] ?? { title: 'RapidGrid', sub: 'Emergency response', Icon: Activity, glyph: 'R' };
  const Icon = meta.Icon;

  let title = meta.title;
  let sub = meta.sub;
  if (role === 'driver' && driverInfo) {
    title = `${driverInfo.driverName} · ${driverInfo.unitId}`;
    sub = driverInfo.hubName ?? meta.sub;
  } else if (role === 'hospital' && hospitalInfo?.name) {
    title = hospitalInfo.name;
    sub = meta.sub;
  }

  const status = health?.status;
  const live = status === 'healthy' && health?.routing?.live_api_key_configured;
  const degraded = status === 'degraded' || (reachable && health?.routing && !health.routing.live_api_key_configured);
  const tier = health === null
    ? { label: 'Connecting…', tone: 'warn' }
    : !reachable || status === 'unhealthy'
      ? { label: 'Backend offline', tone: 'bad' }
      : degraded
        ? { label: 'Offline A*', tone: 'warn' }
        : live
          ? { label: 'Live routing', tone: 'ok' }
          : { label: 'Backend online', tone: 'warn' };

  const tones = {
    ok: 'tier-live',
    warn: 'tier-offline',
    bad: 'tier-down',
  };

  return (
    <div className="flex min-h-screen flex-col bg-field">
      <header className="sticky top-0 z-50 border-b border-rule-ink bg-ink">
        <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-8 w-8 shrink-0 place-items-center rounded-sm bg-signal text-ink">
              <span className="font-mono text-[11px] font-bold leading-none">{meta.glyph}</span>
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="truncate font-display text-[14px] font-semibold leading-none tracking-[0.08em] text-on-ink uppercase">
                  {title}
                </h1>
                <span className="shrink-0 rounded-xs border border-rule-ink px-1.5 py-[3px] t-tag text-on-ink-muted">
                  {role}
                </span>
              </div>
              <p className="mt-1 truncate t-meta uppercase tracking-[0.12em] text-on-ink-muted">
                {sub}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <span
              className={`hidden items-center gap-1.5 rounded-xs px-2 py-1.5 t-tag sm:inline-flex ${tones[tier.tone]}`}
            >
              <span
                className={`inline-block h-1.5 w-1.5 rounded-full bg-current ${
                  tier.tone === 'ok' ? 'live-dot' : ''
                }`}
              />
              {tier.label}
            </span>
            <button
              onClick={() => {
                logout();
                navigate('/', { replace: true });
              }}
              className="tap inline-flex items-center gap-1.5 rounded-sm border border-rule-ink bg-transparent px-3 py-2.5 t-tag text-on-ink transition-colors hover:border-signal hover:text-signal lg:py-1.5"
            >
              <LogOut size={13} />
              <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1">{children}</main>
    </div>
  );
}
