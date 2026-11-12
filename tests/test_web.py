import json
import re
import struct

import pytest
from conftest import FakeSource, ScriptedDetector, wait_for
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from acacia.pipeline import Pipeline
from acacia.web.app import create_app

SEAT = [[0.2, 0.4], [0.9, 0.4], [0.9, 0.62], [0.2, 0.62]]
GOOD_HOST = {"host": "127.0.0.1:18765"}


@pytest.fixture
def pipeline(fast_config, paths):
    p = Pipeline(fast_config, paths, lambda c: ScriptedDetector(), source_factory=lambda u, c: FakeSource(fps=60))
    p._url = lambda: None
    p.start()
    yield p
    p.stop()


@pytest.fixture
def client(pipeline, fast_config):
    # TestClient addresses WebSockets to "testserver", so that host is allowed here.
    app = create_app(pipeline, fast_config, extra_hosts=("testserver",), manage_pipeline=False)
    with TestClient(app, base_url="http://127.0.0.1:18765") as test_client:
        yield test_client


def test_page_is_served_with_a_strict_policy(client):
    response = client.get("/")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe" not in policy


def test_page_loads_nothing_from_outside(client):
    for path in ("/", "/static/app.js", "/static/style.css"):
        text = client.get(path).text
        assert not re.search(r"https?://(?!\$\{)", text), path


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example:18765", "10.0.0.146:18765", ""])
def test_dns_rebinding_hosts_are_refused(client, host):
    assert client.get("/api/health", headers={"host": host}).status_code == 403


def test_cross_site_requests_are_refused(client):
    headers = {"origin": "https://evil.example"}
    assert client.get("/api/state", headers=headers).status_code == 403
    assert client.post("/api/retry", headers=headers).status_code == 403
    assert client.put("/api/calibration", headers=headers, json={"polygons": []}).status_code == 403


def test_same_origin_requests_are_allowed(client):
    assert client.get("/api/state", headers={"origin": "http://127.0.0.1:18765"}).status_code == 200


def test_a_cross_site_page_cannot_open_the_video_socket(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass


def test_socket_delivers_header_and_jpeg(client):
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:18765"}) as socket:
        for _ in range(200):
            message = socket.receive_bytes()
            (length,) = struct.unpack(">I", message[:4])
            header = json.loads(message[4:4 + length])
            jpeg = message[4 + length:]
            if jpeg:
                break
        assert jpeg.startswith(b"\xff\xd8")
        assert header["state"]["state"] in ("UNKNOWN", "OFF")
        assert "wall" in header
        socket.send_text(json.dumps({"frame": header["frame"]["seq"], "evidence": None}))


def test_calibration_rejects_bad_geometry_and_explains(client, pipeline):
    wait_for(lambda: pipeline._live_view is not None, message="live view")
    response = client.put("/api/calibration", json={"polygons": [{"points": [[0, 0], [2, 2], [0, 1]]}]})
    assert response.status_code == 422
    assert "inside the frame" in response.json()["error"]


def test_calibration_save_and_invalidate(client, pipeline):
    wait_for(lambda: pipeline._live_view is not None, message="live view")
    saved = client.put("/api/calibration", json={"polygons": [{"name": "seat", "points": SEAT}]}).json()
    assert saved["status"] == "valid"
    assert saved["view"]["source_width"] == 640
    invalid = client.post("/api/calibration/invalidate", json={"reason": "camera moved"}).json()
    assert invalid["status"] == "invalid"


def test_settings_validation(client):
    assert client.patch("/api/settings", json={"state.on_confirm_s": 4}).json()["state.on_confirm_s"] == 4.0
    assert client.patch("/api/settings", json={"state.on_confirm_s": 9999}).status_code == 422
    assert client.patch("/api/settings", json={"camera.host": 1}).status_code == 422


def test_truth_marks(client):
    assert client.post("/api/truth", json={"kind": "on_start", "note": "lying on left seat"}).status_code == 200
    assert client.post("/api/truth", json={"kind": "bogus"}).status_code == 422
    assert client.get("/api/truth").json()[0]["note"] == "lying on left seat"


def test_events_endpoint(client):
    assert client.get("/api/events?limit=abc").status_code == 200
    assert client.get("/api/events?tier=confirmed").json() == []


def test_oversized_and_malformed_bodies(client):
    assert client.put("/api/calibration", content=b"x" * 300_000).status_code == 422
    assert client.put("/api/calibration", content=b"not json").status_code == 422
    assert client.post("/api/truth", content=b"[1, 2]").status_code == 422


def test_health_exposes_no_credentials(client):
    assert "@" not in client.get("/api/health").text


def test_the_only_way_to_act_on_an_animal_is_the_cue_and_it_starts_disarmed(client):
    """This pins the route list, so any new surface has to be deliberate."""
    routes = {r.path for r in client.app.routes}
    assert routes == {
        "/", "/api/state", "/api/health", "/api/events", "/api/calibration",
        "/api/calibration/invalidate", "/api/settings", "/api/truth", "/api/retry", "/api/cue", "/ws", "/static",
    }
    assert client.get("/api/cue").json()["armed"] is False


def test_the_cue_cannot_be_armed_until_configured(client):
    response = client.post("/api/cue", json={"armed": True})
    assert response.status_code == 409
    assert client.get("/api/cue").json()["armed"] is False


def test_a_cross_site_page_cannot_arm_the_cue(client):
    assert client.post("/api/cue", json={"armed": True}, headers={"origin": "https://evil.example"}).status_code == 403


def test_cue_requires_a_real_boolean(client):
    assert client.post("/api/cue", json={"armed": "yes"}).status_code == 422
