/**
 * Decision rationale.
 *
 * The backend now emits a genuine counterfactual - which hospital lost, on
 * which factor, and what the trade-off cost in road minutes. Previously that
 * arrived as one undifferentiated paragraph among six. Here the trade-off is
 * the centrepiece, drawn as a comparison rather than described in prose,
 * because it is the one thing a dispatcher must understand before overriding.
 */

import React from 'react';
import { AlertTriangle } from 'lucide-react';
import { Panel, PanelHead, Provenance, fmt } from './ui';

/** Split the explanation into its labelled sections. */
function parseExplanation(text = '') {
  const keys = ['ROUTE', 'HOSPITAL', 'TRAFFIC', 'ALTERNATIVE CONSIDERED', 'FUSION', 'WARNING'];
  const out = {};
  keys.forEach((key) => {
    const re = new RegExp(`${key}:\\s*(.*?)(?=\\s(?:${keys.join('|')}):|$)`, 's');
    const hit = text.match(re);
    if (hit) out[key] = hit[1].trim();
  });
  return out;
}

function FactorBar({ factor }) {
  const pctOfWeight = factor.weight > 0 ? factor.score : 0;
  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1 py-2">
      <div className="min-w-0">
        <div className="truncate text-[12px] font-medium text-text">{factor.factor}</div>
        <div className="mt-1 h-[3px] w-full overflow-hidden rounded-full bg-paper-sunk">
          <div
            className="h-full rounded-full bg-ink transition-[width] duration-500"
            style={{ width: `${Math.max(2, pctOfWeight * 100)}%` }}
          />
        </div>
      </div>
      <div className="text-right font-mono text-[11px] leading-tight">
        <div className="font-bold text-text">{fmt.score(factor.weighted_score)}</div>
        <div className="text-text-faint">w {factor.weight != null ? factor.weight.toFixed(2) : '--'}</div>
      </div>
    </div>
  );
}

/** The counterfactual, drawn as the trade-off it actually is. */
function TradeOff({ winner, runnerUp }) {
  if (!winner || !runnerUp) return null;

  const byFactor = (entry) =>
    Object.fromEntries((entry.score_breakdown ?? []).map((b) => [b.factor, b]));
  const w = byFactor(winner);
  const r = byFactor(runnerUp);
  const shared = Object.keys(w).filter((k) => k in r);
  if (!shared.length) return null;

  const conceded = shared.reduce((a, b) =>
    r[b].weighted_score - w[b].weighted_score > r[a].weighted_score - w[a].weighted_score ? b : a,
  );
  const gained = shared.reduce((a, b) =>
    w[b].weighted_score - r[b].weighted_score > w[a].weighted_score - r[a].weighted_score ? b : a,
  );

  const etaDelta =
    winner.road_eta_seconds && runnerUp.road_eta_seconds
      ? (runnerUp.road_eta_seconds - winner.road_eta_seconds) / 60
      : null;

  const Column = ({ entry, chosen, factor, otherFactor }) => (
    <div
      className={`min-w-0 rounded-sm border p-3 ${
        chosen ? 'border-signal bg-signal-wash' : 'border-rule bg-paper-sunk'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={`t-tag ${
            chosen ? 'text-signal-hover' : 'text-text-faint'
          }`}
        >
          {chosen ? 'Selected' : 'Runner-up'}
        </span>
        <span className="font-mono text-[11px] font-bold text-text">
          {fmt.score(entry.score)}
        </span>
      </div>
      <div className="mt-1.5 text-[12px] font-semibold leading-snug text-text">{entry.name}</div>
      <dl className="mt-2.5 space-y-1 border-t border-rule/70 pt-2 t-meta">
        <div className="flex justify-between gap-2">
          <dt className="truncate text-text-faint">{factor}</dt>
          <dd className="font-bold text-text">{entry.__f?.[factor]?.raw_score?.toFixed(2)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="truncate text-text-faint">{otherFactor}</dt>
          <dd className="font-bold text-text">{entry.__f?.[otherFactor]?.raw_score?.toFixed(2)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-text-faint">Road ETA</dt>
          <dd className="font-bold text-text">
            {entry.road_eta_seconds ? `${fmt.minutes(entry.road_eta_seconds)} min` : '--'}
          </dd>
        </div>
      </dl>
    </div>
  );

  const wEntry = { ...winner, __f: w };
  const rEntry = { ...runnerUp, __f: r };

  return (
    <section className="border-t border-rule px-4 py-3.5">
      <div className="eyebrow mb-2.5">The trade-off</div>
      <div className="grid grid-cols-2 gap-2">
        <Column entry={wEntry} chosen factor={gained} otherFactor={conceded} />
        <Column entry={rEntry} factor={gained} otherFactor={conceded} />
      </div>
      <p className="mt-2.5 text-[12px] leading-relaxed text-text-muted">
        <span className="font-semibold text-text">{runnerUp.name}</span> is stronger on{' '}
        <span className="font-semibold text-text">{conceded.toLowerCase()}</span>, but{' '}
        <span className="font-semibold text-text">{winner.name}</span> wins on{' '}
        <span className="font-semibold text-text">{gained.toLowerCase()}</span>
        {etaDelta != null && Math.abs(etaDelta) >= 0.5 && (
          <>
            {etaDelta > 0 ? ' and reaches the patient ' : ' at a cost of '}
            <span className="font-mono font-bold text-signal-hover">
              {Math.abs(etaDelta).toFixed(1)} min
            </span>
            {etaDelta > 0 ? ' sooner' : ' extra transport time'}
          </>
        )}
        .
      </p>
    </section>
  );
}

/**
 * Time to definitive treatment.
 *
 * The headline number, because minutes on the road are only a proxy for the
 * thing that decides the outcome. A nearer hospital that cannot deliver the
 * intervention has to transfer the patient onward, which is where the real
 * time goes - and that is what justifies driving past it.
 */
function OutcomeCard({ outcome }) {
  if (!outcome?.chosen) return null;
  const { chosen, alternative, verdict, outcome_delta_minutes: delta } = outcome;

  const Bar = ({ entry, chosen: isChosen }) => {
    const scale = Math.max(chosen.total_minutes, alternative?.total_minutes ?? 0, entry.target_minutes) * 1.05;
    const seg = (v, cls, title) =>
      v > 0 && (
        <span
          title={`${title}: ${v.toFixed(1)} min`}
          className={cls}
          style={{ width: `${(v / scale) * 100}%` }}
        />
      );
    return (
      <div className="py-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className={`truncate text-[12px] ${isChosen ? 'font-semibold text-text' : 'text-text-muted'}`}>
            {entry.hospital_name}
          </span>
          <span
            className={`shrink-0 font-mono text-[12px] font-bold ${
              entry.within_target ? 'text-verified' : 'text-critical'
            }`}
          >
            {entry.total_minutes.toFixed(0)} min
          </span>
        </div>
        <div className="mt-1.5 flex h-2 w-full overflow-hidden rounded-full bg-paper-sunk">
          {seg(entry.to_patient_minutes + entry.activation_minutes, 'bg-rule-strong', 'Dispatch and drive to patient')}
          {seg(entry.on_scene_minutes, 'bg-text-faint', 'On scene')}
          {seg(entry.to_hospital_minutes, 'bg-signal', 'Transport to hospital')}
          {seg(entry.in_hospital_minutes, 'bg-ink-raised', 'In-hospital workup')}
          {seg(entry.transfer_minutes, 'bg-critical', 'Secondary transfer')}
        </div>
        {entry.requires_transfer && (
          <p className="mt-1.5 t-micro text-critical">
            Needs onward transfer for {entry.intervention} (+{entry.transfer_minutes.toFixed(0)} min)
          </p>
        )}
      </div>
    );
  };

  return (
    <section className="border-t border-rule px-4 py-3.5">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <span className="eyebrow">{chosen.metric}</span>
        <Provenance kind="simulated">Modelled</Provenance>
      </div>
      <p className="mb-2 t-micro text-text-faint">
        Target {chosen.target_minutes.toFixed(0)} min · {chosen.guideline}
      </p>

      <div className="divide-y divide-rule/60">
        <Bar entry={chosen} chosen />
        {alternative && <Bar entry={alternative} />}
      </div>

      {verdict && (
        <p
          className={`mt-2.5 rounded-sm border px-3 py-2 text-[12px] leading-relaxed ${
            delta > 1
              ? 'border-ink bg-paper text-text'
              : 'border-rule bg-paper-sunk text-text-muted'
          }`}
        >
          {verdict}
        </p>
      )}
    </section>
  );
}

export default function AIRationale({ plan }) {
  if (!plan) return null;

  const sections = parseExplanation(plan.explanation ?? '');
  const factors = [...(plan.factor_breakdown ?? [])].sort(
    (a, b) => b.weighted_score - a.weighted_score,
  );

  // Never invent a confidence figure. A missing value means the pipeline
  // degraded, and the dispatcher must see that rather than a reassuring number.
  const hasConfidence = typeof plan.overall_confidence === 'number';
  const confidence = hasConfidence ? Math.round(plan.overall_confidence * 100) : null;
  const degraded = plan.data_freshness && plan.data_freshness !== 'live';

  const ranked = plan.all_hospitals ?? [];

  return (
    <Panel className="overflow-hidden">
      <PanelHead
        label="Decision rationale"
        right={
          <span
            className={`font-mono text-[11px] font-bold ${
              confidence == null
                ? 'text-text-faint'
                : confidence >= 75
                  ? 'text-verified'
                  : 'text-signal-hover'
            }`}
          >
            {confidence == null ? 'confidence unavailable' : `${confidence}% confidence`}
          </span>
        }
      />

      {degraded && (
        <div className="flex items-start gap-2 border-b border-signal-edge bg-signal-wash px-4 py-2.5">
          <AlertTriangle size={13} className="mt-px shrink-0 text-signal-hover" />
          <p className="text-[11px] leading-relaxed text-signal-hover">
            <span className="font-bold uppercase tracking-wide">Degraded data</span> — live routing
            was unavailable, so this plan used the offline road graph. Verify the ETA before
            dispatching.
          </p>
        </div>
      )}

      <OutcomeCard outcome={plan.clinical_outcome} />

      <TradeOff winner={ranked[0]} runnerUp={ranked[1]} />

      <section className="border-t border-rule px-4 py-3.5">
        <div className="eyebrow mb-1">Weighted factors</div>
        <div className="divide-y divide-rule/60">
          {factors.map((f, i) => (
            <FactorBar key={f.factor ?? i} factor={f} />
          ))}
        </div>
      </section>

      {(sections.ROUTE || sections.TRAFFIC) && (
        <section className="space-y-2.5 border-t border-rule bg-paper-sunk px-4 py-3.5">
          {sections.ROUTE && (
            <div>
              <div className="eyebrow mb-1">Route</div>
              <p className="text-[11px] leading-relaxed text-text-muted">{sections.ROUTE}</p>
            </div>
          )}
          {sections.TRAFFIC && (
            <div>
              <div className="eyebrow mb-1">Traffic</div>
              <p className="text-[11px] leading-relaxed text-text-muted">{sections.TRAFFIC}</p>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t border-rule pt-2.5">
            <span className="eyebrow">Data</span>
            <Provenance kind={plan.data_freshness === 'live' ? 'live' : 'modelled'}>
              {plan.data_freshness === 'live' ? 'Live routing' : 'Offline graph'}
            </Provenance>
            <Provenance kind="curated">Hospital capability</Provenance>
            <Provenance kind="simulated">Bed availability</Provenance>
          </div>
        </section>
      )}
    </Panel>
  );
}
