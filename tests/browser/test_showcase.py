"""Browser regressions for the actual built app and an isolated local API.

Build frontend/dist first. These tests never contact the deployed showcase or
use live provider credentials. Run with `pytest tests/browser -q`.
"""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ACCESS_TOKEN = "browser-test-only-operator-token-not-a-real-credential"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not (ROOT / "frontend/dist/index.html").exists():
        pytest.fail("Build the frontend before running browser tests: cd frontend && npm run build")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    directory = tmp_path_factory.mktemp("rapidgrid-browser")
    env = dict(os.environ, RAPIDGRID_MODE="operator", RAPIDGRID_OPERATOR_TOKEN=ACCESS_TOKEN,
               RAPIDGRID_DATA_DIR=str(directory), GOOGLE_ROUTES_API_KEY="", OPENWEATHER_API_KEY="")
    base = f"http://127.0.0.1:{port}"
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("server.py")), str(port)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    pytest.fail((directory / "server.log").read_text())
                try:
                    with urllib.request.urlopen(base + "/health", timeout=1):
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            else:
                pytest.fail("Local app did not become ready\n" + (directory / "server.log").read_text())
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        options = {"headless": True, "args": ["--no-sandbox"]}
        if executable := os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            options["executable_path"] = executable
        instance = playwright.chromium.launch(**options)
        yield instance
        instance.close()


@pytest.fixture
def context(browser, server, request):
    # Service workers own requests outside page.route interception. Disable them
    # in this suite so injected provider/API failures reach the actual UI.
    ctx = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000},
                              geolocation={"latitude": 13.035, "longitude": 77.597},
                              permissions=["geolocation"])
    yield ctx
    if getattr(request.node, "rep_call", None) and request.node.rep_call.failed:
        output = ROOT / "test-results"
        output.mkdir(exist_ok=True)
        for number, page in enumerate(ctx.pages):
            try:
                page.screenshot(path=str(output / f"{request.node.name}-{number}.png"), full_page=True)
            except Exception:
                pass
    ctx.close()


def unlock(context, server):
    response = context.request.post(server + "/api/session", data={"token": ACCESS_TOKEN})
    assert response.ok, response.text()


def test_development_proxy_preserves_same_origin_unlock(context, server, tmp_path):
    """Vite's proxy must not rewrite Host and make legitimate POSTs look foreign."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    with (tmp_path / "vite.log").open("w") as log:
        process = subprocess.Popen(
            ["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
             "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=ROOT / "frontend", env=dict(os.environ, RAPIDGRID_DEV_API_URL=server),
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(120):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "vite.log").read_text())
                try:
                    with urllib.request.urlopen(origin + "/api/session", timeout=1):
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            else:
                pytest.fail("Development proxy did not start")
            response = context.request.post(origin + "/api/session", data={"token": ACCESS_TOKEN},
                                            headers={"Origin": origin})
            assert response.status == 200, response.text()
            assert context.request.get(origin + "/api/incidents/").status == 200
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def poll_incident(context, server, incident_id, status, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = context.request.get(server + "/api/incidents/" + incident_id)
        assert response.ok, response.text()
        incident = response.json()
        if incident["status"] == status:
            return incident
        assert incident["status"] != "failed", incident.get("error")
        time.sleep(0.2)
    pytest.fail(f"{incident_id} did not reach {status}: {incident['status']}")


def test_operator_unlock_denies_wrong_token_and_keeps_secret_out_of_js(context, server):
    page = context.new_page()
    page.goto(server + "/dispatcher")
    expect(page.get_by_role("heading", name="RapidGrid showcase access")).to_be_visible()
    page.get_by_label("Operator access token").fill("wrong-test-token")
    page.get_by_role("button", name="Unlock showcase", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Invalid")
    assert context.request.get(server + "/api/incidents/").status == 401
    page.get_by_label("Operator access token").fill(ACCESS_TOKEN)
    page.get_by_role("button", name="Unlock showcase", exact=True).click()
    expect(page.get_by_role("heading", name="RapidGrid showcase access")).not_to_be_visible()
    expect(page.get_by_text("Call queue", exact=False).first).to_be_visible()
    assert "rapidgrid_operator" not in page.evaluate("document.cookie")
    cookie = next(cookie for cookie in context.cookies() if cookie["name"] == "rapidgrid_operator")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"


def test_report_refresh_dispatch_chat_pickup_refresh_and_handover(context, server):
    unlock(context, server)
    context.add_init_script("""if (location.protocol === 'http:') {
      sessionStorage.setItem('geoagentic_citizen', JSON.stringify({
        name: 'Browser regression citizen', phone: 'TEST-BROWSER-ONLY'
      }));
    }""")
    citizen = context.new_page()
    citizen.goto(server + "/citizen")
    citizen.get_by_role("button", name="SOS").click()
    citizen.locator("textarea").fill("Test scenario: chest pain and sweating near Hebbal.")
    with citizen.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/incidents")) as created:
        citizen.get_by_role("button", name="Send emergency report", exact=True).click()
    assert created.value.status == 202
    incident_id = created.value.json()["incident_id"]
    poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")
    citizen.reload()
    expect(citizen.get_by_text(incident_id, exact=False).first).to_be_visible(timeout=15000)
    assert not any(item["incident_id"] == incident_id for item in
                   context.request.get(server + "/api/driver/active").json()["dispatches"])

    dispatcher = context.new_page()
    dispatcher.goto(server + "/dispatcher")
    dispatcher.get_by_role("button").filter(has_text=incident_id).first.click()
    dispatcher.get_by_role("button", name="Approve and dispatch", exact=True).click()
    poll_incident(context, server, incident_id, "dispatched")

    driver = context.new_page()
    driver.goto(server + "/driver")
    driver.get_by_role("button").filter(has_text=incident_id).first.click()
    message = "Regression test: please confirm this dispatch."
    dispatcher.get_by_role("textbox", name="Message to incident channel").fill(message)
    # A real pointer click catches the overlapping-panel regression; Enter or
    # force=True would let that broken layout pass.
    dispatcher.get_by_role("button", name="Send message", exact=True).click(timeout=5000)
    expect(driver.get_by_text(message, exact=True)).to_be_visible(timeout=10000)

    hospital = context.new_page()
    hospital.goto(server + "/hospital")
    expect(hospital.get_by_text(incident_id, exact=True)).to_be_visible()
    driver.get_by_role("button", name="I have reached the patient", exact=True).click()
    poll_incident(context, server, incident_id, "patient_picked_up")
    driver.reload()
    # The selected assignment and stage must both survive reload.
    expect(driver.get_by_role("button", name="Patient handed over at ER", exact=True)).to_be_visible(timeout=10000)
    expect(driver.get_by_role("button", name="I have reached the patient", exact=True)).not_to_be_visible()
    driver.get_by_role("button", name="Patient handed over at ER", exact=True).click()
    poll_incident(context, server, incident_id, "completed")
    assert not any(item["incident_id"] == incident_id for item in
                   context.request.get(server + "/api/driver/active").json()["dispatches"])


def test_fire_scene_arrival_keeps_backup_ambulance_workflow_active(context, server):
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "Test scenario: warehouse fire with a person suffering burns.",
        "citizen_name": "Multi-unit browser test", "citizen_phone": "TEST-MULTI-ONLY",
        "vehicle_required": "Fire Engine", "include_ambulance_backup": True,
        "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202
    incident_id = created.json()["incident_id"]
    incident = poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")
    plan = incident["action_plan"]
    approved = context.request.post(server + f"/api/incidents/{incident_id}/approve", data={
        "dispatcher_id": "browser-test", "approved_hospital": plan["recommended_hospital"]["hospital_id"],
        "approved_route": plan["recommended_route"]["route_id"],
    })
    assert approved.ok, approved.text()

    def unit_page(vehicle, unit_id):
        page = context.new_page()
        profile = json.dumps({"unitId": unit_id, "vehicleType": vehicle, "driverName": "Test crew"})
        page.add_init_script("if (location.protocol === 'http:') sessionStorage.setItem('geoagentic_driver', "
                             + json.dumps(profile) + ");")
        page.goto(server + "/driver")
        page.get_by_role("button").filter(has_text=incident_id).first.click()
        return page

    fire = unit_page("Fire Engine", "FIRE-TEST")
    fire.get_by_role("button", name="Unit arrived at incident", exact=True).click()
    incident = context.request.get(server + f"/api/incidents/{incident_id}").json()
    assert incident["status"] == "dispatched"
    assert incident["unit_statuses"] == {"primary": "arrived", "ambulance": "dispatched"}

    ambulance = unit_page("Ambulance", "AMB-TEST")
    ambulance.get_by_role("button", name="I have reached the patient", exact=True).click()
    poll_incident(context, server, incident_id, "patient_picked_up")
    ambulance.reload()
    ambulance.get_by_role("button", name="Patient handed over at ER", exact=True).click()
    incident = poll_incident(context, server, incident_id, "completed")
    assert incident["unit_statuses"] == {"primary": "arrived", "ambulance": "arrived"}


def test_voice_send_waits_for_late_final_transcript(context, server):
    unlock(context, server)
    page = context.new_page()
    page.add_init_script("""window.SpeechRecognition = class {
      start() { setTimeout(() => this.onstart?.(), 0); }
      stop() {
        if (this.stopped) return;
        this.stopped = true;
        setTimeout(() => {
          const result = [{transcript: 'late final words'}];
          result.isFinal = true;
          this.onresult?.({resultIndex: 0, results: [result]});
          this.onend?.();
        }, 50);
      }
    };""")
    page.goto(server + "/citizen")
    page.evaluate("sessionStorage.setItem('geoagentic_citizen', JSON.stringify({name:'Voice test',phone:'TEST-VOICE-ONLY'}))")
    page.reload()
    page.get_by_role("button", name="SOS").click()
    page.locator("textarea").fill("Typed report")
    page.get_by_role("button", name="Start voice input", exact=True).click()
    expect(page.get_by_role("button", name="Stop voice input", exact=True)).to_be_visible()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/incidents")) as created:
        page.get_by_role("button", name="Send emergency report", exact=True).click()
    payload = created.value.request.post_data_json
    assert payload["citizen_text"] == "Typed report late final words"
    assert payload["input_modality"] == "voice"
    incident_id = created.value.json()["incident_id"]
    poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")
    message = page.get_by_role("textbox", name="Message to incident channel")
    message.fill("Typed message")
    page.get_by_role("button", name="Start voice input", exact=True).click()
    expect(page.get_by_role("button", name="Stop voice input", exact=True)).to_be_visible()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/api/chat/{incident_id}/send")) as sent:
        page.get_by_role("button", name="Send message", exact=True).click()
    assert sent.value.request.post_data_json["message"] == "Typed message late final words"


def test_hospital_capacity_rejects_failure_and_restores_state(context, server):
    unlock(context, server)
    page = context.new_page()
    page.goto(server + "/hospital")
    page.evaluate("sessionStorage.setItem('geoagentic_hosp', JSON.stringify({id:'blr-007',name:'Aster CMI Hospital (Hebbal)'}))")
    page.reload()
    accepting = page.get_by_role("button", name="Accepting", exact=True)
    divert = page.get_by_role("button", name="On divert", exact=True)
    expect(accepting).to_have_attribute("aria-pressed", "true")
    page.route("**/api/hospital/blr-007/resources", lambda route: route.fulfill(
        status=503, content_type="application/json", body=json.dumps({"detail": "Capacity service unavailable"})))
    divert.click()
    expect(page.get_by_role("alert")).to_contain_text("Capacity service unavailable")
    expect(accepting).to_have_attribute("aria-pressed", "true")
    page.unroute("**/api/hospital/blr-007/resources")
    divert.click()
    expect(divert).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(divert).to_have_attribute("aria-pressed", "true")
    accepting.click()
    expect(accepting).to_have_attribute("aria-pressed", "true")


@pytest.mark.parametrize("route", ["/", "/citizen", "/dispatcher", "/driver", "/hospital"])
def test_mobile_portals_fit_viewport(context, server, route):
    unlock(context, server)
    page = context.new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server + route)
    expect(page.get_by_role("heading", name="RapidGrid showcase access")).not_to_be_visible()
    page.wait_for_timeout(500)
    # Sample the full SOS pulse cycle: its transformed decoration previously
    # overflowed only during part of the animation, making a single check flaky.
    for _ in range(9 if route == "/citizen" else 1):
        dimensions = page.evaluate("({width:innerWidth, document:document.documentElement.scrollWidth})")
        assert dimensions["document"] <= dimensions["width"], dimensions
        page.wait_for_timeout(250)
