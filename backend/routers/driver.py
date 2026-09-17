# Driver Router for live Unified React App
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from routers.incident import ACTIVE_INCIDENTS, save_db

router = APIRouter(prefix="/api/driver", tags=["Driver Queue"])

class ClaimPayload(BaseModel):
    incident_id: str
    driver_id: str = "drv-11"
    unit_type: str = "primary"

@router.get("/active")
async def get_active_dispatches():
    active_list = []
    for inc in ACTIVE_INCIDENTS:
        status = inc.get("status")
        if status in ["dispatched", "awaiting_dispatcher_approval", "processing"]:
            cit_view = inc.get("citizen_view") or {}
            act_plan = inc.get("action_plan") or {}
            rec_hosp = act_plan.get("recommended_hospital") or {}
            rec_route = act_plan.get("recommended_route") or {}

            route_coords = cit_view.get("route_coordinates") or []
            if not route_coords and hasattr(rec_route, "coordinates") and rec_route.coordinates:
                route_coords = [{"lat": c.lat, "lng": c.lng} for c in rec_route.coordinates]

            active_list.append({
                "incident_id": inc["incident_id"],
                "status": inc.get("status"),
                "emergency_type": inc.get("emergency_type") or cit_view.get("emergency_type") or "General Emergency",
                "severity": inc.get("severity") or cit_view.get("severity") or 0.7,
                "hospital_name": cit_view.get("hospital_name") or rec_hosp.get("name") or "Nearest Hospital",
                "hospital_location": cit_view.get("hospital_location") or {"lat": 12.9716, "lng": 77.5946},
                "origin": cit_view.get("origin") or {"lat": 12.9756, "lng": 77.6068},
                "eta_minutes": cit_view.get("eta_minutes") or 7,
                "distance_meters": cit_view.get("distance_meters") or 2400,
                "route_coordinates": route_coords,
                "hospital": rec_hosp,
                "citizen_text": inc.get("citizen_text") or inc.get("details") or "Emergency reported by citizen.",
                "details": inc.get("details") or inc.get("citizen_text") or "Emergency reported by citizen.",
                "vehicle_required": inc.get("vehicle_required") or "Ambulance",
                "include_ambulance_backup": inc.get("include_ambulance_backup") or cit_view.get("include_ambulance_backup") or False,
                "assigned_driver": inc.get("assigned_driver"),
                "driver_claimed": inc.get("driver_claimed", False),
                # Phone numbers needed by DriverDashboard LiveEmergencyChat contacts prop
                "citizen_phone": inc.get("citizen_phone"),
                "unit_phone": inc.get("unit_phone") or cit_view.get("unit_phone"),
                "hospital_phone": inc.get("hospital_phone") or cit_view.get("hospital_phone"),
                # Include the full citizen_view and action_plan so DriverDashboard
                # can access phase1_hub, route_coordinates, hospital_name, etc.
                "citizen_view": cit_view,
                "action_plan": inc.get("action_plan") or {},
            })
    active_list.reverse() # Newest incidents first
    return {"dispatches": active_list}

@router.post("/claim")
async def claim_dispatch(payload: ClaimPayload):
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == payload.incident_id:
            existing_driver = inc.get("assigned_driver")
            if existing_driver and existing_driver != payload.driver_id and inc.get("driver_claimed"):
                raise HTTPException(status_code=409, detail=f"Already claimed by {existing_driver}")
            
            inc["assigned_driver"] = payload.driver_id
            inc["driver_claimed"] = True
            inc["status"] = "dispatched"
            
            unit_statuses = inc.get("unit_statuses") or {"primary": "dispatched", "ambulance": "dispatched"}
            unit_statuses[payload.unit_type] = "dispatched"
            inc["unit_statuses"] = unit_statuses
            
            if "citizen_view" in inc:
                inc["citizen_view"]["status"] = "dispatched"
                inc["citizen_view"]["unit_statuses"] = unit_statuses
                
            save_db()
            return {"status": "claimed", "incident_id": payload.incident_id, "driver_id": payload.driver_id, "unit_statuses": unit_statuses}
    raise HTTPException(status_code=404, detail="Incident not found")

class HospitalChangePayload(BaseModel):
    incident_id: str
    hospital_name: str

BANGALORE_HOSPITAL_COORDS = {
    "Aster CMI Hospital (Hebbal)": {"lat": 13.0473, "lng": 77.5908},
    "MEDSTAR Speciality Hospital": {"lat": 13.0380, "lng": 77.5860},
    "Manipal Hospital (Hebbal)": {"lat": 13.0420, "lng": 77.5920},
    "Victoria Hospital": {"lat": 12.9634, "lng": 77.5746},
    "Fortis Hospital (Cunningham Rd)": {"lat": 12.9880, "lng": 77.5940},
    "Apollo Hospital (Seshadripuram)": {"lat": 12.9940, "lng": 77.5780}
}

@router.post("/arrived_pickup")
async def arrived_pickup(payload: ClaimPayload):
    from routers.incident import route_agent, Coordinate, RouteRequest
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == payload.incident_id:
            inc["driver_stage"] = "hospital"
            inc["status"] = "patient_picked_up"
            
            # Recompute live route from Patient -> Hospital
            cit_view = inc.get("citizen_view") or {}
            origin = cit_view.get("origin") or inc.get("location") or {"lat": 12.9756, "lng": 77.6068}
            hosp_loc = cit_view.get("hospital_location") or {"lat": 13.0473, "lng": 77.5908}

            # Guard against dicts that are missing lat/lng keys (e.g. loaded
            # from an older DB record written before these fields were added).
            origin_lat = origin.get("lat") if isinstance(origin, dict) else 12.9756
            origin_lng = origin.get("lng") if isinstance(origin, dict) else 77.6068
            hosp_lat   = hosp_loc.get("lat") if isinstance(hosp_loc, dict) else 13.0473
            hosp_lng   = hosp_loc.get("lng") if isinstance(hosp_loc, dict) else 77.5908
            if origin_lat is None: origin_lat = 12.9756
            if origin_lng is None: origin_lng = 77.6068
            if hosp_lat is None:   hosp_lat   = 13.0473
            if hosp_lng is None:   hosp_lng   = 77.5908

            try:
                p2_route = route_agent.compute_route(RouteRequest(
                    origin=Coordinate(lat=origin_lat, lng=origin_lng),
                    destination=Coordinate(lat=hosp_lat, lng=hosp_lng)
                ))
                new_coords = [{"lat": c.lat, "lng": c.lng} for c in p2_route.coordinates]
                eta_mins = max(1, int(p2_route.eta // 60))
                dist_meters = p2_route.distance
            except Exception as e:
                new_coords = [{"lat": origin_lat, "lng": origin_lng}, {"lat": hosp_lat, "lng": hosp_lng}]
                eta_mins = 6
                dist_meters = 2800

            cit_view["route_coordinates"] = new_coords
            cit_view["eta_minutes"] = eta_mins
            cit_view["distance_meters"] = dist_meters
            cit_view["message"] = f"Patient picked up. Transporting to {cit_view.get('hospital_name', 'Hospital ER')}."
            cit_view["status"] = "patient_picked_up"
            inc["citizen_view"] = cit_view
            save_db()
            return {"status": "patient_picked_up", "citizen_view": cit_view}
    raise HTTPException(status_code=404, detail="Incident not found")

@router.post("/change_hospital")
async def change_hospital(payload: HospitalChangePayload):
    from routers.incident import route_agent, Coordinate, RouteRequest
    for inc in ACTIVE_INCIDENTS:
        if inc["incident_id"] == payload.incident_id:
            h_coords = BANGALORE_HOSPITAL_COORDS.get(payload.hospital_name) or {"lat": 13.0473, "lng": 77.5908}
            
            cit_view = inc.get("citizen_view") or {}
            origin = cit_view.get("origin") or inc.get("location") or {"lat": 12.9756, "lng": 77.6068}

            # Guard against dicts missing lat/lng (stale DB records).
            origin_lat = origin.get("lat") if isinstance(origin, dict) else 12.9756
            origin_lng = origin.get("lng") if isinstance(origin, dict) else 77.6068
            h_lat      = h_coords.get("lat") if isinstance(h_coords, dict) else 13.0473
            h_lng      = h_coords.get("lng") if isinstance(h_coords, dict) else 77.5908
            if origin_lat is None: origin_lat = 12.9756
            if origin_lng is None: origin_lng = 77.6068
            if h_lat is None:      h_lat      = 13.0473
            if h_lng is None:      h_lng      = 77.5908
            
            # Recompute live Google route from Patient Location -> New Selected Hospital
            try:
                new_route = route_agent.compute_route(RouteRequest(
                    origin=Coordinate(lat=origin_lat, lng=origin_lng),
                    destination=Coordinate(lat=h_lat, lng=h_lng)
                ))
                new_coords = [{"lat": c.lat, "lng": c.lng} for c in new_route.coordinates]
                eta_mins = max(1, int(new_route.eta // 60))
                dist_meters = new_route.distance
            except Exception as e:
                new_coords = [{"lat": origin_lat, "lng": origin_lng}, {"lat": h_lat, "lng": h_lng}]
                eta_mins = 7
                dist_meters = 3200

            cit_view["hospital_name"] = payload.hospital_name
            cit_view["hospital_location"] = h_coords
            cit_view["route_coordinates"] = new_coords
            cit_view["eta_minutes"] = eta_mins
            cit_view["distance_meters"] = dist_meters
            cit_view["message"] = f"Rerouted by driver to {payload.hospital_name}."
            
            inc["hospital_name"] = payload.hospital_name
            inc["citizen_view"] = cit_view
            save_db()
            return {"status": "rerouted", "citizen_view": cit_view}
    raise HTTPException(status_code=404, detail="Incident not found")
