/**
 * Shared primitives.
 *
 * Every colour here comes from the theme tokens in index.css. If you find
 * yourself writing a hex value in a view, add a token instead.
 */

import React from 'react';

/* -------------------------------------------------------------------------
   Panel - the base surface. Hairline rule, small radius, no floating shadow.
   ------------------------------------------------------------------------- */
export function Panel({ ink = false, className = '', children, ...rest }) {
  return (
    <div className={`${ink ? 'panel-ink' : 'panel'} ${className}`} {...rest}>
      {children}
    </div>
  );
}

export function PanelHead({ label, ink = false, right = null, className = '' }) {
  return (
    <div
      className={`flex items-center justify-between gap-3 px-4 py-2.5 border-b ${
        ink ? 'border-rule-ink' : 'border-rule'
      } ${className}`}
    >
      <span className={ink ? 'eyebrow eyebrow-ink' : 'eyebrow'}>{label}</span>
      {right}
    </div>
  );
}

/* -------------------------------------------------------------------------
   Provenance - the signature. Never render a number whose status is unknown.
   ------------------------------------------------------------------------- */
export function Provenance({ kind, children, title, className = '' }) {
  const verified = kind === 'curated' || kind === 'verified' || kind === 'live';
  const label = children ?? (verified ? 'Verified' : 'Modelled');
  return (
    <span
      title={
        title ??
        (verified
          ? 'Sourced from a reviewed dataset or a live feed.'
          : 'Modelled estimate. No live data source exists for this value.')
      }
      className={`inline-flex items-center gap-1.5 rounded-xs px-2 py-[4px] t-tag ${
        verified ? 'prov-verified' : 'prov-modelled'
      } ${className}`}
    >
      <span
        className={`inline-block h-1.5 w-1.5 ${
          verified ? 'bg-current' : 'border border-dashed border-current bg-transparent'
        }`}
      />
      {label}
    </span>
  );
}

/* -------------------------------------------------------------------------
   Stat - a labelled telemetry readout. Mono, tabular, sized for glancing.
   ------------------------------------------------------------------------- */
export function Stat({ label, value, unit, tone = 'default', sub, ink = false, modelled = false }) {
  const tones = {
    default: ink ? 'text-on-ink' : 'text-text',
    signal: 'text-signal',
    critical: 'text-critical',
    muted: ink ? 'text-on-ink-muted' : 'text-text-muted',
  };
  return (
    <div className="min-w-0">
      <div className={ink ? 'eyebrow eyebrow-ink' : 'eyebrow'}>{label}</div>
      <div className={`mt-1 font-mono leading-none ${tones[tone]} ${modelled ? 'font-normal' : 'font-semibold'}`}>
        <span className={`text-[26px] tracking-tight ${modelled ? 'val-modelled' : 'val-verified'}`}>
          {value}
        </span>
        {unit && <span className="ml-1 text-[13px] font-medium opacity-60">{unit}</span>}
      </div>
      {sub && (
        <div className={`mt-1.5 t-micro ${ink ? 'text-on-ink-muted' : 'text-text-faint'}`}>
          {sub}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------
   Severity - triage language. Critical is the only user of P1 red.
   ------------------------------------------------------------------------- */
export function severityBand(severity = 0) {
  if (severity >= 0.8) return { key: 'critical', label: 'Critical', short: 'P1' };
  if (severity >= 0.6) return { key: 'high', label: 'High', short: 'P2' };
  if (severity >= 0.4) return { key: 'moderate', label: 'Moderate', short: 'P3' };
  return { key: 'low', label: 'Low', short: 'P4' };
}

export function SeverityTag({ severity = 0, className = '' }) {
  const band = severityBand(severity);
  const styles = {
    critical: 'bg-critical text-on-ink border-critical',
    high: 'bg-signal text-ink border-ink',
    moderate: 'bg-signal-wash text-signal-hover border-signal-edge',
    low: 'bg-paper-sunk text-text-muted border-rule',
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-xs border px-2 py-[4px] t-tag ${styles[band.key]} ${className}`}
    >
      <span className="opacity-70">{band.short}</span>
      {band.label}
    </span>
  );
}

/* -------------------------------------------------------------------------
   Status - where an incident sits in the pipeline.
   ------------------------------------------------------------------------- */
const STATUS_COPY = {
  processing: { label: 'Assessing', tone: 'wait' },
  failed: { label: 'Failed', tone: 'bad' },
  awaiting_dispatcher_approval: { label: 'Needs approval', tone: 'act' },
  dispatched: { label: 'En route', tone: 'live' },
  claimed: { label: 'Unit claimed', tone: 'live' },
  patient_picked_up: { label: 'Patient aboard', tone: 'live' },
  arrived: { label: 'Arrived', tone: 'done' },
  completed: { label: 'Resolved', tone: 'done' },
};

/** Fall back to sentence case rather than leaking snake_case into the UI. */
function humanise(status) {
  if (!status) return 'Unknown';
  const words = String(status).replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function StatusTag({ status, className = '' }) {
  const meta = STATUS_COPY[status] ?? { label: humanise(status), tone: 'wait' };
  const styles = {
    wait: 'bg-paper-sunk text-text-muted border-rule',
    act: 'bg-signal text-ink border-ink',
    live: 'bg-ink text-on-ink border-ink',
    done: 'bg-paper text-text border-ink',
    bad: 'bg-critical-wash text-critical border-critical',
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-xs border px-2 py-[4px] t-tag ${styles[meta.tone]} ${className}`}
    >
      {meta.tone === 'live' && (
        <span className="live-dot inline-block h-1.5 w-1.5 rounded-full bg-signal" />
      )}
      {meta.label}
    </span>
  );
}

/* -------------------------------------------------------------------------
   Button - one accent, three weights.
   ------------------------------------------------------------------------- */
export function Button({
  variant = 'primary',
  size = 'md',
  className = '',
  children,
  ...rest
}) {
  const variants = {
    primary: 'bg-signal text-ink border-ink hover:bg-ink hover:text-signal',
    ink: 'bg-ink text-on-ink border-ink hover:bg-ink-raised',
    quiet: 'bg-paper text-text border-ink hover:bg-ink hover:text-on-ink',
    ghost: 'bg-transparent text-text-muted border-transparent hover:bg-paper-sunk hover:text-text',
    critical: 'bg-critical text-on-ink border-critical hover:bg-ink',
  };
  const sizes = {
    sm: 'px-3 py-2 text-[12px] lg:px-2.5 lg:py-1.5 lg:text-[11px]',
    md: 'px-4 py-3 text-[13px] lg:py-2.5 lg:text-[12px]',
    lg: 'px-6 py-4 text-[14px] lg:py-3.5 lg:text-[13px]',
  };
  return (
    <button
      className={`tap inline-flex items-center justify-center gap-2 rounded-sm border font-mono font-bold uppercase tracking-[0.08em] transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${variants[variant]} ${sizes[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------
   ConnectionState - the app must never silently fail.
   ------------------------------------------------------------------------- */
export function ConnectionState({ status, lastOk, ink = false }) {
  if (status === 'ok') return null;
  const down = status === 'down';
  return (
    <div
      role="status"
      className={`flex items-center gap-2 rounded-sm border px-2.5 py-2 t-tag ${
        down
          ? 'border-critical bg-critical-wash text-critical'
          : 'border-signal-edge bg-signal-wash text-signal-hover'
      }`}
    >
      <span className="live-dot inline-block h-1.5 w-1.5 rounded-full bg-current" />
      {down ? 'Backend unreachable' : 'Reconnecting'}
      {lastOk && <span className="hidden font-normal normal-case opacity-70 sm:inline">· {lastOk}</span>}
    </div>
  );
}

/* -------------------------------------------------------------------------
   Empty - an empty screen is an invitation to act, not a shrug.
   ------------------------------------------------------------------------- */
export function Empty({ title, hint, action = null, ink = false }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <div
        className={`font-display text-[15px] font-semibold ${ink ? 'text-on-ink' : 'text-text'}`}
      >
        {title}
      </div>
      {hint && (
        <p
          className={`max-w-[38ch] text-[13px] leading-relaxed lg:text-[12px] ${
            ink ? 'text-on-ink-muted' : 'text-text-muted'
          }`}
        >
          {hint}
        </p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/* -------------------------------------------------------------------------
   Formatting helpers - one place, so units never drift between screens.
   ------------------------------------------------------------------------- */
export const fmt = {
  /** Seconds to m:ss, the dispatch vernacular. */
  clock(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return '--:--';
    const s = Math.max(0, Math.round(seconds));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  },
  minutes(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return '--';
    return (seconds / 60).toFixed(1);
  },
  km(metres) {
    if (metres == null || !Number.isFinite(metres)) return '--';
    return (metres / 1000).toFixed(1);
  },
  pct(unit) {
    if (unit == null || !Number.isFinite(unit)) return '--';
    return Math.round(unit * 100);
  },
  score(value) {
    if (value == null || !Number.isFinite(value)) return '--';
    return value.toFixed(3);
  },
};
