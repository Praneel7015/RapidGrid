import { useEffect, useRef, useState } from 'react';

// The server treats a phone fix as current for 15 s; refresh well inside that.
const REFRESH_MS = 5000;
const MIN_POST_MS = 2000;

/**
 * Sends this phone's GPS as the unit's position while `enabled`.
 *
 * watchPosition delivers fixes while moving; a phone standing still may send
 * none, so a fresh reading (never a cached one) is also requested every 5 s.
 * At most one fix is posted every 2 s. Phones stop GPS when the screen
 * sleeps, so a Wake Lock keeps the screen on while sharing. Returns
 * { status: off | waiting | sharing | error, accuracy, error } for the screen;
 * the status reflects what the server accepted, not just the local reading.
 */
export default function useShareLocation(unitId, enabled) {
  const [state, setState] = useState({ status: 'off', accuracy: null, error: null });
  const lastSent = useRef(0);

  useEffect(() => {
    if (!enabled || !unitId) {
      setState({ status: 'off', accuracy: null, error: null });
      return undefined;
    }
    if (!('geolocation' in navigator)) {
      setState({ status: 'error', accuracy: null, error: 'This browser cannot share location.' });
      return undefined;
    }
    let cancelled = false;
    let wakeLock = null;
    const keepAwake = async () => {
      try {
        if ('wakeLock' in navigator && document.visibilityState === 'visible') {
          const sentinel = await navigator.wakeLock.request('screen');
          if (cancelled) sentinel.release().catch(() => {});
          else wakeLock = sentinel;
        }
      } catch {
        // Not fatal: tracking still works while the screen stays on.
      }
    };
    const onVisible = () => { if (document.visibilityState === 'visible') keepAwake(); };
    keepAwake();
    document.addEventListener('visibilitychange', onVisible);
    setState({ status: 'waiting', accuracy: null, error: null });

    const send = async (pos) => {
      if (cancelled) return;
      const { latitude, longitude, accuracy, heading, speed } = pos.coords;
      const now = Date.now();
      if (now - lastSent.current < MIN_POST_MS) return;
      lastSent.current = now;
      try {
        const res = await fetch(`/api/units/${encodeURIComponent(unitId)}/position`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            lat: latitude,
            lng: longitude,
            accuracy_m: Number.isFinite(accuracy) ? Math.min(accuracy, 5000) : null,
            heading: Number.isFinite(heading) ? heading % 360 : null,
            speed_mps: Number.isFinite(speed) && speed >= 0 ? Math.min(speed, 120) : null,
            // When the fix was taken: the server orders fixes by it and
            // drops ones delayed in transit.
            captured_at_ms: Number.isFinite(pos.timestamp) && pos.timestamp > 0 ? pos.timestamp : null,
          }),
        });
        if (cancelled) return;
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          if (cancelled) return;
          if (data.ignored === 'captured too long ago') {
            // Every fix looks minutes old: the phone's clock is wrong, and
            // the server will not use its positions until it is fixed.
            setState({ status: 'error', accuracy, error: "This phone's clock is wrong, so its location is not used. Set date and time to automatic." });
          } else {
            setState({ status: 'sharing', accuracy, error: null });
          }
        }
        else if (res.status !== 429) {
          const data = await res.json().catch(() => ({}));
          setState({ status: 'error', accuracy, error: data.detail || `Position not accepted (HTTP ${res.status})` });
        }
      } catch {
        if (!cancelled) setState({ status: 'error', accuracy, error: 'Connection lost; still trying.' });
      }
    };
    const fail = (err) => {
      if (!cancelled) {
        setState({
          status: 'error',
          accuracy: null,
          error: err.code === 1 ? 'Location permission was denied.' : 'Location is unavailable.',
        });
      }
    };

    const watch = navigator.geolocation.watchPosition(send, fail, {
      enableHighAccuracy: true, maximumAge: 0, timeout: 15000,
    });
    const refresh = setInterval(() => {
      if (Date.now() - lastSent.current >= REFRESH_MS) {
        navigator.geolocation.getCurrentPosition(send, fail, { enableHighAccuracy: true, maximumAge: 0, timeout: 10000 });
      }
    }, REFRESH_MS);

    return () => {
      cancelled = true;
      navigator.geolocation.clearWatch(watch);
      clearInterval(refresh);
      document.removeEventListener('visibilitychange', onVisible);
      wakeLock?.release?.().catch(() => {});
    };
  }, [unitId, enabled]);

  return state;
}
