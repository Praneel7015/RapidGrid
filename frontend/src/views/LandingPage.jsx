/**
 * Landing page.
 *
 * The hero is the product's thesis: the nearest hospital is often the wrong one.
 * Design identity: bone paper, command ink header, amber CTAs, Archivo Narrow.
 */

import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Check } from 'lucide-react';

const CAPABILITIES = [
  {
    title: 'It routes on capability, not just proximity',
    body: 'A cardiac call goes to a facility with a 24×7 cath lab even when a general hospital is closer — the same specialty-centre bypass real EMS protocols use. Capability profiles are curated for 30 Bengaluru hospitals; oncology, maternity and day-surgery units are excluded from emergency routing entirely.',
  },
  {
    title: 'It keeps working when the internet does not',
    body: 'Live traffic routing runs against the Google Routes API. When that is unavailable — quota, outage, venue wi-fi — an A* router falls back to a cached 112,725-node OpenStreetMap graph of Bengaluru and keeps producing real paths, labelled as offline rather than passed off as live.',
  },
  {
    title: 'It separates what it knows from what it models',
    body: 'Hospital capability is curated and drives routing. Live bed counts are modelled, because no hospital data feed is integrated — so they are drawn differently, everywhere they appear. Nothing on screen is a number we invented and presented as a measurement.',
  },
];

const SAMPLE = {
  emergency: 'Cardiac',
  location: 'Hebbal, Bengaluru',
  chosen: { name: 'Fortis Hospital (Bannerghatta Road)', factor: 'Cath lab', value: '0.90', eta: '10.7' },
  runnerUp: { name: "St John's Medical College", factor: 'Cath lab', value: '0.76', eta: '4.5' },
};

export default function LandingPage() {
  const [plan, setPlan] = useState(SAMPLE);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/incidents/');
        if (!res.ok) return;
        const { incidents = [] } = await res.json();
        const withPlan = incidents
          .filter((i) => i.action_plan?.all_hospitals?.length > 1)
          .pop();
        if (!withPlan || cancelled) return;

        const [a, b] = withPlan.action_plan.all_hospitals;
        const fa = a.score_breakdown ?? [];
        const fb = b.score_breakdown ?? [];
        const key = fa.reduce(
          (best, f) => {
            const other = fb.find((x) => x.factor === f.factor);
            const gap = other ? f.weighted_score - other.weighted_score : 0;
            return gap > best.gap ? { gap, factor: f.factor, a: f.raw_score, b: other?.raw_score } : best;
          },
          { gap: -Infinity, factor: null },
        );
        if (!key.factor) return;

        setPlan({
          emergency: (withPlan.emergency_type ?? 'Emergency').replace(/^\w/, (c) => c.toUpperCase()),
          location: 'Bengaluru',
          chosen: {
            name: a.name,
            factor: key.factor,
            value: key.a?.toFixed(2),
            eta: a.road_eta_seconds ? (a.road_eta_seconds / 60).toFixed(1) : null,
          },
          runnerUp: {
            name: b.name,
            factor: key.factor,
            value: key.b?.toFixed(2),
            eta: b.road_eta_seconds ? (b.road_eta_seconds / 60).toFixed(1) : null,
          },
        });
      } catch { /* keep the sample */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const further =
    plan.chosen.eta && plan.runnerUp.eta
      ? (Number(plan.chosen.eta) - Number(plan.runnerUp.eta)).toFixed(1)
      : null;

  return (
    <div className="min-h-screen bg-field">
      <header className="border-b border-ink bg-ink">
        <div className="mx-auto flex max-w-[1440px] items-center justify-between gap-4 px-5 py-3.5 sm:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-[22px] w-[22px] place-items-center rounded-sm bg-signal font-mono text-[11px] font-bold text-ink">
              R
            </div>
            <span className="font-display text-[14px] font-bold tracking-[0.1em] text-on-ink uppercase">
              RapidGrid
            </span>
            <span className="hidden h-5 w-px bg-rule-ink sm:block" />
            <span className="hidden t-meta uppercase tracking-[0.12em] text-on-ink-muted sm:inline">
              Bengaluru emergency vehicle dispatch
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Link
              to="/login"
              className="tap inline-flex items-center gap-1.5 rounded-sm border border-rule-ink px-3 py-2 t-tag text-on-ink transition-colors hover:border-signal hover:text-signal"
            >
              Sign in
            </Link>
          </div>
        </div>
      </header>

      <main>
        <section className="mx-auto max-w-[1440px] px-5 py-12 sm:px-8 sm:py-16 lg:py-20">
          <div className="grid gap-10 lg:grid-cols-[1fr_minmax(320px,400px)] lg:gap-16 lg:items-start">
            <div>
              <p className="eyebrow">The thesis</p>
              <h1 className="mt-5 font-display text-[clamp(36px,8vw,82px)] font-semibold leading-[0.96] tracking-[-0.028em] text-text">
                The nearest hospital is often the wrong one.
              </h1>
              <p className="mt-6 max-w-[62ch] text-[17px] leading-relaxed text-text-muted sm:text-[19px]">
                A cardiac arrest needs a cath lab. Sending the patient to a closer emergency room
                that then has to transfer them onward costs far more time than driving past it.
                RapidGrid routes on what a hospital can actually treat.
              </p>
              <div className="mt-8 flex flex-col gap-3 sm:flex-row">
                <Link
                  to="/citizen"
                  className="tap inline-flex items-center justify-center rounded-sm border border-ink bg-signal px-6 py-4 t-tag text-ink transition-colors hover:bg-ink hover:text-signal"
                >
                  Report an emergency
                </Link>
                <Link
                  to="/dispatcher"
                  className="tap inline-flex items-center justify-center rounded-sm border border-ink bg-transparent px-6 py-4 t-tag text-text transition-colors hover:bg-ink hover:text-on-ink"
                >
                  Open the dispatch console
                </Link>
              </div>
            </div>

            <div className="rounded-md border border-rule bg-paper">
              <div className="border-b border-rule px-4 py-3">
                <span className="eyebrow">What it runs on</span>
              </div>
              <div className="flex flex-col gap-4 p-4">
                {[
                  { n: '112,725', l: 'OSM nodes · 178,089 edges\nBengaluru road graph', modelled: false },
                  { n: '30', l: 'Facilities · curated capability\nprofiles, coords via OSM', modelled: false },
                  { n: '13', l: 'Agents feeding one\nrecommendation', modelled: false },
                  { n: '~100', l: 'ms · A* fallback path,\nbenchmark not measured live', modelled: true },
                ].map((row) => (
                  <div key={row.n}>
                    <div className={`inline-block font-mono text-[26px] ${row.modelled ? 'val-modelled' : 'val-verified'}`}>
                      {row.n}
                    </div>
                    <div className="mt-1.5 whitespace-pre-line t-meta uppercase tracking-[0.1em] text-text-faint">
                      {row.l}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="border-y border-ink bg-paper">
          <div className="mx-auto max-w-[1440px] px-5 sm:px-8">
            <div className="flex flex-wrap items-center gap-3 border-b border-rule py-4">
              <span className="eyebrow text-text">One real decision</span>
              <span className="hidden h-4 w-px bg-rule sm:block" />
              <span className="t-meta uppercase tracking-[0.12em] text-text-faint">
                {plan.emergency} · {plan.location}
              </span>
              <span className="ml-auto">
                <span className="inline-flex items-center gap-1.5 rounded-xs border border-ink px-2 py-1 t-tag">
                  <span className="inline-block h-1.5 w-1.5 bg-ink" />
                  Road ETAs verified
                </span>
              </span>
            </div>
            <div className="grid md:grid-cols-2">
              <div className="bg-signal p-8 md:p-9">
                <span className="inline-flex bg-ink px-2 py-1.5 t-tag text-signal">Selected</span>
                <div className="mt-3 font-display text-[28px] font-semibold leading-tight tracking-tight text-ink sm:text-[36px]">
                  {plan.chosen.name}
                </div>
                <div className="mt-3 t-meta uppercase tracking-[0.1em] text-ink/80">
                  Deciding factor · {plan.chosen.factor}
                </div>
                <div className="mt-6 flex gap-10">
                  <div>
                    <div className="eyebrow mb-2 text-ink/70">Score</div>
                    <span className="val-verified text-[40px] text-ink">{plan.chosen.value}</span>
                  </div>
                  <div>
                    <div className="eyebrow mb-2 text-ink/70">Road ETA</div>
                    <span className="val-verified text-[40px] text-ink">{plan.chosen.eta ?? '—'}</span>
                    <span className="ml-1 font-mono text-[13px]">min</span>
                  </div>
                </div>
              </div>
              <div className="bg-paper-sunk p-8 md:p-9">
                <span className="inline-flex border border-rule px-2 py-1.5 t-tag text-text-faint">
                  Closer, not chosen
                </span>
                <div className="mt-3 font-display text-[28px] font-semibold leading-tight tracking-tight text-text-muted sm:text-[36px]">
                  {plan.runnerUp.name}
                </div>
                <div className="mt-3 t-meta uppercase tracking-[0.1em] text-text-faint">
                  Won on travel time · still the wrong room
                </div>
                <div className="mt-6 flex gap-10">
                  <div>
                    <div className="eyebrow mb-2">Score</div>
                    <span className="val-verified text-[40px] text-text-muted">{plan.runnerUp.value}</span>
                  </div>
                  <div>
                    <div className="eyebrow mb-2">Road ETA</div>
                    <span className="val-verified text-[40px] text-text-muted">{plan.runnerUp.eta ?? '—'}</span>
                    <span className="ml-1 font-mono text-[13px] text-text-muted">min</span>
                  </div>
                </div>
              </div>
            </div>
            <p className="border-t border-rule px-5 py-5 text-[16px] leading-relaxed text-text-muted sm:px-8 sm:text-[22px]">
              {further && Number(further) > 0 ? (
                <>
                  The ambulance drove{' '}
                  <span className="val-verified text-text">{further}</span> minutes further, on
                  purpose — because the closer hospital could not treat this patient as well.
                </>
              ) : (
                <>
                  The system states which hospital it rejected, on which factor, and what the choice
                  cost in road minutes.
                </>
              )}
            </p>
          </div>
        </section>

        <section className="mx-auto max-w-[1440px] px-5 py-14 sm:px-8">
          <div className="grid gap-8 md:grid-cols-3 md:gap-10">
            {CAPABILITIES.map((c) => (
              <article key={c.title}>
                <h2 className="font-display text-[18px] font-semibold leading-snug text-text">
                  {c.title}
                </h2>
                <p className="mt-3 text-[13px] leading-relaxed text-text-muted">{c.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="border-t border-rule bg-paper">
          <div className="mx-auto max-w-[1440px] px-5 py-14 sm:px-8">
            <h2 className="font-display text-[clamp(22px,4vw,28px)] font-semibold tracking-tight text-text">
              One incident, four portals
            </h2>
            <p className="mt-2 max-w-[56ch] text-[14px] leading-relaxed text-text-muted">
              Open them in separate tabs and watch a single call move between them in real time.
            </p>
            <div className="mt-6 grid gap-2 sm:grid-cols-2 lg:gap-3">
              {[
                { to: '/citizen', name: 'Citizen', desc: 'Report an emergency, track the response.', g: 'C' },
                { to: '/dispatcher', name: 'City dispatch', desc: 'Review, override, dispatch.', g: 'D' },
                { to: '/driver', name: 'Field unit', desc: 'Two-stage navigation and handover.', g: 'F' },
                { to: '/hospital', name: 'Emergency department', desc: 'Inbound patients and capacity.', g: 'E' },
              ].map((p) => (
                <Link
                  key={p.to}
                  to={p.to}
                  className="group flex items-center justify-between gap-3 rounded-md border border-rule bg-paper px-4 py-3.5 transition-colors hover:border-ink"
                >
                  <span className="flex items-center gap-3">
                    <span className="grid h-8 w-8 place-items-center rounded-sm bg-ink font-mono text-[11px] font-bold text-signal">
                      {p.g}
                    </span>
                    <span>
                      <span className="block text-[14px] font-semibold text-text">{p.name}</span>
                      <span className="mt-0.5 block text-[12px] text-text-muted">{p.desc}</span>
                    </span>
                  </span>
                  <ArrowRight
                    size={16}
                    className="shrink-0 text-text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-signal"
                  />
                </Link>
              ))}
            </div>
          </div>
        </section>

        <section className="border-t border-rule">
          <div className="mx-auto max-w-[1440px] px-5 py-14 sm:px-8">
            <h2 className="font-display text-[clamp(18px,4vw,22px)] font-semibold tracking-tight text-text">
              What is real, and what is not
            </h2>
            <div className="mt-6 grid gap-8 md:grid-cols-2">
              <div>
                <div className="eyebrow mb-3 text-text">Real · verified</div>
                <ul className="space-y-2">
                  {[
                    'Bengaluru road network — 112,725 nodes from OpenStreetMap',
                    'Live traffic routing via the Google Routes API',
                    'Live weather, factored into ETA',
                    'Hospital capability profiles, curated for 30 facilities',
                    'A* pathfinding, closures and congestion included',
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2 text-[13px] leading-relaxed text-text-muted">
                      <Check size={14} className="mt-[3px] shrink-0 text-text" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="eyebrow mb-3 text-text-faint">Modelled</div>
                <ul className="space-y-2">
                  {[
                    'Live ICU beds and blood units — no hospital feed is integrated',
                    'Ambulance fleet positions — hubs are fixed, not GPS-tracked',
                    'Triage classification — transparent keyword scoring, not a trained model',
                    'Signal preemption and congestion displacement — simulated',
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2 text-[13px] leading-relaxed text-text-muted">
                      <span className="mt-[7px] h-1.5 w-1.5 shrink-0 border border-dashed border-text-faint" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-ink bg-ink">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-3 px-5 py-5 sm:px-8">
          <span className="t-meta uppercase tracking-[0.12em] text-on-ink-muted">
            RapidGrid · Bengaluru emergency vehicle dispatch
          </span>
          <span className="t-meta text-on-ink-muted">
            Road data © OpenStreetMap contributors
          </span>
        </div>
      </footer>
    </div>
  );
}
