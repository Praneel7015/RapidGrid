/**
 * Incident channel.
 *
 * One thread per incident. WebSocket first with HTTP poll fallback.
 * Mic for voice-to-text; optional tel: call targets in the header.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Send } from 'lucide-react';
import { Panel, PanelHead } from './ui';
import MicButton from './MicButton';
import CallButton from './CallButton';
import useSpeechToText from '../hooks/useSpeechToText';

const ROLE_LABEL = {
  citizen: 'Caller',
  driver: 'Responder',
  dispatcher: 'Dispatch',
  hospital: 'ER desk',
};

export default function LiveEmergencyChat({
  incidentId,
  senderRole = 'citizen',
  senderName,
  height = '260px',
  contacts = null,
}) {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState('');
  const [transport, setTransport] = useState('connecting');
  const socket = useRef(null);
  const stream = useRef(null);
  const speech = useSpeechToText({ lang: 'en-IN' });

  const displayName = senderName ?? ROLE_LABEL[senderRole] ?? 'User';

  useEffect(() => {
    speech.setOnFinal((finalText) => {
      setText((prev) => (prev ? `${prev.trim()} ${finalText}` : finalText).trim());
    });
  }, [speech.setOnFinal]);

  const scrollDown = useCallback(() => {
    const el = stream.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(scrollDown, [messages, scrollDown]);

  useEffect(() => {
    if (!incidentId) return;
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`/api/chat/${incidentId}/messages`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setMessages(data.messages ?? []);
      } catch {
        if (!cancelled) setTransport('offline');
      }
    };
    load();
    const timer = setInterval(load, 3000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [incidentId]);

  useEffect(() => {
    if (!incidentId) return;
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws;
    try {
      ws = new WebSocket(`${proto}//${window.location.host}/ws/chat/${incidentId}`);
    } catch {
      setTransport('polling');
      return;
    }
    socket.current = ws;
    ws.onopen = () => setTransport('live');
    ws.onclose = () => setTransport('polling');
    ws.onerror = () => setTransport('polling');
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        setMessages((prev) =>
          prev.some(
            (m) =>
              (msg.id && m.id === msg.id) ||
              (m.message === msg.message &&
                m.sender_role === msg.sender_role &&
                m.timestamp === msg.timestamp),
          )
            ? prev
            : [...prev, msg],
        );
      } catch { /* non-JSON frame */ }
    };
    return () => {
      ws.close();
      socket.current = null;
    };
  }, [incidentId]);

  const send = async (e) => {
    e?.preventDefault();
    speech.stop();
    const body = text.trim();
    if (!body || !incidentId) return;
    setText('');

    const payload = {
      incident_id: incidentId,
      sender_role: senderRole,
      sender_name: displayName,
      message: body,
    };

    try {
      const res = await fetch(`/api/chat/${incidentId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error();
    } catch {
      setTransport('offline');
      setText(body);
    }
  };

  const transportCopy = {
    live: { label: 'Live', tone: 'text-text' },
    polling: { label: 'Delayed', tone: 'text-signal-hover' },
    connecting: { label: 'Connecting', tone: 'text-text-faint' },
    offline: { label: 'Offline', tone: 'text-critical' },
  }[transport];

  const callTargets = [];
  if (contacts) {
    if (senderRole !== 'driver' && contacts.unit) {
      callTargets.push({ phone: contacts.unit, label: 'Unit' });
    }
    if (senderRole !== 'hospital' && contacts.hospital) {
      callTargets.push({ phone: contacts.hospital, label: 'ER' });
    }
    if (senderRole !== 'citizen' && contacts.citizen) {
      callTargets.push({ phone: contacts.citizen, label: 'Caller' });
    }
  }

  return (
    <Panel className="flex flex-col overflow-hidden">
      <PanelHead
        label="Incident channel"
        right={
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            {callTargets.map((t) => (
              <CallButton key={t.label} phone={t.phone} label={t.label} size="sm" />
            ))}
            <span className={`t-meta font-bold ${transportCopy.tone}`}>
              {transportCopy.label}
            </span>
          </div>
        }
      />

      <div ref={stream} className="flex-1 space-y-2.5 overflow-y-auto px-4 py-3" style={{ height }}>
        {messages.length === 0 ? (
          <p className="py-8 text-center text-[12px] text-text-faint">
            No messages yet. Anyone working this incident can write here — or dictate.
          </p>
        ) : (
          messages.map((m, i) => {
            const mine = m.sender_role === senderRole;
            return (
              <div key={m.id ?? i} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                <div className={`max-w-[82%] ${mine ? 'text-right' : ''}`}>
                  <div className="mb-1 flex items-center gap-1.5 t-meta uppercase tracking-[0.1em] text-text-faint">
                    <span className="font-bold">
                      {ROLE_LABEL[m.sender_role] ?? m.sender_role}
                    </span>
                    {m.timestamp && <span>{m.timestamp}</span>}
                  </div>
                  <div
                    className={`inline-block rounded-sm border px-3 py-2 text-left text-[12.5px] leading-relaxed ${
                      mine
                        ? 'border-ink bg-ink text-on-ink'
                        : 'border-rule bg-paper-sunk text-text'
                    }`}
                  >
                    {m.message}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      <form onSubmit={send} className="flex items-center gap-2 border-t border-rule p-2.5">
        <input
          value={speech.listening && speech.interim ? `${text} ${speech.interim}`.trim() : text}
          onChange={(e) => setText(e.target.value)}
          placeholder={speech.listening ? 'Listening…' : 'Type or dictate a message…'}
          className="min-w-0 flex-1 rounded-sm border border-rule bg-paper px-3 py-2.5 text-[16px] text-text placeholder:text-text-faint focus:border-signal focus:outline-none lg:py-2 lg:text-[13px]"
        />
        <MicButton
          supported={speech.supported}
          listening={speech.listening}
          onToggle={speech.toggle}
        />
        <button
          type="submit"
          disabled={!text.trim()}
          aria-label="Send message"
          className="tap grid h-11 w-11 shrink-0 place-items-center rounded-sm border border-ink bg-signal text-ink transition-colors hover:bg-ink hover:text-signal disabled:opacity-40 lg:h-9 lg:w-9"
        >
          <Send size={15} />
        </button>
      </form>
    </Panel>
  );
}
