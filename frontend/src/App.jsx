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
import GuidedDemo from './views/GuidedDemo';
import ControlRoom from './views/ControlRoom';
import Seats, { SeatBanner } from './views/Seats';

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

// Showcase access token removed: every page opens without one.
function SessionGate({ children }) {
  return children;
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
            <Route path="/demo" element={<GuidedDemo />} />
            <Route path="/seats" element={<Seats />} />
            <Route path="/control" element={<DemoRole role="dispatcher"><SeatBanner /><ControlRoom /></DemoRole>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </SessionGate>
    </AuthProvider>
  );
}
