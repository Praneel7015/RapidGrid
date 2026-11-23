/**
 * Disruption controls.
 *
 * These are interventions on the causal city model, not map annotations.
 * Closing a road here changes the cost of the network, so every route computed
 * afterwards genuinely differs - including routes for incidents that were
 * already planned and had nothing to do with the closure, because the traffic
 * displaced off the closed road congests the streets around it.
 */

import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, RotateCcw, CloudRain, Clock } from 'lucide-react';
import { Panel, PanelHead, Button, Provenance } from './ui';

const WEATHER = ['Clear', 'Clouds', 'Rain', 'Thunderstorm', 'Fog'];

export default function CityControls({ onChange }) {
  const [state, setState] = useState(null);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);

  // One condition change at a time, and a state read that started before a
  // change never overwrites what that change returned.
  const running = useRef(false);
  const changes = useRef(0);

  const load = async () => {
    const seen = changes.current;
    try {
      const res = await fetch('/api/city/state');
      if (res.ok) {
        const data = await res.json();
        if (seen === changes.current) setState(data);
      }
    } catch { /* the console still works without the city model */ }
  };

  useEffect(() => {
    load();
  }, []);

  // Road-name autocomplete.
  useEffect(() => {
    if (query.trim().length < 3) {
      setMatches([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/city/roads?q=${encodeURIComponent(query.trim())}`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setMatches(data.roads ?? []);
      } catch { /* ignore */ }
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [query]);

  const act = async (url, body, describe) => {
    if (running.current) return;
    running.current = true;
    changes.current += 1;
    setBusy(true);
    setNote(null);
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      });
      const data = await res.json();
      if (!res.ok) {
        setNote(data.detail ?? 'That did not work.');
      } else {
        setState(data.state ?? data);
        setNote(describe(data));
        onChange?.();
      }
    } catch {
      setNote('Cannot reach the city model.');
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const closeRoad = (road) =>
    act('/api/city/close', { road, label: road }, (d) =>
      `Closed ${d.closed} segments on ${road}. Traffic displaced onto ${d.displaced} nearby segments.`,
    );

  return (
    <Panel>
      <PanelHead
        label="Disruptions"
        right={<Provenance kind="simulated">Modelled</Provenance>}
      />

      <div className="space-y-3 p-3">
        <div>
          <label className="eyebrow" htmlFor="road-close">
            Close a road
          </label>
          <input
            id="road-close"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Bellary Road, Hosur Road…"
            className="mt-2 w-full rounded-sm border border-rule bg-paper px-3 py-2.5 text-[16px] text-text placeholder:text-text-faint focus:border-signal focus:outline-none lg:py-2 lg:text-[13px]"
          />
          {matches.length > 0 && (
            <div className="mt-1.5 max-h-40 overflow-y-auto rounded-sm border border-rule">
              {matches.map((m) => (
                <button
                  key={m.name}
                  onClick={() => {
                    closeRoad(m.name);
                    setQuery('');
                    setMatches([]);
                  }}
                  disabled={busy}
                  className="flex w-full items-center justify-between gap-2 border-b border-rule/60 px-3 py-2.5 text-left last:border-0 hover:bg-signal-wash disabled:opacity-50"
                >
                  <span className="truncate text-[12.5px] text-text">{m.name}</span>
                  <span className="shrink-0 t-meta text-text-faint">{m.segments} seg</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="eyebrow flex items-center gap-1.5" htmlFor="city-hour">
              <Clock size={11} /> Hour
            </label>
            <select
              id="city-hour"
              disabled={busy}
              value={state?.hour ?? 9}
              onChange={(e) =>
                act('/api/city/conditions', { hour: Number(e.target.value) }, (d) =>
                  `Time set to ${String(d.hour).padStart(2, '0')}:00 (peak factor ${d.peak_factor}).`,
                )
              }
              className="mt-2 w-full rounded-sm border border-rule bg-paper px-2 py-2.5 text-[13px] text-text focus:border-signal focus:outline-none lg:py-2"
            >
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>
                  {String(h).padStart(2, '0')}:00
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="eyebrow flex items-center gap-1.5" htmlFor="city-weather">
              <CloudRain size={11} /> Weather
            </label>
            <select
              id="city-weather"
              disabled={busy}
              value={state?.weather ?? 'Clear'}
              onChange={(e) =>
                act('/api/city/conditions', { weather: e.target.value }, (d) =>
                  `Weather set to ${d.weather} (travel x${d.weather_multiplier}).`,
                )
              }
              className="mt-2 w-full rounded-sm border border-rule bg-paper px-2 py-2.5 text-[13px] text-text focus:border-signal focus:outline-none lg:py-2"
            >
              {WEATHER.map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* One click for the classic hard case: evening peak in the rain. */}
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            act('/api/city/conditions', { hour: 18, weather: 'Rain' }, (d) =>
              `Evening rush in the rain: 18:00, peak factor ${d.peak_factor}, travel x${d.weather_multiplier} (modelled).`,
            )
          }
          className="w-full rounded-sm border border-rule bg-paper px-3 py-2 text-left text-[12.5px] font-semibold text-text hover:border-signal disabled:opacity-50"
        >
          <CloudRain size={12} className="mr-1.5 inline" aria-hidden="true" />
          Preset: evening rush + rain
        </button>

        {state && (
          <dl className="grid grid-cols-3 gap-2 border-t border-rule pt-3">
            {[
              ['Closed', state.closed_segments],
              ['Displaced', state.displaced_segments],
              ['Peak', state.peak_factor],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="eyebrow">{label}</dt>
                <dd className="mt-1 font-mono text-[15px] font-bold text-text">{value}</dd>
              </div>
            ))}
          </dl>
        )}

        {note && (
          <p className="flex items-start gap-2 rounded-sm border border-signal-edge bg-signal-wash px-3 py-2 text-[12px] leading-relaxed text-signal-hover">
            <AlertTriangle size={13} className="mt-px shrink-0" />
            {note}
          </p>
        )}

        <Button
          variant="quiet"
          size="sm"
          disabled={busy}
          onClick={() =>
            act('/api/city/clear', {}, () => 'All disruptions cleared. City back to baseline.')
          }
          className="w-full"
        >
          <RotateCcw size={13} /> Reset the city
        </Button>

        <p className="t-micro leading-relaxed text-text-faint">
          Closures remove segments from routing and push their traffic onto neighbouring roads, so
          unrelated routes slow down too. Coefficients are literature-informed estimates, not fitted
          to Bengaluru traffic counts.
        </p>
      </div>
    </Panel>
  );
}
