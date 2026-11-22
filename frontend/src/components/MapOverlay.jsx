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
  unit: '#1d5fd1',
  trail: '#1d5fd1',
  proposal: '#178a4a',
  alternative: '#6b6872',
  alert: '#c23a2b',
  corridor: '#178a4a',
};

function marker(kind, label) {
  return L.divIcon({
    className: '',
    html: `<div class="rg-marker" style="width:26px;height:26px;background:${COLOR[kind]}">${label}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

// Popup content is built from text nodes, never HTML strings: hospital and
// road names come from OpenStreetMap and the API, and must not be parsed as
// markup. Each line is [text] or [text, 'b' | 'i'] for bold or italic.
function popup(...lines) {
  const root = document.createElement('div');
  lines.forEach(([text, style], index) => {
    if (index > 0) root.appendChild(document.createElement('br'));
    const node = style ? document.createElement(style) : document.createElement('span');
    node.textContent = String(text ?? '');
    root.appendChild(node);
  });
  return root;
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
  unit = null,
  trail = [],
  simSpeed = null,
  proposals = [],
  alertPoint = null,
  corridor = [],
  focusKey = null,
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

    // Mark user interaction so we stop overriding their zoom/pan. Only input
    // events count: 'zoomstart' also fires for our own fitBounds, which made
    // the first automatic fit look like a user zoom.
    const onInteract = () => { userInteracted.current = true; };
    const container = map.current.getContainer();
    const userEvents = ['wheel', 'touchstart', 'dblclick', 'keydown'];
    userEvents.forEach((name) => container.addEventListener(name, onInteract, { passive: true }));
    const zoomButtons = container.querySelector('.leaflet-control-zoom');
    // Capture phase: Leaflet stops propagation on the +/- anchors, so a
    // bubbling listener here never sees those clicks.
    zoomButtons?.addEventListener('click', onInteract, true);
    map.current.on('dragstart', onInteract);

    // A map created inside a container that is hidden or still being sized
    // renders grey tiles until it is told to re-measure.
    const invalidate = () => map.current && map.current.invalidateSize();
    const raf = requestAnimationFrame(invalidate);
    const observer = new ResizeObserver(invalidate);
    observer.observe(holder.current);

    return () => {
      userEvents.forEach((name) => container.removeEventListener(name, onInteract));
      zoomButtons?.removeEventListener('click', onInteract, true);
      cancelAnimationFrame(raf);
      observer.disconnect();
      if (map.current) {
        map.current.remove();
        map.current = null;
        layers.current = null;
        unitLayer.current = null;
        unitMarker.current = null;
        trailLine.current = null;
      }
    };
  }, []);

  // When the incident itself changes (new route, hospital divert), reset the
  // fit guard so the new route is automatically framed. The route geometry is
  // part of the key: a reroute between the same pins must be reframed too.
  // Every point counts, in either accepted shape, so a detour that keeps the
  // same endpoints still changes the key.
  const lineKey = (coords) => toLatLngs(coords).map(([lat, lng]) => `${lat},${lng}`).join(';');
  const routeKey = [
    origin?.lat, origin?.lng,
    hospital?.lat, hospital?.lng,
    hub?.lat, hub?.lng,
    lineKey(routeCoordinates), lineKey(phase1Coordinates),
    // A new monitoring alert re-frames the map on the unit and the
    // proposed routes, even if the user had zoomed elsewhere.
    focusKey ?? '',
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
          popup(
            [seg.road ?? 'Road', 'b'],
            [`Congestion x${(seg.multiplier ?? 1).toFixed(2)}`],
            ['Displaced by a nearby closure (modelled)', 'i'],
          ),
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
          .bindPopup(popup(['Road closed', 'b'], [segment.road ?? ''], ['Excluded from routing']))
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
        .bindPopup(popup(['Phase 1', 'b'], ['Station hub to patient']))
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
        .bindPopup(popup(['Phase 2', 'b'], ['Patient to hospital ER']))
        .addTo(group);
      bounds.push(...p2);
    }

    const pin = (point, kind, label, content) => {
      if (!point || !Number.isFinite(point.lat) || !Number.isFinite(point.lng)) return;
      L.marker([point.lat, point.lng], { icon: marker(kind, label) })
        .bindPopup(content)
        .addTo(group);
      bounds.push([point.lat, point.lng]);
    };

    pin(hub, 'hub', 'H', popup([hub?.name ?? 'Station hub', 'b'], ['Responding unit base']));
    pin(origin, 'patient', 'P', popup(['Patient location', 'b']));
    pin(hospital, 'hospital', 'ER', popup([hospital?.name ?? 'Destination hospital', 'b'], ['Receiving ER']));

    // The monitoring agent's proposals: the recommended route (solid green)
    // and alternatives (grey, dashed), where the alert fired, and the
    // junctions in the green-corridor request.
    proposals.forEach((p, i) => {
      const pts = toLatLngs(p.coordinates);
      if (pts.length < 2) return;
      L.polyline(pts, {
        color: i === 0 ? COLOR.proposal : COLOR.alternative,
        weight: i === 0 ? 5 : 3.5,
        opacity: i === 0 ? 0.9 : 0.7,
        dashArray: i === 0 ? null : '6 6',
        lineCap: 'round',
      })
        .bindPopup(popup([p.label ?? 'Proposed route', 'b'],
          [Number.isFinite(p.eta_s) ? `${(p.eta_s / 60).toFixed(1)} min (modelled)` : '']))
        .addTo(group);
      bounds.push(...pts);
    });
    corridor.forEach((j) => {
      if (!Number.isFinite(j.lat) || !Number.isFinite(j.lng)) return;
      L.circleMarker([j.lat, j.lng], { radius: 5, color: COLOR.corridor, weight: 2, fillOpacity: 0.9 })
        .bindPopup(popup(['Green-corridor junction', 'b'], [j.road ?? ''], [`in ${j.eta_min} min (request, not signal control)`]))
        .addTo(group);
    });
    if (alertPoint && Number.isFinite(alertPoint.lat) && Number.isFinite(alertPoint.lng)) {
      L.marker([alertPoint.lat, alertPoint.lng], { icon: marker('alert', '!') })
        .bindPopup(popup(['Alert raised here', 'b']))
        .addTo(group);
    }

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
  }, [routeCoordinates, phase1Coordinates, origin, hospital, hub, closures, congestion, activePhase, proposals, alertPoint, corridor]);

  // The responding unit and its trajectory live on their own layer and are
  // moved in place on every poll, so an open popup stays open. Not part of
  // the fit, so a moving unit never re-zooms the map.
  const unitLayer = useRef(null);
  const unitMarker = useRef(null);
  const trailLine = useRef(null);
  useEffect(() => {
    if (!map.current) return;
    if (!unitLayer.current) unitLayer.current = L.layerGroup().addTo(map.current);

    const path = toLatLngs(trail);
    if (path.length >= 2) {
      if (trailLine.current) trailLine.current.setLatLngs(path);
      else {
        trailLine.current = L.polyline(path, { color: COLOR.trail, weight: 3, opacity: 0.55, lineCap: 'round' })
          .bindPopup(popup(['Trajectory', 'b'], ['Recent position fixes']))
          .addTo(unitLayer.current);
      }
    } else if (trailLine.current) {
      trailLine.current.remove();
      trailLine.current = null;
    }

    const pos = unit?.position;
    if (pos && Number.isFinite(pos.lat) && Number.isFinite(pos.lng)) {
      const source =
        pos.source === 'gps'
          ? `Live GPS${Number.isFinite(pos.accuracy_m) ? ` ±${Math.round(pos.accuracy_m)} m` : ''}`
          : pos.source === 'simulated'
            ? `Simulated GPS${simSpeed ? ` ×${simSpeed}` : ''}`
            : 'At station';
      const content = popup([unit.unit_id ?? 'Unit', 'b'], [source]);
      if (unitMarker.current) {
        unitMarker.current.setLatLng([pos.lat, pos.lng]);
        unitMarker.current.setPopupContent(content);
      } else {
        unitMarker.current = L.marker([pos.lat, pos.lng], { icon: marker('unit', 'U'), zIndexOffset: 1000 })
          .bindPopup(content)
          .addTo(unitLayer.current);
      }
    } else if (unitMarker.current) {
      unitMarker.current.remove();
      unitMarker.current = null;
    }
  }, [unit, trail, simSpeed]);

  return (
    <div
      ref={holder}
      style={{ height, width: '100%' }}
      className={`${theme === 'ink' ? 'map-ink' : 'map-tinted'} overflow-hidden rounded-md border border-rule ${className}`}
    />
  );
}
