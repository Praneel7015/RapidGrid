/**
 * Citizen emergency app.
 *
 * Sized for crisis: one unmistakable action, short sentences, voice dictation
 * on the report form, and click-to-call once a unit/hospital is assigned.
 */

import React, { useState, useEffect, useRef } from 'react';
import { useAuth } from '../context/AuthContext';
import { MapPin, Phone, Check, AlertTriangle, ChevronLeft } from 'lucide-react';
import MapOverlay from '../components/MapOverlay';
import LiveEmergencyChat from '../components/LiveEmergencyChat';
import MicButton from '../components/MicButton';
import CallButton from '../components/CallButton';
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

  const [stage, setStage] = useState('home'); // home | report | sending | tracking | failed
  const [service, setService] = useState('Ambulance');
  const [details, setDetails] = useState('');
  const [usedVoice, setUsedVoice] = useState(false);
  const [coords, setCoords] = useState(null);
  const [locationLabel, setLocationLabel] = useState('Locating you…');
  const [error, setError] = useState(null);
  const [step, setStep] = useState(0);

  const [pollUrl, setPollUrl] = useState(null);
  const [incident, setIncident] = useState(null);
  const view = incident?.citizen_view ?? null;

  const speech = useSpeechToText({ lang: 'en-IN' });
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  useEffect(() => {
    speech.setOnFinal((finalText) => {
      setUsedVoice(true);
      setDetails((prev) => (prev ? `${prev.trim()} ${finalText}` : finalText).trim());
    });
  }, [speech.setOnFinal]);

  useEffect(() => {
    if (!navigator.geolocation) {
      setCoords(FALLBACK_LOCATION);
      setLocationLabel('Using Bengaluru city centre — GPS unavailable');
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (!mounted.current) return;
        setCoords({ lat: pos.coords.latitude, lng: pos.coords.longitude });
        setLocationLabel(
          `${pos.coords.latitude.toFixed(4)}°N, ${pos.coords.longitude.toFixed(4)}°E`,
        );
      },
      () => {
        if (!mounted.current) return;
        setCoords(FALLBACK_LOCATION);
        setLocationLabel('Using Bengaluru city centre — location not shared');
      },
      { timeout: 8000 },
    );
  }, []);

  useEffect(() => {
    if (stage !== 'home' || !citizenInfo?.phone) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/incidents/');
        if (!res.ok) return;
        const { incidents = [] } = await res.json();
        const mine = incidents.filter(
          (i) => i.citizen_phone?.trim() === citizenInfo.phone.trim(),
        );
        const latest = mine[mine.length - 1];
        // Only rejoin in-flight work. Failed/completed reports must not trap the home screen.
        const active = latest && !['failed', 'completed'].includes(latest.status);
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
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        setIncident(data);
        if (data.status === 'failed') {
          setError(data.error || 'Dispatch could not complete your report.');
          setStage('failed');
          return;
        }
        if (data.citizen_view) setStage('tracking');
      } catch {
        if (!cancelled) setError('Lost connection to dispatch. Retrying…');
      }
    };
    poll();
    const timer = setInterval(poll, 2500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [pollUrl, stage]);

  const sendSOS = async () => {
    speech.stop();
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
          citizen_text: details.trim() || `Emergency reported, ${service} requested.`,
          citizen_name: citizenInfo?.name ?? 'Anonymous',
          citizen_phone: citizenInfo?.phone ?? 'Unregistered',
          vehicle_required: service,
          location: coords ?? FALLBACK_LOCATION,
          input_modality: usedVoice ? 'voice' : 'text',
          timestamp: new Date().toISOString(),
        }),
      });
      const data = await res.json();
      if (res.ok || res.status === 202) {
        setPollUrl(data.poll_url ?? `/api/incidents/${data.incident_id}`);
      } else {
        setError(data.detail ?? 'Dispatch rejected the report. Try again.');
        setStage('report');
      }
    } catch {
      setError('Cannot reach emergency dispatch. Check your connection and try again.');
      setStage('report');
    }
  };

  const shell = (children) => (
    <div className="mx-auto w-full max-w-[520px] px-4 py-5 lg:max-w-[720px]">{children}</div>
  );

  if (stage === 'home') {
    return shell(
      <div className="flex min-h-[calc(100vh-120px)] flex-col">
        <div className="mb-6">
          <h1 className="font-display text-[clamp(22px,6vw,28px)] font-semibold leading-tight text-text">
            Need emergency help?
          </h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-text-muted">
            Tap SOS to send your location to city dispatch. You can add details next — including by voice.
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

        <a
          href="tel:112"
          className="tap mt-6 flex items-center justify-center gap-2 rounded-sm border border-ink bg-paper py-4 t-tag text-text hover:bg-ink hover:text-on-ink"
        >
          <Phone size={15} /> Or call 112
        </a>
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

        <label className="mt-5 block">
          <div className="flex items-center justify-between gap-2">
            <span className="eyebrow">What is happening?</span>
            <MicButton
              supported={speech.supported}
              listening={speech.listening}
              onToggle={speech.toggle}
            />
          </div>
          <textarea
            value={speech.listening && speech.interim ? `${details} ${speech.interim}`.trim() : details}
            onChange={(e) => setDetails(e.target.value)}
            rows={4}
            placeholder="Chest pain, difficulty breathing, road accident… or tap the mic"
            className="mt-2 w-full resize-none rounded-sm border border-rule bg-paper p-3 text-[16px] leading-relaxed text-text placeholder:text-text-faint focus:border-signal focus:outline-none lg:text-[14px]"
          />
          <span className="mt-2 block t-micro text-text-faint">
            {speech.listening
              ? 'Listening… speak clearly.'
              : 'The more specific you are, the better the hospital match. You can dictate.'}
          </span>
        </label>

        <div className="mt-4 flex items-center gap-2 rounded-sm border border-rule bg-paper-sunk px-3 py-2.5">
          <MapPin size={14} className="shrink-0 text-signal" />
          <span className="t-meta text-text-muted">{locationLabel}</span>
        </div>

        {error && stage === 'report' && (
          <div className="mt-3 flex items-start gap-2 rounded-sm border border-critical-edge bg-critical-wash px-3 py-2.5">
            <AlertTriangle size={14} className="mt-px shrink-0 text-critical" />
            <p className="text-[12px] leading-relaxed text-critical">{error}</p>
          </div>
        )}

        <Button onClick={sendSOS} size="lg" className="mt-5 w-full">
          {stage === 'failed' ? 'Try again' : 'Send emergency report'}
        </Button>

        <a
          href="tel:112"
          className="tap mt-3 flex w-full items-center justify-center gap-2 rounded-sm border border-rule py-3 t-tag text-text-muted hover:text-text"
        >
          <Phone size={14} /> Call 112 instead
        </a>
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
          <p className="mt-1.5 text-[13px] text-text-muted">Stay on this screen.</p>
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
  const unitPhone = incident?.unit_phone ?? view?.unit_phone ?? null;
  const hospitalPhone = incident?.hospital_phone ?? view?.hospital_phone ?? null;

  return shell(
    <div className="space-y-3">
      <Panel>
        <PanelHead
          label={`Incident ${incident?.incident_id ?? ''}`}
          right={<StatusTag status={incident?.status} />}
        />
        <div className="px-4 py-3.5">
          <p className="text-[14px] font-semibold leading-snug text-text">
            {dispatched
              ? `A ${incident.vehicle_required?.toLowerCase() ?? 'unit'} is on the way to you.`
              : 'Your report is with the city dispatcher for approval.'}
          </p>
          {view?.hospital_name && (
            <p className="mt-1.5 text-[13px] leading-relaxed text-text-muted">
              You will be taken to <span className="font-semibold text-text">{view.hospital_name}</span>.
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <CallButton phone={unitPhone} label="Call unit" variant="primary" />
            <CallButton phone={hospitalPhone} label="Call ER" />
            <CallButton phone="112" label="Call 112" />
          </div>
        </div>

        {view && (
          <div className="flex flex-wrap items-center gap-x-8 gap-y-4 border-t border-rule px-4 py-3.5">
            <Stat
              label="Unit reaches you in"
              value={view.phase1_eta_minutes ?? '--'}
              unit="min"
              tone="signal"
              sub={view.phase1_hub?.name}
            />
            <Stat
              label="Then to hospital"
              value={view.eta_minutes ?? '--'}
              unit="min"
              sub={`${fmt.km(view.distance_meters)} km`}
            />
          </div>
        )}
      </Panel>

      {view && (
        <Panel className="overflow-hidden">
          <MapOverlay
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
            className="!rounded-none !border-0"
          />
        </Panel>
      )}

      {incident?.incident_id && (
        <LiveEmergencyChat
          incidentId={incident.incident_id}
          senderRole="citizen"
          senderName={citizenInfo?.name}
          contacts={{
            unit: unitPhone,
            hospital: hospitalPhone,
            citizen: citizenInfo?.phone,
          }}
        />
      )}

      <button
        onClick={() => {
          setStage('home');
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
