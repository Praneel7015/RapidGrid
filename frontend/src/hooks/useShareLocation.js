import { useEffect, useRef, useState } from 'react';

/**
 * Sends this phone's GPS as the unit's position while `enabled`.
 *
 * Uses watchPosition and posts at most one fix every 2 s. Phones stop GPS when
 * the screen sleeps, so a Wake Lock keeps the screen on while sharing (it is
 * re-requested when the page becomes visible again). Returns the state for
 * the screen: { status, accuracy, error }.
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
          wakeLock = await navigator.wakeLock.request('screen');
        }
      } catch {
        // Not fatal: tracking still works while the screen stays on.
      }
    };
    const onVisible = () => { if (document.visibilityState === 'visible') keepAwake(); };
    keepAwake();
    document.addEventListener('visibilitychange', onVisible);
    setState({ status: 'waiting', accuracy: null, error: null });

    const watch = navigator.geolocation.watchPosition(
      async (pos) => {
        if (cancelled) return;
        const { latitude, longitude, accuracy, heading } = pos.coords;
        setState({ status: 'sharing', accuracy, error: null });
        const now = Date.now();
        if (now - lastSent.current < 2000) return;
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
            }),
          });
          if (!cancelled && !res.ok && res.status !== 429) {
            const data = await res.json().catch(() => ({}));
            setState({ status: 'error', accuracy, error: data.detail || `Position not accepted (HTTP ${res.status})` });
          }
        } catch {
          if (!cancelled) setState((s) => ({ ...s, error: 'Connection lost; still trying.' }));
        }
      },
      (err) => {
        if (!cancelled) {
          setState({
            status: 'error',
            accuracy: null,
            error: err.code === 1 ? 'Location permission was denied.' : 'Location is unavailable.',
          });
        }
      },
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 },
    );

    return () => {
      cancelled = true;
      navigator.geolocation.clearWatch(watch);
      document.removeEventListener('visibilitychange', onVisible);
      wakeLock?.release?.().catch(() => {});
    };
  }, [unitId, enabled]);

  return state;
}
