"""
End-to-End Simulation Script for GeoAgentic.

Runs the full 10-step workflow defined in the architecture, start to finish,
on one incident with no manual intervention between steps.
"""

import sys
import time

import requests

API_URL = "http://localhost:8000/api"


def print_step(step_num: int, title: str, details: str = ""):
    print(f"\n[{step_num}/10] === {title} ===")
    if details:
        print(f"  > {details}")
    time.sleep(1)  # small pause for readability


def run_simulation():
    print("Starting GeoAgentic End-to-End Simulation...\n")

    # Step 1: Citizen Report (Accessibility Agent)
    print_step(1, "Citizen Report", "Citizen uses sign-language video to report a trauma incident.")
    accessibility_payload = {
        "raw_input": "[MOCK VIDEO INTERPRETER] Subject signing about a bad collision, looks injured",
        "modality": "sign_language",
        "location": {"lat": 12.9716, "lng": 77.5946},
    }
    r1 = requests.post(f"{API_URL}/accessibility/report", json=accessibility_payload)
    if r1.status_code != 200:
        print(f"Failed at step 1: {r1.text}")
        sys.exit(1)

    access_data = r1.json()
    incident_report = access_data["incident_report"]
    print(f"  * Extracted type: {incident_report['incident_type']}")
    print(f"  * Extracted severity: {incident_report['severity_estimate']}")

    # Step 2: Emergency Coordinator
    print_step(
        2,
        "Emergency Coordinator",
        "Coordinator receives the normalised report, evaluates severity, and spins up agents.",
    )
    coord_payload = {"incident_report": incident_report}
    r2 = requests.post(f"{API_URL}/coordinator/coordinate", json=coord_payload)
    if r2.status_code != 200:
        print(f"Failed at step 2: {r2.text}")
        sys.exit(1)

    coord_data = r2.json()
    incident_id = coord_data["incident_id"]
    activated = coord_data["activated_agents"]
    print(f"  * Incident ID assigned: {incident_id}")
    print(f"  * Activated {len(activated)} agents, including: {', '.join(activated)}")

    # Step 3: Traffic Intelligence
    print_step(
        3,
        "Traffic Intelligence",
        "Analysing current road network and broadcasting TrafficSnapshot.",
    )
    r3 = requests.post(f"{API_URL}/traffic/analyse", json={})
    if r3.status_code != 200:
        print(f"Failed at step 3: {r3.text}")
        sys.exit(1)
    print("  * TrafficSnapshot published to event bus.")

    # Step 4: Prediction Agent (Listen & Predict)
    print_step(
        4,
        "Prediction Agent",
        "Listening to traffic updates to calculate live ETAs (handled async).",
    )
    print("  * Prediction Agent is standing by.")

    # Step 5 & 6 & 7: Hospital, Route, and Decision Fusion
    print_step(
        5,
        "Decision Fusion Engine",
        "Triggering pipeline: Hospital Intelligence -> Route Optimization -> Fusion",
    )
    fusion_payload = {
        "incident_location": incident_report["location"],
        "emergency_type": incident_report["incident_type"],
        "severity": incident_report["severity_estimate"],
        "vehicle_location": "N1",
        "vehicle_type": "ambulance",
    }
    r7 = requests.post(f"{API_URL}/fusion/combine", json=fusion_payload)
    if r7.status_code != 200:
        print(f"Failed at step 5/6/7: {r7.text}")
        sys.exit(1)

    fusion_data = r7.json()
    print(
        f"  * (Step 5) Hospital Intelligence ranked {len(fusion_data['all_hospitals'])} candidates."
    )
    print(
        f"  * (Step 6) Route Optimization computed route (ETA: {fusion_data['recommended_route']['eta']}s)."
    )
    print("  * (Step 7) Decision Fusion created action plan.")
    rationale = fusion_data["explanation"].replace("\u2192", "->")
    print(f"  * Rationale: {rationale}")

    # Step 8: Communication Agent
    print_step(
        8, "Communication Agent", "FUSION_COMPLETE event caught on bus. Dispatching notifications."
    )
    print(
        "  * (Observe server logs for concurrent dispatch to Hospital, Police, Driver, and Family)"
    )
    time.sleep(1)  # wait for async logs to settle

    # Step 9: Dispatcher & Driver
    print_step(9, "Ambulance Departure", "Dispatcher reviewed the plan and ambulance is en route.")
    print("  * Ambulance departed from N1.")

    # Step 10: Learning Agent
    print_step(
        10, "Learning Agent", "Incident resolved. Sending CASE_CLOSED event for offline learning."
    )
    learning_payload = {
        "incident_id": incident_id,
        "predicted_eta": fusion_data["recommended_route"]["eta"],
        "actual_eta": fusion_data["recommended_route"]["eta"]
        + 45,  # Simulated actual time was slightly longer
        "dispatcher_override": "None",
    }
    r10 = requests.post(f"{API_URL}/learning/close_case", json=learning_payload)
    if r10.status_code != 200:
        print(f"Failed at step 10: {r10.text}")
        sys.exit(1)

    print("  * Case closed. (Observe server logs for Learning Agent STUB output).")

    print("\n=== End-to-End Simulation Complete! ===")


if __name__ == "__main__":
    run_simulation()
