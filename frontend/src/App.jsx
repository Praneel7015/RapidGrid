import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import SharedShell from './components/SharedShell';
import LandingPage from './views/LandingPage';
import Login from './views/Login';
import CitizenDashboard from './views/CitizenDashboard';
import DispatcherDashboard from './views/DispatcherDashboard';
import DriverDashboard from './views/DriverDashboard';
import HospitalDashboard from './views/HospitalDashboard';

/**
 * Demo role gate.
 *
 * Deliberately permissive: visiting /dispatcher signs you in as the
 * dispatcher, so a judge can open four tabs and watch one incident move
 * between roles without juggling logins.
 *
 * This is NOT access control, and it is not presented as such - the login
 * screen says so in as many words. The previous version did the same thing
 * while calling itself ProtectedRoute, which implied a guarantee it never
 * provided. Real deployments need authentication here.
 */
function DemoRole({ role, children }) {
  const { role: current, login } = useAuth();
  React.useEffect(() => {
    if (current !== role) login(role);
  }, [current, role, login]);
  return children;
}

const portal = (role, View) => (
  <DemoRole role={role}>
    <SharedShell>
      <View />
    </SharedShell>
  </DemoRole>
);

function SessionGate({ children }) {
  const [session, setSession] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [token, setToken] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const check = React.useCallback(async () => {
    try {
      const res = await fetch('/api/session', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Access check failed (HTTP ${res.status}).`);
      setSession(await res.json());
      setError(null);
    } catch (err) {
      // Opened from the installed app with no connection: say so plainly.
      setError(navigator.onLine === false
        ? 'You are offline. RapidGrid needs a connection to check access, and will retry when you are back online.'
        : err.message || 'Cannot check showcase access.');
    }
  }, []);
  React.useEffect(() => {
    const reset = () => { setSession(null); check(); };
    check();
    const timer = window.setInterval(check, 30000);
    window.addEventListener('rapidgrid-session-changed', reset);
    window.addEventListener('focus', check);
    window.addEventListener('online', check);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('rapidgrid-session-changed', reset);
      window.removeEventListener('focus', check);
      window.removeEventListener('online', check);
    };
  }, [check]);
  const unlock = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Unlock failed (HTTP ${res.status}).`);
      setToken('');
      await check();
    } catch (err) { setError(err.message || 'Unable to unlock showcase.'); }
    finally { setBusy(false); }
  };
  if (session?.authenticated) return children;
  return (
    <main className="mx-auto max-w-md px-5 py-16">
      <h1 className="font-display text-3xl font-semibold">RapidGrid showcase access</h1>
      <p className="mt-3 text-sm text-text-muted">This demonstration uses a shared operator access token. It does not verify individual identities or provide separate permissions for each role. For real emergencies call 112.</p>
      {session ? <form onSubmit={unlock} className="mt-6 space-y-4">
        <label className="block text-sm">Operator access token
          <input type="password" autoComplete="off" required value={token} onChange={(e) => setToken(e.target.value)} className="mt-2 block w-full rounded-sm border border-rule bg-paper p-3" />
        </label>
        <button disabled={busy} className="tap w-full rounded-sm border border-ink bg-signal p-3 font-semibold">{busy ? 'Unlocking…' : 'Unlock showcase'}</button>
      </form> : <button onClick={check} className="tap mt-5 border border-rule p-3">Retry access check</button>}
      {error && <p role="alert" className="mt-4 text-sm text-critical">{error}</p>}
    </main>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <SessionGate>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<LandingPage />} />
            <Route path="/login" element={<Login />} />
            <Route path="/citizen" element={portal('citizen', CitizenDashboard)} />
            <Route path="/dispatcher" element={portal('dispatcher', DispatcherDashboard)} />
            <Route path="/driver" element={portal('driver', DriverDashboard)} />
            <Route path="/hospital" element={portal('hospital', HospitalDashboard)} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </SessionGate>
    </AuthProvider>
  );
}
