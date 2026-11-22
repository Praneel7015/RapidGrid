/*
 * Citizen sign-in: an existing account, a new one, or none at all.
 *
 * An account (phone number + 6-digit PIN) lets a caller find their report
 * again and lets responders reach them. Reporting never requires one.
 * Numbers are not verified by SMS; the form says so.
 */

import React, { useState } from 'react';
import { Button } from './ui';

async function send(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? 'Check the details you entered.' : data.detail;
    throw new Error(detail || `Request failed (HTTP ${res.status})`);
  }
  return data;
}

const field = 'w-full rounded-sm border border-rule bg-paper px-3 py-2.5 text-[15px] text-text';

export default function CitizenAccess({ onSignedIn, onAnonymous }) {
  const [mode, setMode] = useState('signin'); // signin | register
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [pin, setPin] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = mode === 'register'
        ? await send('/api/citizens/register', { name, phone, pin })
        : await send('/api/citizens/session', { phone, pin });
      onSignedIn({ name: data.name, phone: data.phone, registered: true });
    } catch (err) {
      setError(err.message);
      setPin('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 px-4 py-4">
      <div className="flex gap-1" role="tablist">
        {[['signin', 'I have an account'], ['register', 'New here']].map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={mode === key}
            onClick={() => { setMode(key); setError(null); }}
            className={`flex-1 rounded-sm border px-3 py-2 text-[13px] font-semibold ${
              mode === key ? 'border-ink bg-ink text-on-ink' : 'border-rule bg-paper text-text'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      <form onSubmit={submit} className="space-y-3">
        {mode === 'register' && (
          <div>
            <label htmlFor="citizen-name" className="eyebrow block">Your name</label>
            <input id="citizen-name" className={field} value={name} maxLength={80} required
              autoComplete="name" onChange={(e) => setName(e.target.value)} />
          </div>
        )}
        <div>
          <label htmlFor="citizen-phone" className="eyebrow block">Phone number</label>
          <input id="citizen-phone" className={field} value={phone} required inputMode="tel"
            autoComplete="tel" placeholder="+91 98xxx xxxxx" onChange={(e) => setPhone(e.target.value)} />
        </div>
        <div>
          <label htmlFor="citizen-pin" className="eyebrow block">
            {mode === 'register' ? 'Choose a 6-digit PIN' : '6-digit PIN'}
          </label>
          <input id="citizen-pin" className={`${field} font-mono tracking-[0.3em]`} type="password"
            inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required
            autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            value={pin} onChange={(e) => setPin(e.target.value.replace(/[^0-9]/g, ''))} />
        </div>
        {error && <p role="alert" className="text-[12.5px] text-critical">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy || pin.length !== 6}>
          {busy ? 'Please wait…' : mode === 'register' ? 'Create account' : 'Sign in'}
        </Button>
        {mode === 'register' && (
          <p className="text-[11.5px] leading-relaxed text-text-muted">
            The number is not verified by SMS. Staff see it only when they ask, and each look is recorded.
          </p>
        )}
      </form>
      <div className="border-t border-rule pt-3">
        <Button
          variant="quiet"
          className="w-full"
          disabled={busy}
          onClick={async () => {
            if (busy) return;
            setBusy(true);
            setError(null);
            try {
              await onAnonymous();
            } catch (err) {
              setError(err.message || 'Something went wrong. Try again.');
            } finally {
              setBusy(false);
            }
          }}
        >
          Report without an account
        </Button>
        <p className="mt-1.5 text-[11.5px] text-text-muted">In an emergency you never need an account.</p>
      </div>
    </div>
  );
}
