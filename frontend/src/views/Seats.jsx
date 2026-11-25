/*
 * Choose your seat: five people, five laptops, five views of the same live
 * system. Each laptop remembers its seat, and every screen says whose it is.
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Heart, Map, PlusSquare, Radio, Truck } from 'lucide-react';

export const SEATS = [
  { id: 'caller', path: '/citizen', title: 'Caller', who: 'The person asking for help',
    does: 'Reports the emergency by voice or text, then follows the ambulance live.', Icon: Heart, color: '#c23a2b' },
  { id: 'dispatcher', path: '/dispatcher', title: 'Dispatcher', who: 'City control',
    does: 'Approves the plan, stages a road closure, and decides on the agent\'s alerts.', Icon: Radio, color: '#e8a020' },
  { id: 'crew', path: '/login?pick=driver', title: 'Ambulance crew', who: 'In the vehicle',
    does: 'Gets the call and the route, confirms pickup, can share live GPS from a phone.', Icon: Truck, color: '#1d5fd1' },
  { id: 'er', path: '/login?pick=hospital', title: 'ER desk', who: 'The receiving hospital',
    does: 'Sees the patient coming, accepts the handover, can go on divert.', Icon: PlusSquare, color: '#178a4a' },
  { id: 'control', path: '/control', title: 'City control room', who: 'The projector',
    does: 'The whole city at once: every hospital, station, vehicle and emergency.', Icon: Map, color: '#2e2c32' },
];

const KEY = 'rapidgrid.seat';

export function currentSeat() {
  try {
    return SEATS.find((s) => s.id === window.localStorage.getItem(KEY)) ?? null;
  } catch {
    return null;
  }
}

export function chooseSeat(id) {
  try {
    if (id) window.localStorage.setItem(KEY, id);
    else window.localStorage.removeItem(KEY);
  } catch { /* storage blocked: the seat lasts for this page only */ }
}

export default function Seats() {
  const navigate = useNavigate();
  const mine = currentSeat();
  return (
    <main className="mx-auto max-w-[1100px] px-4 py-8">
      <h1 className="font-display text-[clamp(24px,4vw,34px)] font-extrabold">Choose your seat</h1>
      <p className="mt-2 max-w-[640px] text-text-muted">
        One live system, five points of view. Each person opens RapidGrid on their own laptop or phone and takes one seat;
        what one does appears on the others within seconds.
      </p>
      <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="seats">
        {SEATS.map(({ id, path, title, who, does, Icon, color }, n) => (
          <button key={id} type="button"
            onClick={() => { chooseSeat(id); navigate(path); }}
            className={`tap flex flex-col items-start gap-2 rounded-md border-2 bg-paper p-5 text-left transition-colors hover:border-ink ${mine?.id === id ? 'border-ink' : 'border-rule'}`}>
            <span className="flex items-center gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-full text-white" style={{ background: color }}>
                <Icon size={20} aria-hidden />
              </span>
              <span>
                <span className="block t-meta uppercase tracking-[0.1em] text-text-muted">Seat {n + 1} · {who}</span>
                <span className="block font-display text-[19px] font-extrabold">{title}</span>
              </span>
            </span>
            <span className="text-[13.5px] text-text-muted">{does}</span>
            {mine?.id === id && <span className="t-tag text-signal-hover">This laptop's seat</span>}
          </button>
        ))}
      </div>
      <p className="mt-6 text-[13px] text-text-muted">
        To run the whole story on one screen instead, use the <a className="underline" href="/demo">guided demo</a>.
      </p>
    </main>
  );
}

/** A strip on every screen of a laptop that took a seat. */
export function SeatBanner() {
  const navigate = useNavigate();
  const seat = currentSeat();
  // Not inside the guided demo's frames: that page shows every seat at once.
  let framed = false;
  try { framed = window.self !== window.top; } catch { framed = true; }
  if (!seat || framed) return null;
  return (
    <div data-testid="seat-banner" className="flex items-center justify-between gap-3 px-4 py-1.5 text-[12.5px] font-semibold text-white"
      style={{ background: seat.color }}>
      <span>You are: {seat.title.toUpperCase()} <span className="font-normal opacity-90">· {seat.who}</span></span>
      <button type="button" className="underline underline-offset-2" onClick={() => navigate('/seats')}>Change seat</button>
    </div>
  );
}
