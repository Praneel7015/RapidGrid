"""Browser regressions for the actual built app and an isolated local API.

Build frontend/dist first. These tests never contact the deployed showcase or
use live provider credentials. Run with `pytest tests/browser -q`.
"""
from pathlib import Path
import json
import re
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
    # The rationale panel's Traffic section renders only if "TRAFFIC:" parses.
    traffic = dispatcher.locator("div").filter(has=dispatcher.locator(".eyebrow", has_text=re.compile(r"^Traffic$"))).last
    expect(traffic).to_contain_text("Modelled, no live feed")
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


def test_hospital_reported_beds_reach_the_ranking(context, server):
    unlock(context, server)
    page = context.new_page()
    page.goto(server + "/hospital")
    page.evaluate("sessionStorage.setItem('geoagentic_hosp', JSON.stringify({id:'blr-007',name:'Aster CMI Hospital (Hebbal)'}))")
    page.reload()
    beds = page.get_by_label("Available beds")
    expect(beds).to_be_enabled()
    expect(page.get_by_text("No bed figure reported", exact=False)).to_be_visible()
    beds.fill("0")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/hospital/blr-007/resources")) as saved:
        page.get_by_role("button", name="Save", exact=True).click()
    assert saved.value.status == 200 and saved.value.json()["beds_available"] == 0
    expect(page.get_by_text("Ranking uses your reported 0 available beds.")).to_be_visible()
    page.reload()
    expect(beds).to_have_value("0")  # stored on the server, not just in the page

    def bed_score(name):
        ranked = context.request.post(server + "/api/hospital/rank", data={
            "incident_location": {"lat": 13.035, "lng": 77.597}, "emergency_type": "cardiac",
        }).json()["ranked_hospitals"]
        entry = next(h for h in ranked if h["name"] == name)
        return next(b["raw_score"] for b in entry["score_breakdown"] if b["factor"] == "Bed availability")

    assert bed_score("Aster CMI Hospital (Hebbal)") == 0.0
    # An accepting/divert change keeps an unsaved bed figure in the field.
    beds.fill("7")
    page.get_by_role("button", name="On divert", exact=True).click()
    expect(page.get_by_role("button", name="On divert", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(beds).to_have_value("7")
    page.get_by_role("button", name="Accepting", exact=True).click()
    expect(page.get_by_role("button", name="Accepting", exact=True)).to_have_attribute("aria-pressed", "true")
    beds.fill("")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("No bed figure reported", exact=False)).to_be_visible()
    assert bed_score("Aster CMI Hospital (Hebbal)") == 0.5


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


def test_ranked_destination_click_needs_explicit_confirmation(context, server):
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "Test scenario: chest pain near Hebbal.", "citizen_name": "Browser confirm test",
        "citizen_phone": "TEST-CONFIRM-ONLY", "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202, created.text()
    incident_id = created.json()["incident_id"]
    plan = poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")["action_plan"]
    target = plan["all_hospitals"][-1]["name"]

    page = context.new_page()
    approvals = []
    page.on("request", lambda r: approvals.append(r.url) if r.url.endswith("/approve") else None)
    page.goto(server + "/dispatcher")
    page.get_by_role("button").filter(has_text=incident_id).first.click()
    page.get_by_role("button").filter(has_text=target).last.click()
    expect(page.get_by_role("button", name="Confirm dispatch", exact=True)).to_be_visible()
    page.wait_for_timeout(600)
    assert approvals == []  # selecting a row must not dispatch
    # The one-click approve would send to the recommended hospital, not this one.
    expect(page.get_by_role("button", name="Approve and dispatch", exact=True)).to_be_disabled()
    assert context.request.get(server + f"/api/incidents/{incident_id}").json()["status"] == "awaiting_dispatcher_approval"

    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Confirm dispatch", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Approve and dispatch", exact=True)).to_be_enabled()
    page.get_by_role("button").filter(has_text=target).last.click()
    page.get_by_role("button", name="Confirm dispatch", exact=True).click()
    dispatched = poll_incident(context, server, incident_id, "dispatched")
    assert dispatched["citizen_view"]["hospital_name"] == target
    assert len(approvals) == 1


def test_override_list_also_needs_confirmation(context, server):
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "Test scenario: chest pain near Hebbal.", "citizen_name": "Browser override test",
        "citizen_phone": "TEST-OVERRIDE-ONLY", "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202, created.text()
    incident_id = created.json()["incident_id"]
    plan = poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")["action_plan"]
    target = plan["all_hospitals"][-1]["name"]

    page = context.new_page()
    approvals = []
    page.on("request", lambda r: approvals.append(r.url) if r.url.endswith("/approve") else None)
    page.goto(server + "/dispatcher")
    page.get_by_role("button").filter(has_text=incident_id).first.click()
    page.get_by_role("button", name="Send to a different hospital", exact=True).click()
    override = page.locator("div.rise").filter(has_text="Override destination")
    override.get_by_role("button").filter(has_text=target).first.click()
    expect(page.get_by_role("button", name="Confirm dispatch", exact=True)).to_be_visible()
    page.wait_for_timeout(600)
    assert approvals == []
    # Closing the list drops the hidden choice, so Approve is not left locked.
    page.get_by_role("button", name="Close", exact=True).click()
    expect(page.get_by_role("button", name="Approve and dispatch", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Send to a different hospital", exact=True).click()
    override.get_by_role("button").filter(has_text=target).first.click()
    page.get_by_role("button", name="Confirm dispatch", exact=True).click()
    dispatched = poll_incident(context, server, incident_id, "dispatched")
    assert dispatched["citizen_view"]["hospital_name"] == target
    assert len(approvals) == 1


def test_report_without_a_location_fix_needs_the_caller_to_confirm_the_fallback(browser, server):
    # No geolocation permission: the browser denies the request.
    ctx = browser.new_context(service_workers="block", viewport={"width": 390, "height": 844})
    try:
        unlock(ctx, server)
        page = ctx.new_page()
        posted = []
        page.on("request", lambda r: posted.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/api/incidents") else None)
        page.goto(server + "/citizen")
        page.get_by_role("button", name="SOS").click()
        page.locator("textarea").fill("Test scenario: fall at home.")
        send = page.get_by_role("button", name="Send emergency report", exact=True)
        expect(page.get_by_text("We could not get your location", exact=False)).to_be_visible(timeout=15000)
        expect(send).to_be_disabled()
        assert posted == []
        page.get_by_role("checkbox").check()
        expect(send).to_be_enabled()
    finally:
        ctx.close()


def test_zoom_button_click_survives_dispatcher_refresh(context, server):
    # Leaflet stops propagation on the +/- anchors. If the map misses that
    # click, the next 2 s incident refresh refits and undoes the user's zoom.
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "Test scenario: chest pain near Hebbal.",
        "citizen_name": "Zoom browser test", "citizen_phone": "TEST-ZOOM-ONLY",
        "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202
    incident_id = created.json()["incident_id"]
    poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")

    page = context.new_page()
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
    page.goto(server + "/dispatcher")
    page.get_by_role("button").filter(has_text=incident_id).first.click()

    def zoom():
        return page.evaluate("""() => {
          const levels = [...document.querySelectorAll('img.leaflet-tile')]
            .map((img) => Number((img.getAttribute('src') || '').split('/').at(-3)))
            .filter(Number.isFinite);
          return levels.length ? Math.max(...levels) : null;
        }""")

    page.wait_for_function("document.querySelectorAll('img.leaflet-tile').length > 0")
    page.wait_for_timeout(2500)  # first fit and its tiles settle
    fitted = zoom()
    assert fitted is not None
    page.locator(".leaflet-control-zoom-in").click()
    page.wait_for_timeout(1000)
    assert zoom() == fitted + 1
    # Two refresh cycles of the incident list.
    page.wait_for_timeout(5000)
    assert zoom() == fitted + 1



def test_map_popups_show_names_as_text_not_markup(context, server):
    # Hospital and road names come from OpenStreetMap and the API. A name that
    # looks like HTML must be shown literally, never parsed.
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "Test scenario: chest pain near Hebbal.",
        "citizen_name": "Popup browser test", "citizen_phone": "TEST-POPUP-ONLY",
        "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202
    incident_id = created.json()["incident_id"]
    poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")

    hostile = '<img src=x onerror="window.__popupInjected=1">Hostile ER'

    def rename(route):
        response = route.fetch()
        body = response.json()
        for incident in body if isinstance(body, list) else body.get("incidents", []):
            if isinstance(incident.get("citizen_view"), dict):
                incident["citizen_view"]["hospital_name"] = hostile
        route.fulfill(response=response, json=body)

    page = context.new_page()
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
    page.route(re.compile(r".*/api/incidents/$"), rename)
    page.goto(server + "/dispatcher")
    page.get_by_role("button").filter(has_text=incident_id).first.click()
    page.locator(".rg-marker", has_text="ER").click()
    content = page.locator(".leaflet-popup-content")
    content.wait_for()
    page.wait_for_timeout(500)
    assert page.evaluate("window.__popupInjected") is None, "the name's markup ran"
    assert content.locator("img").count() == 0
    expect(content).to_contain_text(hostile)


def test_driver_outage_is_not_shown_as_no_calls(context, server):
    unlock(context, server)
    page = context.new_page()
    page.route(re.compile(r".*/api/driver/active$"), lambda route: route.fulfill(status=503, body="down"))
    page.goto(server + "/driver")
    expect(page.get_by_text("Cannot reach dispatch")).to_be_visible(timeout=10000)
    expect(page.get_by_text("No calls assigned")).to_have_count(0)


def test_dispatcher_sees_when_triage_cannot_read_a_report(context, server):
    unlock(context, server)
    created = context.request.post(server + "/api/incidents", data={
        "citizen_text": "ನನ್ನ ತಂದೆಗೆ ಎದೆ ನೋವು",
        "citizen_name": "Script browser test", "citizen_phone": "TEST-SCRIPT-ONLY",
        "location": {"lat": 13.035, "lng": 77.597},
    })
    assert created.status == 202
    incident_id = created.json()["incident_id"]
    poll_incident(context, server, incident_id, "awaiting_dispatcher_approval")
    page = context.new_page()
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
    page.goto(server + "/dispatcher")
    card = page.get_by_role("button").filter(has_text=incident_id).first
    expect(card).to_contain_text("Kannada", timeout=10000)

# --- Progressive web app ---------------------------------------------------------

@pytest.fixture
def pwa_context(browser, server):
    # The main suite blocks service workers so request interception reaches the
    # page; these tests need them.
    ctx = browser.new_context(service_workers="allow", viewport={"width": 390, "height": 844})
    unlock(ctx, server)
    yield ctx
    ctx.close()


def test_manifest_and_icons_make_the_app_installable(pwa_context, server):
    response = pwa_context.request.get(server + "/manifest.webmanifest")
    assert response.ok
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == "RapidGrid" and manifest["display"] == "standalone"
    assert manifest["start_url"] == "/" and manifest["theme_color"]
    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"]:
        image = pwa_context.request.get(server + icon["src"])
        assert image.ok and image.headers["content-type"] == "image/png", icon["src"]
    assert {s["url"] for s in manifest["shortcuts"]} == {"/citizen", "/dispatcher", "/driver", "/hospital"}


def test_app_shell_opens_offline_but_live_data_is_never_cached(pwa_context, server):
    page = pwa_context.new_page()
    page.goto(server + "/citizen")
    page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.ready.then(() => true)")
    page.reload()  # now controlled by the service worker
    page.wait_for_function("navigator.serviceWorker.controller !== null")

    pwa_context.set_offline(True)
    try:
        page.goto(server + "/dispatcher")
        # The shell renders from the cache, and the page says it is offline.
        expect(page.get_by_role("status").filter(has_text="You are offline")).to_be_visible(timeout=10000)
        assert page.locator("#root *").count() > 0
        # Live data is network-only: an API call fails rather than returning a cached copy.
        failed = page.evaluate("""() => fetch('/api/incidents/').then(() => false, () => true)""")
        assert failed
    finally:
        pwa_context.set_offline(False)
    # Back online, the app recovers without a manual reload.
    expect(page.get_by_role("status").filter(has_text="You are offline")).to_have_count(0, timeout=10000)

    cached = page.evaluate("""async () => {
        const urls = [];
        for (const name of await caches.keys()) {
            for (const request of await (await caches.open(name)).keys()) urls.push(new URL(request.url).pathname);
        }
        return urls;
    }""")
    assert cached, "the app shell should be precached"
    assert not [u for u in cached if u.startswith(("/api", "/ws", "/health"))]
    assert not [u for u in cached if u.endswith(".wasm") or "transformers" in u]


def controlled_page(context, server, path="/citizen"):
    page = context.new_page()
    page.goto(server + path)
    page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.ready.then(() => true)")
    page.reload()
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    return page


def test_server_routes_with_a_query_string_bypass_the_app_shell(pwa_context, server):
    # Workbox matches navigations on pathname plus query, so /api?x must not
    # fall through to the cached index.html.
    page = controlled_page(pwa_context, server)
    for path in ("/api?probe=1", "/health?probe=1"):
        response = page.goto(server + path)
        assert "application/json" in response.headers.get("content-type", ""), path
        assert 'id="root"' not in page.content(), path


def test_update_reloads_only_the_tab_that_chose_reload(pwa_context, server):
    later = controlled_page(pwa_context, server)
    chooser = controlled_page(pwa_context, server)
    later.evaluate("window.__stillHere = true")

    sw = ROOT / "frontend/dist/sw.js"
    original = sw.read_bytes()
    try:
        # Any byte change to sw.js is a new version for the browser.
        sw.write_bytes(original + b"\n// browser-test update\n")
        for page in (later, chooser):
            page.evaluate("navigator.serviceWorker.getRegistration().then((r) => r.update())")
        for page in (later, chooser):
            expect(page.get_by_text("A new version of RapidGrid is ready.")).to_be_visible(timeout=15000)

        later.get_by_role("button", name="Later", exact=True).click()
        with chooser.expect_navigation(timeout=15000):
            chooser.get_by_role("button", name="Reload", exact=True).click()

        # The other tab is told, and keeps its page (and any open call).
        expect(later.get_by_text("RapidGrid was updated in another tab.", exact=False)).to_be_visible(timeout=15000)
        assert later.evaluate("window.__stillHere === true")
    finally:
        sw.write_bytes(original)
