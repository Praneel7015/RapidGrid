import { useEffect, useMemo, useState } from 'react';

/** Poll a JSON endpoint every `everyMs` (0: once), keeping the last good
 * answer through failures. `pick` takes the list out of the response. */
function usePolled(url, everyMs, pick) {
  const [items, setItems] = useState([]);
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) setItems(pick(data) ?? []);
        }
      } catch {
        // keep the last answer
      } finally {
        inFlight = false;
      }
    };
    load();
    const timer = everyMs ? setInterval(load, everyMs) : null;
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [url, everyMs]); // eslint-disable-line react-hooks/exhaustive-deps
  return items;
}

/** The whole fleet, every 5 s (dispatch only), without `exceptUnit` (a map
 * draws the unit on the selected call itself, with its trail). */
export function useFleet(exceptUnit = null) {
  const units = usePolled('/api/units', 5000, (d) => d.units);
  return useMemo(() => units.filter((u) => u.unit_id !== exceptUnit), [units, exceptUnit]);
}

/** Every hospital the service knows, with whether it is taking patients
 * (every 15 s, so a divert shows up on the map). */
export function useHospitals() {
  return usePolled('/api/hospital/list', 15000, (d) => d.hospitals);
}

/** Every emergency station (fixed: loaded once). */
export function useStations() {
  return usePolled('/api/stations', 0, (d) => d.stations);
}

/** Every OpenStreetMap hospital in Bangalore, tagged by area (loaded once,
 * read from a saved file on the server, so it works without the internet). */
export function useOsmHospitals() {
  return usePolled('/api/city/osm-hospitals', 0, (d) => d.hospitals);
}
