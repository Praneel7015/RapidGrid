/**
 * Citizen emergency app.
 *
 * Sized for crisis: one unmistakable action, short sentences, voice dictation
 * on the report form, and click-to-call once a unit/hospital is assigned.
 */

import React, { useState, useEffect, useRef } from 'react';
import shortId from '../utils/shortId';
import { useAuth } from '../context/AuthContext';
import { MapPin, Phone, Check, AlertTriangle, ChevronLeft } from 'lucide-react';
import MapOverlay from '../components/MapOverlay';
import { useHospitals, useOsmHospitals } from '../hooks/useCityLayers';
import useUnitTrack, { liveEtaMinutes } from '../hooks/useUnitTrack';
import LiveEmergencyChat from '../components/LiveEmergencyChat';
import MicButton from '../components/MicButton';
import CallButton from '../components/CallButton';
import CallNumber from '../components/CallNumber';
import useIncidentContacts from '../hooks/useIncidentContacts';
import useSpeechToText from '../hooks/useSpeechToText';
import { Panel, PanelHead, Button, Stat, StatusTag } from '../components/ui';
import { fmt } from '../components/ui';

const SERVICES = [
  { id: 'Ambulance', label: 'Medical', hint: 'Injury or illness' },
  { id: 'Fire Engine', label: 'Fire', hint: 'Fire or rescue' },
  { id: 'Police Cruiser', label: 'Police', hint: 'Crime or safety' },
  { id: 'Disaster Rescue', label: 'Disaster', hint: 'Collapse or flood' },
];

const PIPELINE = [
  'Reading your report',
  'Assessing severity',
  'Checking traffic conditions',
  'Ranking nearby hospitals',
  'Calculating the fastest route',
  'Waiting for dispatcher approval',
];

const FALLBACK_LOCATION = { lat: 12.9756, lng: 77.6068 };

export default function CitizenDashboard() {
  const { citizenInfo } = useAuth();
  // Every hospital around the caller, so they can see where help comes from.
  const hospitals = useHospitals();
  const osmHospitals = useOsmHospitals();

  const [stage, setStage] = useState('home'); // home | report | sending | tracking | failed
  // The caller's last call, once it is handed over: a closing summary on the
  // home screen instead of a silent return to "Need emergency help?".
  const [finished, setFinished] = useState(null);
  const [service, setService] = useState('Ambulance');
  const [details, setDetails] = useState('');
  const [usedVoice, setUsedVoice] = useState(false);
  const [coords, setCoords] = useState(null);
  const [locationLabel, setLocationLabel] = useState('Locating you…');
  // pending | gps | fallback. A fallback is never sent unless the caller confirms it.
  const [locationSource, setLocationSource] = useState('pending');
  const [fallbackConfirmed, setFallbackConfirmed] = useState(false);
  const [error, setError] = useState(null);
  const [step, setStep] = useState(0);

  const [pollUrl, setPollUrl] = useState(null);
  const [incident, setIncident] = useState(null);
  const [sending, setSending] = useState(false); // double-submit guard
  const view = incident?.citizen_view ?? null;
  const callContacts = useIncidentContacts(incident?.incident_id);
  const track = useUnitTrack(incident?.incident_id, ['dispatched', 'patient_picked_up'].includes(incident?.status));

  const speech = useSpeechToText({ lang: 'en-IN' });
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  useEffect(() => {
    speech.setOnFinal((finalText) => {
      setUsedVoice(true);
      setDetails((prev) => (prev ? `${prev.trim()} ${finalText}` : finalText).trim());
    });
    return () => speech.setOnFinal(null);
  }, [speech.setOnFinal]);

  useEffect(() => {
    if (!navigator.geolocation) {
      setCoords(FALLBACK_LOCATION);
      setLocationSource('fallback');
      setLocationLabel('Bengaluru city centre — GPS unavailable');
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (!mounted.current) return;
        setCoords({ lat: pos.coords.latitude, lng: pos.coords.longitude });
        setLocationSource('gps');
        setLocationLabel(
          `${pos.coords.latitude.toFixed(4)}°N, ${pos.coords.longitude.toFixed(4)}°E`,
        );
      },
      () => {
        if (!mounted.current) return;
        setCoords(FALLBACK_LOCATION);
        setLocationSource('fallback');
        setLocationLabel('Bengaluru city centre — your location was not shared');
      },
      { timeout: 8000 },
    );
  }, []);

  useEffect(() => {
    if (stage !== 'home') return;
    let cancelled = false;
    (async () => {
      try {
        // A signed-in caller's own reports come from their account; an
        // anonymous caller's last report is remembered by this browser only.
        // Nobody can list other callers' reports.
        // The server knows whether this browser is signed in (the account
        // cookie), even in a new tab; ask it rather than tab storage.
        let mine = [];
        // A 401 here just means nobody is signed in: not an error for the caller.
        const res0 = await fetch('/api/citizens/me/incidents').catch(() => null);
        if (res0?.ok) mine = (await res0.json()).incidents ?? [];
        if (!mine.length) {
          let lastId = null;
          try { lastId = sessionStorage.getItem('rapidgrid_my_incident'); } catch { /* storage off */ }
          if (lastId) {
            const res = await fetch(`/api/incidents/${encodeURIComponent(lastId)}`);
            if (res.ok) mine = [await res.json()];
          }
        }
        const latest = mine[mine.length - 1];
        // Only rejoin in-flight work. Failed/completed reports must not trap the home screen.
        const active = latest && !['failed', 'completed', 'arrived'].includes(latest.status) ? latest : null;
        let dismissed = null;
        try { dismissed = sessionStorage.getItem('rapidgrid_summary_dismissed'); } catch { /* storage off */ }
        if (!cancelled && latest && latest.status === 'completed' && latest.incident_id !== dismissed) {
          setFinished(latest);
        }
        if (!cancelled && active?.citizen_view) {
          setIncident(latest);
          setPollUrl(`/api/incidents/${latest.incident_id}`);
          setStage('tracking');
        } else if (!cancelled && active?.status === 'processing') {
          setIncident(latest);
          setPollUrl(`/api/incidents/${latest.incident_id}`);
          setStage('sending');
        }
      } catch { /* offline: the SOS button still works */ }
    })();
    return () => { cancelled = true; };
  }, [stage, citizenInfo]);

  useEffect(() => {
    if (stage !== 'sending') return;
    setStep(0);
    const timer = setInterval(
      () => setStep((s) => Math.min(s + 1, PIPELINE.length - 1)),
      2600,
    );
    return () => clearInterval(timer);
  }, [stage]);

  useEffect(() => {
    if (!pollUrl || (stage !== 'sending' && stage !== 'tracking')) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(pollUrl);
        if (!res.ok) throw new Error(`Dispatch status unavailable (HTTP ${res.status}). Retrying…`);
        const data = await res.json();
        if (cancelled) return;
        setIncident(data);
        setError(null);
        if (data.status === 'failed') {
          setError(data.error || 'Dispatch could not complete your report.');
          setStage('failed');
          return;
        }
        if (data.citizen_view) setStage('tracking');
      } catch (err) {
        if (!cancelled) setError(err.message || 'Lost connection to dispatch. Retrying…');
      }
    };
    poll();
    const timer = setInterval(poll, 2500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [pollUrl, stage]);

  const sendSOS = async () => {
    if (sending) return; // double-submit guard
    setSending(true);
    // stop() delivers the final words asynchronously. Collect those words
    // directly, instead of posting the render's stale draft text.
    const spoken = await speech.finish();
    if (!mounted.current) return;
    const reportText = `${details} ${spoken}`.trim();
    const voice = usedVoice || Boolean(spoken);
    navigator.vibrate?.([180, 90, 180]);
    setError(null);
    setIncident(null);
    setPollUrl(null);
    setStage('sending');
    try {
      const res = await fetch('/api/incidents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          citizen_text: reportText || `Emergency reported, ${service} requested.`,
          // A signed-in caller's account details replace these on the server.
          citizen_name: citizenInfo?.name ?? 'Anonymous',
          citizen_phone: citizenInfo?.phone ?? 'Unregistered',
          // Only an explicit "Report without an account" choice; a fresh tab
          // with a valid sign-in still reports under the account.
          anonymous: citizenInfo?.anonymousChoice === true,
          vehicle_required: service,
          location: coords,
          input_modality: voice ? 'voice' : 'text',
          timestamp: new Date().toISOString(),
        }),
      });
      // A proxy error page is not JSON: the server did answer, so say that, not "no connection".
      const data = await res.json().catch(() => ({}));
      if (res.ok || res.status === 202) {
        try { sessionStorage.setItem('rapidgrid_my_incident', data.incident_id); } catch { /* storage off */ }
        setPollUrl(data.poll_url ?? `/api/incidents/${data.incident_id}`);
      } else {
        setError(typeof data.detail === 'string' ? data.detail : `Dispatch rejected the report (HTTP ${res.status}). Try again.`);
        setStage('report');
        return;
      }
    } catch {
      setError('Cannot reach emergency dispatch. Check your connection and try again.');
      setStage('report');
    } finally {
      setSending(false);
    }
  };

  const shell = (children) => (
    <div className="mx-auto w-full max-w-[520px] px-4 py-5 lg:max-w-[720px]">{children}</div>
  );

  if (stage === 'home') {
    return shell(
      <div className="flex min-h-[calc(100vh-120px)] flex-col">
        {finished && (
          <Panel className="mb-5" data-testid="call-summary">
            <PanelHead label={`Incident ${shortId(finished.incident_id)} · complete`} />
            <div className="space-y-1.5 px-4 py-3.5 text-[13px] leading-relaxed text-text">
              <p className="text-[14px] font-semibold">
                {finished.citizen_view?.hospital_name
                  ? `Handed over at ${finished.citizen_view.hospital_name}.`
                  : 'The response is complete.'}
              </p>
              {finished.assigned_unit && <p className="text-text-muted">Unit {finished.assigned_unit} responded.</p>}
              <button
                type="button"
                className="t-meta font-bold text-text-muted underline-offset-2 hover:underline"
                onClick={() => {
                  try { sessionStorage.setItem('rapidgrid_summary_dismissed', finished.incident_id); } catch { /* storage off */ }
                  setFinished(null);
                }}
              >
                Dismiss
              </button>
            </div>
          </Panel>
        )}
        <div className="mb-6">
          <h1 className="font-display text-[clamp(22px,6vw,28px)] font-semibold leading-tight text-text">
            Need emergency help?
          </h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-text-muted">
            Tap SOS to prepare a demo report. Add details and review your location before sending.
          </p>
        </div>

        <div className="flex flex-1 flex-col items-center justify-center gap-6">
          <button
            onClick={() => setStage('report')}
            className="group relative grid aspect-square w-[min(62vw,240px)] place-items-center rounded-full border-4 border-critical bg-critical text-on-ink transition-transform active:scale-95"
          >
            <span className="rg-ping absolute inset-0 rounded-full text-critical opacity-30" />
            <span className="relative text-center">
              <span className="block font-display text-[clamp(34px,9vw,44px)] font-semibold leading-none tracking-tight">
                SOS
              </span>
              <span className="mt-2 block t-tag opacity-80">
                Get help now
              </span>
            </span>
          </button>

          <div className="flex items-center gap-2 rounded-sm border border-rule bg-paper px-3 py-2">
            <MapPin size={14} className="shrink-0 text-signal" />
            <span className="t-meta text-text-muted">{locationLabel}</span>
          </div>
        </div>

        <Panel className="mt-6 overflow-hidden">
          <MapOverlay
            hospitals={hospitals}
            osmHospitals={osmHospitals}
            height="min(40vh, 340px)"
            className="!rounded-none !border-0"
          />
        </Panel>

        <div className="mt-6 flex justify-center">
          <CallNumber
            phone="112"
            label="Or call 112"
            iconSize={15}
            className="tap flex w-full items-center justify-center gap-2 rounded-sm border border-ink bg-paper py-4 t-tag text-text hover:bg-ink hover:text-on-ink"
          />
        </div>
      </div>,
    );
  }

  if (stage === 'report' || stage === 'failed') {
    return shell(
      <div>
        <button
          onClick={() => {
            setStage('home');
            setError(null);
            setIncident(null);
            setPollUrl(null);
          }}
          className="tap mb-4 inline-flex items-center gap-1 py-1 t-tag text-text-muted hover:text-text"
        >
          <ChevronLeft size={14} /> Back
        </button>

        <h1 className="font-display text-[clamp(20px,5.5vw,24px)] font-semibold leading-tight text-text">
          {stage === 'failed' ? 'Report could not be completed' : 'What kind of help?'}
        </h1>

        {stage === 'failed' && (
          <div className="mt-3 flex items-start gap-2 rounded-sm border border-critical bg-critical-wash px-3 py-2.5 hz-refuse">
            <AlertTriangle size={14} className="mt-px shrink-0 text-critical" />
            <p className="text-[12px] leading-relaxed text-critical">
              Dispatch could not finish routing this report. You can try again, or call 112.
            </p>
          </div>
        )}

        <div className="mt-4 grid grid-cols-2 gap-2">
          {SERVICES.map((s) => (
            <button
              key={s.id}
              onClick={() => setService(s.id)}
              className={`tap rounded-sm border p-4 text-left transition-colors lg:p-3.5 ${
                service === s.id
                  ? 'border-ink bg-signal'
                  : 'border-rule bg-paper hover:border-rule-strong'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-[14px] font-semibold text-text">{s.label}</span>
                {service === s.id && <Check size={15} className="text-ink" />}
              </div>
              <p className="mt-0.5 text-[11px] text-text-muted">{s.hint}</p>
            </button>
          ))}
        </div>

        <div className="mt-5 block">
          <div className="flex items-center justify-between gap-2">
            <label htmlFor="report-details" className="eyebrow">What is happening?</label>
            <MicButton
              supported={speech.supported}
              listening={speech.listening}
              phase={speech.phase}
              onToggle={speech.toggle}
            />
          </div>
          <textarea
            id="report-details"
            value={speech.listening && speech.interim ? `${details} ${speech.interim}`.trim() : details}
            onChange={(e) => setDetails(e.target.value)}
            rows={4}
            placeholder="Chest pain, difficulty breathing, road accident… or tap the mic"
            className="mt-2 w-full resize-none rounded-sm border border-rule bg-paper p-3 text-[16px] leading-relaxed text-text placeholder:text-text-faint focus:border-signal focus:outline-none lg:text-[14px]"
          />
          <span role="status" className={`mt-2 block t-micro ${speech.error ? 'text-critical' : 'text-text-faint'}`}>
            {speech.error
              ? speech.error
              : speech.phase === 'loading'
                ? 'Loading the speech model… this happens once.'
                : speech.phase === 'transcribing'
                  ? 'Transcribing…'
                  : speech.listening && speech.engine === 'whisper'
                    ? 'Listening… tap the mic again when you finish.'
                    : speech.listening
                      ? 'Listening… speak clearly.'
                      : 'The more specific you are, the better the hospital match. You can dictate.'}
          </span>
        </div>

        <div className="mt-4 flex items-center gap-2 rounded-sm border border-rule bg-paper-sunk px-3 py-2.5">
          <MapPin size={14} className="shrink-0 text-signal" />
          <span className="t-meta text-text-muted">{locationLabel}</span>
        </div>

        {error && (stage === 'report' || stage === 'failed') && (
          <div className="mt-3 flex items-start gap-2 rounded-sm border border-critical-edge bg-critical-wash px-3 py-2.5">
            <AlertTriangle size={14} className="mt-px shrink-0 text-critical" />
            <p className="text-[12px] leading-relaxed text-critical">{error}</p>
          </div>
        )}

        {locationSource === 'fallback' && (
          <label className="mt-3 flex items-start gap-2.5 rounded-sm border border-signal-edge bg-signal-wash px-3 py-2.5">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 shrink-0"
              checked={fallbackConfirmed}
              onChange={(e) => setFallbackConfirmed(e.target.checked)}
            />
            <span className="text-[12px] leading-relaxed text-text">
              We could not get your location. Help would be sent to <strong>Bengaluru city
              centre</strong>, not to you. Tick this only if that is where you are; otherwise
              call 112.
            </span>
          </label>
        )}

        <Button
          onClick={sendSOS}
          disabled={sending || !coords || (locationSource === 'fallback' && !fallbackConfirmed)}
          size="lg"
          className="mt-5 w-full"
        >
          {!coords ? 'Finding your location…' : stage === 'failed' ? 'Try again' : 'Send emergency report'}
        </Button>

        <div className="mt-3 flex justify-center">
          <CallNumber
            phone="112"
            label="Call 112 instead"
            className="tap flex w-full items-center justify-center gap-2 rounded-sm border border-rule py-3 t-tag text-text-muted hover:text-text"
          />
        </div>
      </div>,
    );
  }

  if (stage === 'sending') {
    return shell(
      <div className="flex min-h-[calc(100vh-120px)] flex-col justify-center">
        <div className="mb-8 text-center">
          <div className="live-dot mx-auto mb-4 h-3 w-3 rounded-full bg-signal" />
          <h1 className="font-display text-[20px] font-semibold text-text">
            Help is being arranged
          </h1>
          <p className="mt-1.5 text-[13px] text-text-muted">Waiting for dispatch assessment. You can refresh to resume.</p>
          <p className="mt-2 text-[11px] text-text-faint">Illustrative checklist; the service does not report individual step progress.</p>
          {error && <p role="status" className="mt-3 text-sm text-critical">{error}</p>}
        </div>

        <ol className="space-y-0">
          {PIPELINE.map((label, i) => {
            const done = i < step;
            const current = i === step;
            return (
              <li key={label} className="flex items-center gap-3 py-2.5">
                <span
                  className={`grid h-5 w-5 shrink-0 place-items-center rounded-sm border t-micro ${
                    done
                      ? 'border-ink bg-ink text-on-ink'
                      : current
                        ? 'border-ink bg-signal text-ink'
                        : 'border-rule bg-paper text-text-faint'
                  }`}
                >
                  {done ? <Check size={11} /> : i + 1}
                </span>
                <span
                  className={`text-[13px] ${
                    current ? 'font-semibold text-text' : done ? 'text-text-muted' : 'text-text-faint'
                  }`}
                >
                  {label}
                </span>
              </li>
            );
          })}
        </ol>
      </div>,
    );
  }

  const dispatched = ['dispatched', 'claimed', 'patient_picked_up', 'arrived', 'completed'].includes(
    incident?.status,
  );
  // From the server's private directory, by role: a signed-in caller gets
  // their unit and dispatch; everyone gets 112.
  const unitPhone = callContacts?.unit?.phone ?? null;
  const dispatchPhone = callContacts?.dispatch?.phone ?? null;
  // Before the directory answers, 112 works anywhere in India; if the
  // directory says there is no hotline (null), no button is shown.
  const hotline = callContacts === null || callContacts === undefined ? '112' : callContacts.hotline_112?.phone;

  return shell(
    <div className="space-y-3">
      {error && <p role="status" className="text-sm text-critical">{error}</p>}
      <Panel data-tour="caller-status">
        <PanelHead
          label={`Incident ${shortId(incident?.incident_id)}`}
          right={<StatusTag status={incident?.status} />}
        />
        <div className="px-4 py-3.5">
          <p className="text-[14px] font-semibold leading-snug text-text">
            {incident?.status === 'completed' || incident?.status === 'arrived'
              ? 'This response is complete.'
              : incident?.status === 'patient_picked_up'
                ? 'Your unit is transporting you to the receiving hospital.'
                : dispatched
              ? `${/^[aeiou]/i.test(incident.vehicle_required ?? 'unit') ? 'An' : 'A'} ${incident.vehicle_required?.toLowerCase() ?? 'unit'} is on the way to you.`
              : 'Your report is with the city dispatcher for approval.'}
          </p>
          {view?.hospital_name && (
            <p className="mt-1.5 text-[13px] leading-relaxed text-text-muted">
              You will be taken to <span className="font-semibold text-text">{view.hospital_name}</span>.
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <CallButton phone={unitPhone} label="Call unit" variant="primary" />
            <CallButton phone={dispatchPhone} label="Call dispatch" />
            <CallButton phone={hotline} label={callContacts?.hotline_112?.phone ? `Call ${callContacts.hotline_112.name}` : 'Call 112'} />
          </div>
        </div>

        {view && (
          <div className="flex flex-wrap items-center gap-x-8 gap-y-4 border-t border-rule px-4 py-3.5">
            {incident?.status !== 'patient_picked_up' ? (
              <>
                <Stat
                  label="Unit reaches you in"
                  value={liveEtaMinutes(track, 'to_scene') ?? view.phase1_eta_minutes ?? '--'}
                  unit="min"
                  tone="signal"
                  modelled
                  sub={view.phase1_hub?.name}
                />
                {(view.hospital_name || view.hospital_location) && (
                  // Only when the response takes the patient to hospital (not
                  // a scene-only police, fire or rescue response).
                  <Stat
                    label="Then to hospital"
                    value={view.eta_minutes ?? '--'}
                    unit="min"
                    modelled
                    sub={`${fmt.km(view.distance_meters)} km`}
                  />
                )}
              </>
            ) : (
              <Stat
                label="Arriving at hospital in"
                value={liveEtaMinutes(track, 'to_hospital') ?? view.eta_minutes ?? '--'}
                unit="min"
                tone="signal"
                modelled
                sub={view.hospital_name}
              />
            )}
          </div>
        )}
      </Panel>

      {!view && (
        // No emergency yet: the city map, so the hospitals are visible.
        <Panel className="overflow-hidden">
          <MapOverlay
            hospitals={hospitals}
            osmHospitals={osmHospitals}
            height="min(46vh, 400px)"
            className="!rounded-none !border-0"
          />
        </Panel>
      )}

      {view && (
        <Panel className="overflow-hidden">
          <MapOverlay
            hospitals={hospitals}
            osmHospitals={osmHospitals}
            height="min(46vh, 400px)"
            routeCoordinates={view.route_coordinates}
            phase1Coordinates={view.phase1_route_coordinates}
            origin={view.origin}
            hospital={
              view.hospital_location
                ? { ...view.hospital_location, name: view.hospital_name }
                : null
            }
            hub={view.phase1_hub}
            activePhase={incident?.status === 'patient_picked_up' ? 2 : 1}
            unit={track.unit}
            trail={track.trail}
            simSpeed={track.simSpeed}
            className="!rounded-none !border-0"
          />
        </Panel>
      )}

      {incident?.incident_id && (
        <LiveEmergencyChat
          incidentId={incident.incident_id}
          senderRole="citizen"
          senderName="Caller"
        />
      )}

      <button
        onClick={() => {
          setStage('report');
          setIncident(null);
          setPollUrl(null);
          setDetails('');
          setUsedVoice(false);
          setError(null);
        }}
        className="tap w-full rounded-sm border border-rule bg-paper py-3 t-tag text-text-muted hover:text-text"
      >
        Report another emergency
      </button>
    </div>,
  );
}
