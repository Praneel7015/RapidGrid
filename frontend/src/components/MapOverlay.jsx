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

// Vehicle colours by state (the legend uses the same).
const UNIT_STATE = {
  free: { color: '#178a4a', label: 'Free' },
  busy: { color: '#e8a020', label: 'On a call' },
  off: { color: '#9a97a0', label: 'Off duty' },
};
const TYPE_LETTER = { Ambulance: 'A', 'Fire Engine': 'F', 'Police Cruiser': 'P', 'Disaster Rescue': 'R' };

function unitState(u) {
  if (u.on_duty === false) return 'off';
  return u.status === 'available' ? 'free' : 'busy';
}

function hospitalIcon(onDivert) {
  const bg = onDivert ? '#9a97a0' : '#ffffff';
  const fg = onDivert ? '#ffffff' : '#c23a2b';
  return L.divIcon({
    className: '',
    html: `<div class="rg-hospital" style="width:18px;height:18px;border-radius:4px;background:${bg};color:${fg};border:1.5px solid ${onDivert ? '#6b6872' : '#c23a2b'};font:700 14px/15px system-ui;text-align:center">+</div>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function stationIcon(type) {
  return L.divIcon({
    className: '',
    html: `<div class="rg-station" style="width:16px;height:16px;transform:rotate(45deg);background:#2e2c32;border:1.5px solid #fff;box-shadow:0 0 0 1px #2e2c32"><span style="display:block;transform:rotate(-45deg);color:#fff;font:700 9px/14px system-ui;text-align:center">${TYPE_LETTER[type] ?? 'S'}</span></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

function legendControl() {
  const control = L.control({ position: 'bottomleft' });
  control.onAdd = () => {
    const box = L.DomUtil.create('div', 'rg-legend');
    box.setAttribute('data-testid', 'map-legend');
    box.style.cssText = 'background:rgba(255,255,255,.92);padding:6px 8px;border-radius:6px;font:12px/1.5 system-ui;color:#2e2c32;box-shadow:0 1px 4px rgba(0,0,0,.2)';
    const row = (swatch, text) => {
      const line = L.DomUtil.create('div', '', box);
      line.style.cssText = 'display:flex;align-items:center;gap:6px';
      const mark = L.DomUtil.create('span', '', line);
      mark.style.cssText = swatch;
      const label = L.DomUtil.create('span', '', line);
      label.textContent = text;
    };
    row('width:10px;height:10px;border-radius:2px;border:1.5px solid #c23a2b;background:#fff', 'Hospital');
    row('width:10px;height:10px;border-radius:2px;background:#9a97a0', 'Hospital on divert');
    row('width:9px;height:9px;transform:rotate(45deg);background:#2e2c32', 'Station');
    Object.values(UNIT_STATE).forEach(({ color, label }) => row(`width:10px;height:10px;border-radius:50%;background:${color}`, `Vehicle: ${label.toLowerCase()}`));
    row('width:10px;height:10px;border-radius:50%;border:2px solid #1d5fd1', 'Live GPS');
    L.DomEvent.disableClickPropagation(box);
    return box;
  };
  return control;
}

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

// One shared empty list for omitted props: a fresh [] on every render would
// look like new data and redraw (and close popups on) the route layers.
const NONE = Object.freeze([]);

// A GPS place counts as live only while its fix is recent (the backend stops
// trusting a fix after 15 s). Older places are labelled as such, never "live".
const GPS_FRESH_S = 15;
function gpsFresh(pos) {
  const when = pos?.captured_at ?? pos?.at;
  return pos?.source === 'gps' && Number.isFinite(when) && Date.now() / 1000 - when <= GPS_FRESH_S;
}

export default function MapOverlay({
  routeCoordinates,
  phase1Coordinates,
  origin,
  hospital,
  hub,
  closures = NONE,
  congestion = NONE,
  activePhase = 2,
  unit = null,
  trail = NONE,
  simSpeed = null,
  proposals = NONE,
  alertPoint = null,
  corridor = NONE,
  focusKey = null,
  // Every other unit in the fleet ({unit_id, type, status, position}), and
  // the one the agent proposes as backup, drawn as small markers.
  fleet = NONE,
  highlightUnit = null,
  // The city around the call: every hospital ({hospital_id, name, location,
  // capabilities, beds_total, on_divert}) and every station ({name, type,
  // lat, lng}). Given either, the map gets a legend and layer switches.
  hospitals = NONE,
  osmHospitals = NONE,
  stations = NONE,
  // Every active emergency ({incident_id, location, status, emergency_type,
  // background}) as a marker at its scene: the control room's whole city.
  calls = NONE,
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
  const lastBounds = useRef(null);

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
    // When the box changes size (projector mode, a rotated phone), fit the
    // route again unless the user has moved the map themselves.
    const invalidate = () => {
      if (!map.current) return;
      map.current.invalidateSize();
      if (lastBounds.current && !userInteracted.current) {
        try {
          // Not animated: Leaflet drops a new zoom while one is animating,
          // so the last of a burst of resizes would be lost.
          map.current.fitBounds(lastBounds.current, { padding: [44, 44], maxZoom: 15, animate: false });
        } catch { /* a box with no size yet */ }
      }
    };
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
        fleetLayer.current = null;
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
      lastBounds.current = null;
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
      bounds.push([alertPoint.lat, alertPoint.lng]);
    }

    if (bounds.length >= 2) {
      try {
        // Only auto-fit on the first load with real data, or when the route
        // changes to a completely different set of coordinates (e.g. hospital
        // divert). Once the user has zoomed/panned, leave their view alone.
        const shouldFit = !hasFit.current || !userInteracted.current;
        if (shouldFit) {
          lastBounds.current = L.latLngBounds(bounds);
          map.current.fitBounds(lastBounds.current, { padding: [44, 44], maxZoom: 15 });
          hasFit.current = true;
        }
      } catch {
        /* bounds can be degenerate while data is still arriving */
      }
    }
  }, [routeCoordinates, phase1Coordinates, origin, hospital, hub, closures, congestion, activePhase, proposals, alertPoint, corridor, focusKey]);

  // The rest of the fleet: small markers on their own layer, redrawn on each
  // fleet poll (not part of the fit, so they never re-zoom the map).
  const fleetLayer = useRef(null);
  useEffect(() => {
    if (!map.current) return;
    if (!fleetLayer.current) fleetLayer.current = L.layerGroup().addTo(map.current);
    fleetLayer.current.clearLayers();
    // Units parked at the same station would sit on one point: fan them out
    // a little around it so each one can be seen and clicked.
    const parked = {};
    for (const u of fleet) {
      const pos = u.position;
      if (!pos || !Number.isFinite(pos.lat) || !Number.isFinite(pos.lng)) continue;
      const proposed = u.unit_id === highlightUnit;
      const state = unitState(u);
      let at = [pos.lat, pos.lng];
      if (pos.source === 'station') {
        const key = `${pos.lat.toFixed(5)},${pos.lng.toFixed(5)}`;
        // Around the station's own marker, never on top of it.
        const n = (parked[key] = (parked[key] ?? -1) + 1);
        const angle = n * (Math.PI / 3) + Math.PI / 6;
        at = [pos.lat + 0.0014 * Math.cos(angle), pos.lng + 0.0014 * Math.sin(angle)];
      }
      const size = proposed ? 20 : 16;
      const fill = UNIT_STATE[state].color;
      const fresh = gpsFresh(pos);
      const ring = proposed ? `3px solid ${COLOR.proposal}`
        : fresh ? '2.5px solid #1d5fd1' : '1.5px solid #ffffff';
      const where = pos.source === 'gps'
        ? (fresh ? 'live GPS' : 'GPS, no recent fix')
        : pos.source === 'simulated' ? 'simulated' : 'at station';
      L.marker(at, {
        icon: L.divIcon({
          className: '',
          html: `<div class="rg-fleet rg-fleet-${state}${proposed ? ' rg-fleet-backup' : ''}" style="width:${size}px;height:${size}px;border-radius:50%;background:${fill};border:${ring};box-shadow:0 0 0 1px rgba(0,0,0,.35);color:#fff;font:700 9px/${size - 3}px system-ui;text-align:center;opacity:${state === 'off' ? 0.7 : 1}">${TYPE_LETTER[u.type] ?? ''}</div>`,
          iconSize: [size, size],
          iconAnchor: [size / 2, size / 2],
        }),
        keyboard: false,
        title: `${u.unit_id}, ${UNIT_STATE[state].label.toLowerCase()}`,
        zIndexOffset: state === 'busy' ? 500 : 0,
      })
        .bindPopup(popup([u.unit_id, 'b'], [`${u.type} · ${UNIT_STATE[state].label.toLowerCase()}`],
          [`${u.station ?? ''}`, 'i'], [`Position: ${where}`],
          ...(proposed ? [['Proposed backup', 'b']] : [])))
        .addTo(fleetLayer.current);
    }
  }, [fleet, highlightUnit]);

  // Hospitals and stations: their own layers, with switches and a legend
  // once the map shows the city (not part of the fit either).
  const hospitalLayer = useRef(null);
  const stationLayer = useRef(null);
  const cityControls = useRef(null);
  const callLayer = useRef(null);
  const osmNamedLayer = useRef(null);
  const osmOtherLayer = useRef(null);
  const showCity = hospitals.length > 0 || stations.length > 0 || osmHospitals.length > 0;
  useEffect(() => {
    if (!map.current || !showCity || cityControls.current) return;
    hospitalLayer.current = L.layerGroup().addTo(map.current);
    stationLayer.current = L.layerGroup().addTo(map.current);
    callLayer.current = L.layerGroup().addTo(map.current);
    // All Bangalore hospitals: the named areas shown by default, the rest off.
    osmNamedLayer.current = L.layerGroup().addTo(map.current);
    osmOtherLayer.current = L.layerGroup();
    if (!fleetLayer.current) fleetLayer.current = L.layerGroup().addTo(map.current);
    const switches = L.control.layers(null, {
      Hospitals: hospitalLayer.current,
      'Hospitals: Whitefield, Mathikere, venue': osmNamedLayer.current,
      'Other Bangalore hospitals (OSM)': osmOtherLayer.current,
      Stations: stationLayer.current,
      Vehicles: fleetLayer.current,
      Emergencies: callLayer.current,
    }, { position: 'topright', collapsed: false });
    const legend = legendControl();
    switches.addTo(map.current);
    legend.addTo(map.current);
    cityControls.current = [switches, legend];
  }, [showCity]);
  useEffect(() => () => {
    (cityControls.current ?? []).forEach((c) => c.remove());
  }, []);
  useEffect(() => {
    if (!hospitalLayer.current) return;
    hospitalLayer.current.clearLayers();
    for (const h of hospitals) {
      const at = h.location ?? h;
      if (!Number.isFinite(at?.lat) || !Number.isFinite(at?.lng)) continue;
      const strengths = Object.entries(h.capabilities ?? {})
        .filter(([, v]) => v >= 0.7).sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([k]) => k.replace(/_/g, ' '));
      L.marker([at.lat, at.lng], { icon: hospitalIcon(h.on_divert), keyboard: false, title: h.name })
        .bindPopup(popup([h.name, 'b'],
          [h.on_divert ? 'On divert: not taking new patients' : 'Taking patients', h.on_divert ? 'b' : undefined],
          ...(strengths.length ? [[`Strong in: ${strengths.join(', ')}`]] : []),
          ...(Number.isFinite(h.beds_total) ? [[`${h.beds_total} beds${Number.isFinite(h.beds_available) ? `, ${h.beds_available} free (reported)` : ''}`]] : [])))
        .addTo(hospitalLayer.current);
    }
  }, [hospitals, showCity]);
  useEffect(() => {
    if (!osmNamedLayer.current) return;
    osmNamedLayer.current.clearLayers();
    osmOtherLayer.current.clearLayers();
    for (const h of osmHospitals) {
      if (!Number.isFinite(h.lat) || !Number.isFinite(h.lng)) continue;
      const named = h.area !== 'bangalore';
      const area = named ? h.area.charAt(0).toUpperCase() + h.area.slice(1) : 'Bangalore';
      L.circleMarker([h.lat, h.lng], {
        radius: named ? 6 : 3.5,
        color: named ? COLOR.corridor : '#6b7280',
        weight: 2,
        fillOpacity: 0.85,
      })
        .bindPopup(popup([h.name, 'b'], [`${area} (OpenStreetMap, not a live feed)`]))
        .addTo(named ? osmNamedLayer.current : osmOtherLayer.current);
    }
  }, [osmHospitals, showCity]);
  useEffect(() => {
    if (!stationLayer.current) return;
    stationLayer.current.clearLayers();
    for (const st of stations) {
      if (!Number.isFinite(st.lat) || !Number.isFinite(st.lng)) continue;
      L.marker([st.lat, st.lng], { icon: stationIcon(st.type), keyboard: false, title: st.name })
        .bindPopup(popup([st.name, 'b'], [`${st.type} station`]))
        .addTo(stationLayer.current);
    }
  }, [stations, showCity]);
  useEffect(() => {
    if (!callLayer.current) return;
    callLayer.current.clearLayers();
    const label = { processing: 'Being assessed', awaiting_dispatcher_approval: 'Waiting for dispatch',
      dispatched: 'Unit on the way', patient_picked_up: 'Patient on board' };
    for (const c of calls) {
      const at = c.location;
      if (!Number.isFinite(at?.lat) || !Number.isFinite(at?.lng) || !label[c.status]) continue;
      const waiting = c.status === 'processing' || c.status === 'awaiting_dispatcher_approval';
      L.marker([at.lat, at.lng], {
        icon: L.divIcon({
          className: '',
          html: `<div class="rg-call${waiting ? ' rg-call-waiting' : ''}" style="width:22px;height:22px;border-radius:50%;background:${waiting ? '#c23a2b' : '#e8a020'};color:#fff;border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.4);font:800 13px/18px system-ui;text-align:center">!</div>`,
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
        keyboard: false,
        zIndexOffset: 800,
        title: `${c.emergency_type ?? 'Emergency'}: ${label[c.status]}`,
      })
        .bindPopup(popup([c.short_id ?? c.incident_id, 'b'], [`${c.emergency_type ?? 'Emergency'} · ${label[c.status]}`],
          ...(c.assigned_unit ? [[`Unit: ${c.assigned_unit}`]] : []),
          ...(c.background ? [['Simulated city call', 'i']] : [])))
        .addTo(callLayer.current);
    }
  }, [calls, showCity]);

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
      unitLayer.current.removeLayer(trailLine.current); // out of the group, not just off the map
      trailLine.current = null;
    }

    const pos = unit?.position;
    if (pos && Number.isFinite(pos.lat) && Number.isFinite(pos.lng)) {
      const source =
        pos.source === 'gps'
          ? (gpsFresh(pos)
            ? `Live GPS${Number.isFinite(pos.accuracy_m) ? ` ±${Math.round(pos.accuracy_m)} m` : ''}`
            : `GPS, no recent fix (${Math.round(Date.now() / 1000 - (pos.captured_at ?? pos.at ?? 0))} s old)`)
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
      unitLayer.current.removeLayer(unitMarker.current);
      unitMarker.current = null;
    }
  }, [unit, trail, simSpeed]);

  return (
    <div
      ref={holder}
      data-tour="map"
      style={{ height, width: '100%' }}
      className={`${theme === 'ink' ? 'map-ink' : 'map-tinted'} overflow-hidden rounded-md border border-rule ${className}`}
    />
  );
}
