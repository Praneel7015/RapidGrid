/**
 * Tactical map.
 *
 * Replaces a component that could draw exactly one polyline and no markers -
 * which meant the product's headline feature, two-phase routing, was
 * structurally impossible to show.
 *
 * This renders:
 *   Phase 1  station hub -> patient      (dashed: the unit is still coming)
 *   Phase 2  patient -> hospital ER      (solid: the transport leg)
 *   markers  hub / patient / hospital, drawn from the design system
 *   closures blocked road segments, when the city model reports any
 *
 * Tiles are OpenStreetMap - the only free, key-free source still serving real
 * imagery - desaturated and tinted via CSS so the map belongs to this product
 * rather than looking like an embedded third-party widget.
 */

import React, { useEffect, useRef } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILE_ATTR = '&copy; OpenStreetMap contributors';

const COLOR = {
  phase1: '#3a3842',
  phase2: '#e8a020',
  closure: '#c23a2b',
  congestion: '#c23a2b',
  hub: '#2e2c32',
  patient: '#e8a020',
  hospital: '#2e2c32',
};

function marker(kind, label) {
  return L.divIcon({
    className: '',
    html: `<div class="rg-marker" style="width:26px;height:26px;background:${COLOR[kind]}">${label}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

function toLatLngs(coords) {
  if (!Array.isArray(coords)) return [];
  return coords
    .map((c) =>
      Array.isArray(c)
        ? [c[0], c[1]]
        : c && Number.isFinite(c.lat) && Number.isFinite(c.lng)
          ? [c.lat, c.lng]
          : null,
    )
    .filter(Boolean);
}

export default function MapOverlay({
  routeCoordinates,
  phase1Coordinates,
  origin,
  hospital,
  hub,
  closures = [],
  congestion = [],
  activePhase = 2,
  height = '400px',
  theme = 'light',
  className = '',
}) {
  const holder = useRef(null);
  const map = useRef(null);
  const layers = useRef(null);
  // Track whether we have ever fit the bounds from real data. Once the user
  // has interacted (zoom/pan), we stop auto-fitting so their view is preserved.
  const hasFit = useRef(false);
  const userInteracted = useRef(false);

  // Create the map exactly once. The previous version never called remove(),
  // which throws "Map container is already initialized" on React's dev
  // double-mount and leaks a map instance on every route change.
  useEffect(() => {
    if (map.current || !holder.current) return;

    map.current = L.map(holder.current, {
      zoomControl: false,
      attributionControl: true,
      preferCanvas: true,
    }).setView([12.9716, 77.5946], 12);

    L.tileLayer(TILE_URL, { attribution: TILE_ATTR, maxZoom: 19 }).addTo(map.current);
    L.control.zoom({ position: 'bottomright' }).addTo(map.current);
    layers.current = L.layerGroup().addTo(map.current);

    // Mark user interaction so we stop overriding their zoom/pan.
    const onInteract = () => { userInteracted.current = true; };
    map.current.on('zoomstart', onInteract);
    map.current.on('dragstart', onInteract);

    // A map created inside a container that is hidden or still being sized
    // renders grey tiles until it is told to re-measure.
    const invalidate = () => map.current && map.current.invalidateSize();
    const raf = requestAnimationFrame(invalidate);
    const observer = new ResizeObserver(invalidate);
    observer.observe(holder.current);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      if (map.current) {
        map.current.remove();
        map.current = null;
        layers.current = null;
      }
    };
  }, []);

  // When the incident itself changes (new route, hospital divert), reset the
  // fit guard so the new route is automatically framed.
  const routeKey = [
    origin?.lat, origin?.lng,
    hospital?.lat, hospital?.lng,
    hub?.lat, hub?.lng,
  ].join(',');
  const prevRouteKey = useRef(routeKey);
  useEffect(() => {
    if (prevRouteKey.current !== routeKey) {
      hasFit.current = false;
      userInteracted.current = false;
      prevRouteKey.current = routeKey;
    }
  }, [routeKey]);

  useEffect(() => {
    const group = layers.current;
    group.clearLayers();

    const p1 = toLatLngs(phase1Coordinates);
    const p2 = toLatLngs(routeCoordinates);
    const bounds = [];

    // Displaced congestion sits at the very bottom - it is the state of the
    // city, not a property of this route. Redder means more displaced traffic.
    congestion.forEach((seg) => {
      const pts = toLatLngs(seg.coordinates ?? seg);
      if (pts.length < 2) return;
      const severity = Math.min(1, ((seg.multiplier ?? 1) - 1) / 1.4);
      L.polyline(pts, {
        color: COLOR.congestion,
        weight: 3 + severity * 6,
        opacity: 0.18 + severity * 0.5,
        lineCap: 'round',
      })
        .bindPopup(
          `<b>${seg.road ?? 'Road'}</b><br>Congestion x${(seg.multiplier ?? 1).toFixed(2)}` +
            '<br><i>Displaced by a nearby closure (modelled)</i>',
        )
        .addTo(group);
    });

    // Closures next, so route lines draw over them.
    closures.forEach((segment) => {
      const pts = toLatLngs(segment.coordinates ?? segment);
      if (pts.length >= 2) {
        L.polyline(pts, {
          color: COLOR.closure,
          weight: 7,
          opacity: 0.9,
          dashArray: '2 7',
          lineCap: 'butt',
        })
          .bindPopup(`<b>Road closed</b><br>${segment.road ?? ''}<br>Excluded from routing`)
          .addTo(group);
        bounds.push(...pts);
      }
    });

    // Phase 1: the unit is still en route to the patient, so it reads as
    // provisional - dashed, cooler, thinner.
    if (p1.length >= 2) {
      L.polyline(p1, {
        color: COLOR.phase1,
        weight: activePhase === 1 ? 5 : 3.5,
        opacity: activePhase === 1 ? 0.95 : 0.5,
        dashArray: '7 6',
        lineCap: 'round',
      })
        .bindPopup('<b>Phase 1</b><br>Station hub to patient')
        .addTo(group);
      bounds.push(...p1);
    }

    // Phase 2: the transport leg. Solid, signal-coloured, the primary line.
    if (p2.length >= 2) {
      L.polyline(p2, {
        color: '#ffffff',
        weight: activePhase === 2 ? 9 : 7,
        opacity: 0.85,
        lineCap: 'round',
        lineJoin: 'round',
      }).addTo(group);
      L.polyline(p2, {
        color: COLOR.phase2,
        weight: activePhase === 2 ? 5.5 : 4,
        opacity: activePhase === 2 ? 1 : 0.6,
        lineCap: 'round',
        lineJoin: 'round',
      })
        .bindPopup('<b>Phase 2</b><br>Patient to hospital ER')
        .addTo(group);
      bounds.push(...p2);
    }

    const pin = (point, kind, label, popup) => {
      if (!point || !Number.isFinite(point.lat) || !Number.isFinite(point.lng)) return;
      L.marker([point.lat, point.lng], { icon: marker(kind, label) })
        .bindPopup(popup)
        .addTo(group);
      bounds.push([point.lat, point.lng]);
    };

    pin(hub, 'hub', 'H', `<b>${hub?.name ?? 'Station hub'}</b><br>Responding unit base`);
    pin(origin, 'patient', 'P', '<b>Patient location</b>');
    pin(
      hospital,
      'hospital',
      'ER',
      `<b>${hospital?.name ?? 'Destination hospital'}</b><br>Receiving ER`,
    );

    if (bounds.length >= 2) {
      try {
        // Only auto-fit on the first load with real data, or when the route
        // changes to a completely different set of coordinates (e.g. hospital
        // divert). Once the user has zoomed/panned, leave their view alone.
        const shouldFit = !hasFit.current || !userInteracted.current;
        if (shouldFit) {
          map.current.fitBounds(L.latLngBounds(bounds), { padding: [44, 44], maxZoom: 15 });
          hasFit.current = true;
        }
      } catch {
        /* bounds can be degenerate while data is still arriving */
      }
    }
  }, [routeCoordinates, phase1Coordinates, origin, hospital, hub, closures, congestion, activePhase]);

  return (
    <div
      ref={holder}
      style={{ height, width: '100%' }}
      className={`${theme === 'ink' ? 'map-ink' : 'map-tinted'} overflow-hidden rounded-md border border-rule ${className}`}
    />
  );
}
