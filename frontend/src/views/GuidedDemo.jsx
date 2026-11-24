/**
 * Guided demo: the official scenario on all four screens at once.
 *
 * Each "Next step" makes the same API calls a person would make on that
 * role's screen (report, approve, claim, stage a disruption, accept the
 * agent's route, pickup, handover), and the four screens below update
 * themselves. It acts as every role, so it needs the operator sign-in (or
 * the open local demo). It is a presenter's tool and a fallback if a live
 * click fails on stage; nothing here is hidden from the real screens.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '../components/ui';

const SCENE = { lat: 13.0358, lng: 77.597 }; // near Hebbal
const REPORT_TEXT = 'Demo: my father collapsed with severe chest pain and is barely conscious, near Hebbal flyover.';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(url, { method = 'GET', body, timeoutMs = 20000 } = {}) {
  let res;
  try {
    // Every request has a deadline, so a stalled one ends the step with an
    // error the presenter can retry, instead of "Working…" forever.
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    throw new Error(err?.name === 'TimeoutError' || err?.name === 'AbortError'
      ? 'The server did not answer in time. Press "Next step" to retry this step.'
      : 'Cannot reach the server. Press "Next step" to retry this step.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`;
    throw new Error(res.status === 401 || res.status === 403
      ? `${detail}. The guided demo needs the operator sign-in.` : detail);
  }
  return data;
}

async function waitFor(check, { timeoutMs = 60000, everyMs = 1000 } = {}) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > end) throw new Error('Timed out waiting; try the step again or continue on the screens.');
    await sleep(everyMs);
  }
}

const incident = (id) => api(`/api/incidents/${encodeURIComponent(id)}`);

/** Can this browser act as every role? Operator sign-in, or the open local
 * demo (no staff PINs). Checked before anything is created. */
async function canRunAllRoles() {
  const [session, staff] = await Promise.all([
    api('/api/session').catch(() => ({})),
    api('/api/staff/session').catch(() => ({})),
  ]);
  return Boolean(session.operator_authenticated) || staff.enabled === false;
}

/** The unit now on the call (a backup may have replaced the first one). */
async function currentUnit(ctx) {
  const inc = await incident(ctx.id);
  ctx.unit = inc.assigned_unit ?? ctx.unit;
  return inc;
}

const STEPS = [
  {
    title: 'A caller reports an emergency',
    run: async (ctx) => {
      if (!(await canRunAllRoles())) {
        throw new Error('The guided demo acts as every role, so it needs the operator sign-in. Nothing was created.');
      }
      if (!ctx.id) { // a retry after a timeout must not file a second report
        const created = await api('/api/incidents', {
          method: 'POST',
          body: { citizen_text: REPORT_TEXT, location: SCENE, anonymous: true, citizen_name: 'Guided demo' },
        });
        ctx.id = created.incident_id;
      }
      try { sessionStorage.setItem('rapidgrid_my_incident', ctx.id); } catch { /* storage off */ }
      const inc = await waitFor(async () => {
        const i = await incident(ctx.id);
        if (i.status === 'failed') throw new Error(i.error || 'Planning failed');
        return i.status === 'awaiting_dispatcher_approval' ? i : null;
      });
      ctx.plan = inc.action_plan;
      const h = ctx.plan?.recommended_hospital;
      return `Triage: ${inc.emergency_type ?? 'emergency'}. RapidGrid ranked hospitals by capability and modelled road time and recommends ${h?.name ?? 'a hospital'}. Nothing moves until dispatch approves.`;
    },
    reload: ['citizen', 'dispatcher'],
  },
  {
    title: 'Dispatch approves the plan',
    run: async (ctx) => {
      // A retry after a lost response: the call may already be approved
      // (approving again would be refused). Read it first.
      const before = await incident(ctx.id);
      if (before.status === 'awaiting_dispatcher_approval') {
        const approved = await api(`/api/incidents/${encodeURIComponent(ctx.id)}/approve`, {
          // (A 409 "no ambulance free" is explained below.)
          method: 'POST',
          body: {
            dispatcher_id: 'guided-demo',
            approved_hospital: ctx.plan?.recommended_hospital?.hospital_id ?? ctx.plan?.recommended_hospital?.name,
            approved_route: ctx.plan?.recommended_route?.route_id ?? 'recommended',
          },
        });
        ctx.unit = approved.assigned_unit ?? approved.citizen_view?.assigned_unit;
      } else {
        ctx.unit = before.assigned_unit ?? before.citizen_view?.assigned_unit;
      }
      if (!ctx.unit) throw new Error('No free unit was assigned; reset the demo and try again.');
      // The crew screen opens this call (not just the queue).
      try { sessionStorage.setItem('rapidgrid_driver_incident', ctx.id); } catch { /* storage off */ }
      return `${ctx.unit} is dispatched from its station. The caller's screen shows it on its way.`;
    },
    reload: ['citizen', 'driver', 'hospital'],
  },
  {
    title: 'The unit sets off; the agent starts watching',
    run: async (ctx) => {
      await api('/api/driver/claim', { method: 'POST', body: { incident_id: ctx.id, driver_id: ctx.unit } });
      const track = await api(`/api/incidents/${encodeURIComponent(ctx.id)}/track`).catch(() => ({}));
      await sleep(4000); // let it move a little
      const speed = track.sim_speed ? `x${track.sim_speed}` : 'faster than real time';
      return `${ctx.unit}'s crew confirmed. The monitoring agent compares its position with the planned route every second. Movement here is simulated (${speed}); a crew phone can share real GPS instead.`;
    },
    reload: ['driver'],
  },
  {
    title: 'A road ahead closes',
    run: async (ctx) => {
      // If the agent has already raised an alert for this unit (it can, on
      // its own), show that one: staging is refused while one is open.
      const already = ((await incident(ctx.id)).monitoring?.alerts ?? []).find((a) => a.status === 'open');
      if (!ctx.road && !already) { // a retry must not close a second road
        const staged = await api(`/api/monitoring/demo/${encodeURIComponent(ctx.id)}`, {
          method: 'POST', body: { action: 'close_road_ahead' },
        });
        ctx.road = staged.road || 'staged';
      }
      const alert = await waitFor(async () => {
        const i = await incident(ctx.id);
        return (i.monitoring?.alerts ?? []).find((a) => a.status === 'open');
      }, { timeoutMs: 30000 });
      ctx.alert = alert;
      return `The agent: ${alert.explanation}`;
    },
  },
  {
    title: "Dispatch follows the agent's advice",
    run: async (ctx) => {
      const alert = ctx.alert;
      const base = `/api/incidents/${encodeURIComponent(ctx.id)}/alerts/${encodeURIComponent(alert.alert_id)}`;
      if (alert.recommendation === 'dispatch_backup' && alert.backup) {
        if (!ctx.backupUnit) { // a retry of a failed claim must not dispatch again
          const sent = await api(`${base}/backup`, { method: 'POST' });
          ctx.backupUnit = sent.unit_id;
        }
        ctx.unit = ctx.backupUnit;
        await api('/api/driver/claim', { method: 'POST', body: { incident_id: ctx.id, driver_id: ctx.unit } });
        return `The agent advised a backup: ${ctx.unit} is closer now. Dispatch sent it and released the first unit. The decision is in the control-room log and the incident channel.`;
      }
      if (alert.routes?.length) {
        await api(`${base}/accept`, { method: 'POST', body: { route_index: 0 } });
        return `Dispatch accepted the recommended route. It goes to ${ctx.unit}'s screen and the caller's, and into the control-room log and the incident channel.`;
      }
      return 'The agent advised holding: no faster route or closer unit exists. Dispatch keeps the unit on its way; the decision stays with the dispatcher.';
    },
    reload: ['driver', 'citizen'],
  },
  {
    title: 'Pickup: hospitals ranked again',
    run: async (ctx) => {
      await currentUnit(ctx);
      await api('/api/driver/arrived_pickup', { method: 'POST', body: { incident_id: ctx.id, driver_id: ctx.unit } });
      const ranking = await waitFor(async () => (await incident(ctx.id)).pickup_ranking, { timeoutMs: 20000 })
        .catch(() => null);
      return ranking
        ? `Skipping ahead to the scene: patient aboard. ${ranking.summary}`
        : 'Skipping ahead to the scene: patient aboard; transport to the hospital begins.';
    },
    reload: ['driver', 'citizen', 'hospital'],
  },
  {
    title: 'Handover at the ER',
    run: async (ctx) => {
      await api(`/api/incidents/${encodeURIComponent(ctx.id)}/arrived?unit_type=primary`, { method: 'POST' });
      return 'The ER has the patient. The unit is free again, and the full story is in the control-room log.';
    },
    reload: ['driver', 'citizen', 'hospital', 'dispatcher'],
  },
];

const SCREENS = [
  ['citizen', 'Caller', "the caller's phone"],
  ['dispatcher', 'Dispatch', 'the dispatch console'],
  ['driver', 'Ambulance crew', "the ambulance crew's phone"],
  ['hospital', 'ER desk', "the hospital's ER desk"],
];

/**
 * For each step: which screen to show, what to point at on it, and what to
 * say, for someone who has never seen the app. `look` is a CSS selector
 * inside that screen (data-tour markers on the real screens).
 */
const TOUR = [
  {
    screen: 'citizen', look: '[data-tour="caller-status"]',
    what: 'Someone calls for help. RapidGrid reads their words, works out what kind of help is needed, ranks the hospitals by what they can treat, and plans the road route.',
    where: 'The call card: what was understood, and what happens next.',
  },
  {
    screen: 'dispatcher', look: '[data-tour="map"]',
    what: 'A dispatcher checks the plan and approves it. Only then does an ambulance move. Nothing is sent without a person agreeing.',
    where: 'The map: the route from the ambulance station to the caller, and on to the hospital.',
  },
  {
    screen: 'dispatcher', look: '[data-testid="agent-watching"]',
    what: "The ambulance sets off. From now on, the monitoring agent compares its position with the planned route every second. (On stage the movement is simulated, six times faster; a crew phone can share real GPS instead.)",
    where: 'What the agent sees right now: how far along it is, how far off the route, and the time still to go.',
  },
  {
    screen: 'dispatcher', look: '[data-tour="agent-alert"]', open: '[data-testid="agent-trace"]',
    what: 'A road on the route ahead is closed. Within seconds the agent raises an alert: the cause, the delay, the new arrival time, routes around it, and whether to keep this ambulance or send a closer one.',
    where: 'The red alert, and under it "How the agent decided": each step it took, with its numbers.',
  },
  {
    screen: 'driver', look: '[data-tour="crew-eta"]',
    what: "Dispatch follows the agent's advice. The new route goes straight to the ambulance crew and to the caller, and the decision is recorded.",
    where: "The crew's screen: the new route on the map and the updated time to go.",
  },
  {
    screen: 'driver', look: '[data-tour="pickup-ranking"]',
    what: 'The crew reaches the patient. RapidGrid ranks the hospitals again from the scene, because things may have changed since the call began.',
    where: 'The hospitals ranked again from the scene, with the chosen one marked.',
  },
  {
    screen: 'citizen', look: '[data-testid="call-summary"], [data-tour="caller-status"]',
    what: 'The patient is handed over at the ER. The ambulance is free again, and every decision is in the control-room log.',
    where: "The caller's screen closes the call with a short summary.",
  },
];

const TOUR_STYLE = `
.rg-tour-target { outline: 3px solid #e8a020 !important; outline-offset: 4px; border-radius: 6px;
  box-shadow: 0 0 0 9999px rgba(20, 18, 24, 0.18); position: relative; z-index: 5;
  animation: rg-tour-pulse 1.6s ease-in-out 3; }
@keyframes rg-tour-pulse { 50% { outline-color: rgba(232, 160, 32, 0.35); } }`;

/** Scroll to and outline the part of the (same-origin) screen to look at.
 * Waits for it, since the screen may still be loading or updating. */
async function spotlight(frame, selector, openSelector, isCurrent, staleDoc, zoom = '') {
  const end = Date.now() + 15000;
  while (Date.now() < end && isCurrent()) {
    const doc = frame?.contentDocument;
    // Wait for a screen that was reloaded to have its new page loaded (the
    // old one would get the outline and then vanish), and for the screen to
    // be shown (a hidden one measures as zero, so the scroll would be wrong).
    const ready = doc && doc !== staleDoc && doc.readyState === 'complete' && frame.offsetParent !== null;
    const el = ready ? doc.querySelector(selector) : null;
    if (el) {
      // Projector zoom first, so the scroll is measured on the final layout.
      if (doc.documentElement.style.zoom !== zoom) doc.documentElement.style.zoom = zoom;
      if (!doc.getElementById('rg-tour-style')) {
        const style = doc.createElement('style');
        style.id = 'rg-tour-style';
        style.textContent = TOUR_STYLE;
        doc.head.appendChild(style);
      }
      doc.querySelectorAll('.rg-tour-target').forEach((e) => e.classList.remove('rg-tour-target'));
      el.classList.add('rg-tour-target');
      if (openSelector) doc.querySelector(openSelector)?.setAttribute('open', '');
      // Scroll inside the screen only (scrollIntoView would also scroll this
      // page and push the guidance out of view).
      const win = doc.defaultView;
      const top = el.getBoundingClientRect().top;
      win.scrollBy({ top: top - win.innerHeight / 4, behavior: 'smooth' });
      return true;
    }
    await sleep(400);
  }
  return false;
}

const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : new URLSearchParams();
// Seconds to hold each finished step in "Play all" (?pause=N for rehearsals).
const PAUSE_S = Math.max(1, Number(params.get('pause')) || 8);
const PROJECTOR_ZOOM = '1.25';

export default function GuidedDemo() {
  const [projector, setProjector] = useState(params.get('projector') === '1');
  const projectorRef = useRef(projector);
  projectorRef.current = projector;
  const [autoplay, setAutoplay] = useState(false);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [caption, setCaption] = useState(null);
  const [error, setError] = useState(null);
  const [screen, setScreen] = useState('citizen');
  const ctx = useRef({});
  const frames = useRef({});
  const shown = useRef(0); // bumped on every change of focus, so an old spotlight stops

  const running = useRef(false);
  const done = step >= STEPS.length;
  const tour = TOUR[Math.min(step, STEPS.length - 1)];
  const lastDone = step > 0 ? TOUR[step - 1] : null;

  const [pointed, setPointed] = useState(false);
  const focus = (name, look, open, staleDoc = null) => {
    setScreen(name);
    setPointed(false);
    const mine = ++shown.current;
    if (look) {
      spotlight(frames.current[name], look, open, () => mine === shown.current, staleDoc,
        projectorRef.current ? PROJECTOR_ZOOM : '')
        .then((found) => { if (found && mine === shown.current) setPointed(true); });
    }
  };

  const next = async () => {
    if (running.current || step >= STEPS.length) return;
    running.current = true;
    setBusy(true);
    setError(null);
    const current = STEPS[step];
    const guide = TOUR[step];
    // Show the screen where this step happens while it runs.
    focus(guide.screen);
    try {
      setCaption(`${current.title}…`);
      const text = await current.run(ctx.current);
      setCaption(text);
      const before = frames.current[guide.screen]?.contentDocument ?? null;
      const reloaded = (current.reload ?? []).includes(guide.screen);
      for (const name of current.reload ?? []) {
        frames.current[name]?.contentWindow?.location.reload();
      }
      setStep(step + 1);
      focus(guide.screen, guide.look, guide.open, reloaded ? before : null);
    } catch (err) {
      setError(/is free right now/.test(err.message)
        ? 'No ambulance is free: earlier demo calls still hold them. Finish those calls, or reset the demo '
          + '(Dispatch tab, control-room log, "Reset demo"; on the hosted site, unlock it as the operator first), '
          + 'then press "Next step" again.'
        : err.message);
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const restart = () => {
    ctx.current = {};
    setStep(0);
    setError(null);
    setCaption(null);
    focus('citizen');
  };

  // Play all: run each step, hold it for PAUSE_S (longer on the alert, the
  // moment the audience needs to read), then the next; stop at the end or
  // on an error (the error stays on screen, "Next step" retries it).
  useEffect(() => {
    if (!autoplay || busy || error) return undefined;
    if (step >= STEPS.length) { setAutoplay(false); return undefined; }
    const hold = step === 0 ? 0 : (step === 4 ? 1.75 : 1) * PAUSE_S * 1000;
    const timer = setTimeout(() => { next(); }, hold);
    return () => clearTimeout(timer);
  }, [autoplay, busy, error, step]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (error) setAutoplay(false); }, [error]);

  // Projector mode: bigger text inside every screen (same-origin frames).
  const applyZoom = useCallback((frame) => {
    try {
      const root = frame?.contentDocument?.documentElement;
      if (root) root.style.zoom = projector ? PROJECTOR_ZOOM : '';
    } catch { /* not loaded yet */ }
  }, [projector]);
  useEffect(() => {
    Object.values(frames.current).forEach(applyZoom);
  }, [applyZoom]);

  const progress = Math.round((step / STEPS.length) * 100);
  // The screen the guidance is about (the step's), even if the viewer has
  // switched tabs since.
  const aboutScreen = busy ? TOUR[step]?.screen : (lastDone?.screen ?? screen);
  const screenLabel = SCREENS.find(([n]) => n === aboutScreen);

  return (
    <main className="min-h-screen bg-paper text-text">
      {/* Only this slim bar stays pinned (a phone keeps most of its height
          for the screen); the guidance below scrolls with the page. */}
      <div className="sticky top-0 z-10 border-b border-rule bg-paper-sunk px-3 py-2 sm:px-5">
        <div className={`mx-auto ${projector ? 'max-w-none' : 'max-w-[1100px]'}`}>
          <div className="flex items-center justify-between gap-2">
            <div>
              <h1 className="text-[15px] font-bold">Guided demo</h1>
              <p className="t-meta text-text-muted">
                {done ? 'All 7 steps done' : `Step ${step + 1} of ${STEPS.length}: ${STEPS[step].title}`}
              </p>
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="quiet" aria-pressed={projector} onClick={() => setProjector((v) => !v)}>
                {projector ? 'Exit projector' : 'Projector'}
              </Button>
              {!done && (
                <Button variant="quiet" aria-pressed={autoplay} onClick={() => { setError(null); setAutoplay((v) => !v); }}>
                  {autoplay ? 'Pause' : 'Play all'}
                </Button>
              )}
              {done
                ? <Button onClick={restart}>Run again</Button>
                : <Button onClick={next} disabled={busy || autoplay}>{busy ? 'Working…' : 'Next step'}</Button>}
            </div>
          </div>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-rule" aria-hidden="true">
            <div className="h-full bg-signal transition-all" style={{ width: `${progress}%` }} />
          </div>
          {projector && (
            // Projector: one large caption line in the bar; the screen fills the rest.
            <div data-testid="projector-caption" className="mt-2 grid gap-1 sm:grid-cols-[3fr_2fr] sm:gap-4">
              <p role="status" aria-live="polite" className="text-[20px] font-semibold leading-snug">
                {caption
                  // The full text is on the screen below; the bar keeps the
                  // first three sentences, readable from the back of a room.
                  // (No lookbehind: older Safari cannot parse it.)
                  ? caption.split(/\.\s+(?=[A-Z])/).slice(0, 3).join('. ').replace(/([^.])$/, '$1.')
                  : 'The official scenario: an ambulance is sent, a road on its way closes, and the agent works out what to do.'}
              </p>
              <p className="text-[16px] leading-snug text-text-muted">
                {lastDone && !busy ? <><span className="font-semibold text-signal-hover">Look at:</span> {lastDone.where}</> : null}
              </p>
            </div>
          )}
          {error && projector && <p role="alert" className="mt-1 text-[15px] text-critical">{error}</p>}
        </div>
      </div>
      {!projector && (
      <section aria-label="Guided demo controls" className="border-b border-rule bg-paper-sunk px-3 pb-3 sm:px-5">
        <div className="mx-auto max-w-[1100px]">

          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <div className="rounded-sm border border-rule bg-paper px-3 py-2">
              <div className="eyebrow">What is happening</div>
              <p role="status" aria-live="polite" className="mt-1 text-[14px] leading-relaxed">
                {caption ?? 'This tour plays the official scenario: an ambulance is sent to a caller, a road on its way closes, and the monitoring agent works out what to do. Press "Next step" to begin; each step shows the screen where it happens.'}
              </p>
              {lastDone && !busy && (
                <p className="mt-1.5 text-[12.5px] leading-relaxed text-text-muted">{lastDone.what}</p>
              )}
            </div>
            <div className="rounded-sm border border-signal-edge bg-signal-wash px-3 py-2">
              <div className="eyebrow">Where to look</div>
              <p className="mt-1 text-[14px] leading-relaxed">
                {lastDone && !busy
                  ? <>On <strong>{screenLabel?.[2]}</strong>: {lastDone.where}{' '}
                    {screen === lastDone.screen && pointed ? 'It is outlined in amber below.'
                      : <button type="button" className="font-semibold underline" onClick={() => focus(lastDone.screen, lastDone.look, lastDone.open)}>Show me</button>}</>
                  : busy
                    ? <>Watch <strong>{screenLabel?.[2]}</strong> below while this step runs.</>
                    : <>Below is <strong>{screenLabel?.[2]}</strong>. Use the tabs to see any of the four screens.</>}
              </p>
            </div>
          </div>
          {error && <p role="alert" className="mt-2 text-[13px] text-critical">{error}</p>}
          {/* On a phone the screen is below the guidance: one tap to it. */}
          <button
            type="button"
            className="mt-2 w-full rounded-sm border border-ink px-3 py-2 text-[13px] font-semibold sm:hidden"
            onClick={() => document.getElementById('tour-screen')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
          >
            See the screen ↓
          </button>
          {step === 0 && !busy && !caption && (
            <ol className="mt-3 grid gap-1 text-[12.5px] text-text-muted sm:grid-cols-2">
              {STEPS.map((s, i) => <li key={s.title}>{i + 1}. {s.title}</li>)}
            </ol>
          )}
          <p className="mt-2 hidden t-meta text-text-faint sm:block">Tip: reset the demo first (Dispatch screen, control-room log, "Reset demo") for a clean stage.</p>
        </div>
      </section>
      )}

      <nav id="tour-screen" aria-label="Screens" className={`mx-auto flex scroll-mt-16 gap-1 overflow-x-auto px-3 pt-3 sm:px-5 ${projector ? 'max-w-none' : 'max-w-[1100px]'}`}>
        {SCREENS.map(([name, label]) => (
          <button
            key={name}
            type="button"
            onClick={() => focus(name)}
            aria-pressed={screen === name}
            className={`shrink-0 rounded-t-sm border border-b-0 px-3 py-2 text-[12.5px] font-semibold ${
              screen === name ? 'border-ink bg-paper text-text' : 'border-rule bg-paper-sunk text-text-muted'}`}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className={`mx-auto px-3 pb-6 sm:px-5 ${projector ? 'max-w-none' : 'max-w-[1100px]'}`}>
        {SCREENS.map(([name, label]) => (
          <figure key={name} className={`overflow-hidden rounded-b-sm rounded-tr-sm border border-ink ${screen === name ? '' : 'hidden'}`}>
            <figcaption className="sr-only">{label}</figcaption>
            {/* All four stay loaded (so each keeps polling); only one is shown. */}
            <iframe
              ref={(el) => { frames.current[name] = el; }}
              title={`${label} screen`}
              src={`/${name}`}
              onLoad={(e) => applyZoom(e.currentTarget)}
              className={`w-full bg-paper ${projector ? 'h-[calc(100vh-190px)] min-h-[480px]' : 'h-[70vh] min-h-[420px] sm:h-[78vh]'}`}
            />
          </figure>
        ))}
      </div>
    </main>
  );
}
