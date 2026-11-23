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

import React, { useRef, useState } from 'react';
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
      const approved = await api(`/api/incidents/${encodeURIComponent(ctx.id)}/approve`, {
        method: 'POST',
        body: {
          dispatcher_id: 'guided-demo',
          approved_hospital: ctx.plan?.recommended_hospital?.hospital_id ?? ctx.plan?.recommended_hospital?.name,
          approved_route: ctx.plan?.recommended_route?.route_id ?? 'recommended',
        },
      });
      ctx.unit = approved.assigned_unit ?? approved.citizen_view?.assigned_unit;
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
      if (!ctx.road) { // a retry must not close a second road
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
  ['citizen', 'Caller'],
  ['dispatcher', 'Dispatch'],
  ['driver', 'Ambulance crew'],
  ['hospital', 'ER desk'],
];

export default function GuidedDemo() {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [caption, setCaption] = useState('Press "Next step" to start the official scenario. Reset the demo first (dispatcher, control-room log) for a clean stage.');
  const [error, setError] = useState(null);
  const ctx = useRef({});
  const frames = useRef({});

  const running = useRef(false);

  const next = async () => {
    if (running.current || step >= STEPS.length) return;
    running.current = true;
    setBusy(true);
    setError(null);
    try {
      const current = STEPS[step];
      setCaption(`${current.title}…`);
      const text = await current.run(ctx.current);
      setCaption(text);
      for (const name of current.reload ?? []) {
        frames.current[name]?.contentWindow?.location.reload();
      }
      setStep(step + 1);
    } catch (err) {
      setError(err.message);
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const restart = () => {
    ctx.current = {};
    setStep(0);
    setError(null);
    setCaption('Ready. Press "Next step" to report a new emergency.');
  };

  return (
    <main className="min-h-screen bg-paper p-3 text-text">
      <section aria-label="Guided demo controls" className="mb-3 rounded-sm border border-rule bg-paper-sunk p-3">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-[15px] font-bold">Guided demo</h1>
          <ol className="flex flex-wrap gap-1.5 text-[11px]">
            {STEPS.map((s, i) => (
              <li key={s.title} aria-current={i === step ? 'step' : undefined}
                className={`rounded-sm border px-2 py-1 ${i < step ? 'border-rule text-text-faint line-through' : i === step ? 'border-ink font-semibold' : 'border-rule text-text-muted'}`}>
                {i + 1}. {s.title}
              </li>
            ))}
          </ol>
          <div className="ml-auto flex gap-2">
            {step >= STEPS.length
              ? <Button size="sm" onClick={restart}>Run again</Button>
              : <Button size="sm" onClick={next} disabled={busy}>{busy ? 'Working…' : 'Next step'}</Button>}
          </div>
        </div>
        <p role="status" aria-live="polite" className="mt-2 text-[14px] leading-relaxed">{caption}</p>
        {error && <p role="alert" className="mt-1 text-[13px] text-critical">{error}</p>}
      </section>
      <div className="grid gap-3 lg:grid-cols-2">
        {SCREENS.map(([name, label]) => (
          <figure key={name} className="overflow-hidden rounded-sm border border-rule">
            <figcaption className="border-b border-rule bg-paper-sunk px-3 py-1.5 text-[11px] font-bold uppercase tracking-[0.1em]">{label}</figcaption>
            <iframe
              ref={(el) => { frames.current[name] = el; }}
              title={`${label} screen`}
              src={`/${name}`}
              className="h-[460px] w-full bg-paper"
            />
          </figure>
        ))}
      </div>
    </main>
  );
}
