"""The Lambda with fake storage and a fake network. Nothing here touches AWS or Alexa."""

import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-trigger-value"
NOW = 1_800_000_000.0


class FakeStore:
    def __init__(self, **state):
        self.state = state

    def load(self):
        return dict(self.state)

    def save(self, **fields):
        self.state.update(fields)


@pytest.fixture
def lf(monkeypatch):
    spec = importlib.util.spec_from_file_location("lambda_function", ROOT / "alexa" / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._store = FakeStore(access_token="good-token", refresh_token="refresh-1", expires_at=str(NOW + 3600))
    module._secrets.update(trigger_secret=SECRET, lwa_client_id="client", lwa_client_secret="client-secret")
    module.calls = []
    module.gateway_status = [202]

    def fake_post(url, data, headers):
        module.calls.append((url, data, headers))
        if url == module.LWA_TOKEN_URL:
            return 200, json.dumps({"access_token": "new-token", "refresh_token": "refresh-2", "expires_in": 3600}).encode()
        status = module.gateway_status.pop(0) if len(module.gateway_status) > 1 else module.gateway_status[0]
        return status, b""

    monkeypatch.setattr(module, "http_post", fake_post)
    monkeypatch.setattr(module, "now", lambda: NOW)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


def signed(action, ts=NOW, secret=SECRET, encode=False):
    body = json.dumps({"action": action, "ts": ts})
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    payload = base64.b64encode(body.encode()).decode() if encode else body
    return {"requestContext": {"http": {"method": "POST"}}, "headers": {"X-Acacia-Signature": signature},
            "body": payload, "isBase64Encoded": encode}


def gateway_calls(lf):
    return [json.loads(c[1]) for c in lf.calls if c[0] == lf.EVENT_GATEWAY]


def directive(namespace, name, **extra):
    head = {"namespace": namespace, "name": name, "messageId": "m", "payloadVersion": "3", **extra.pop("head", {})}
    return {"directive": {"header": head, **extra}}


# -- smart home ---------------------------------------------------------------


def test_discovery_describes_a_reportable_motion_sensor(lf):
    event = lf.lambda_handler(directive("Alexa.Discovery", "Discover", payload={}))["event"]
    assert event["header"]["name"] == "Discover.Response"
    endpoint = event["payload"]["endpoints"][0]
    assert endpoint["displayCategories"] == ["MOTION_SENSOR"]
    capabilities = {c["interface"]: c for c in endpoint["capabilities"]}
    assert capabilities["Alexa"]["version"] == "3"  # required on every endpoint
    assert capabilities["Alexa.EndpointHealth"]["version"] == "3.1"  # required on sensors
    motion = capabilities["Alexa.MotionSensor"]["properties"]
    assert motion["proactivelyReported"] is True  # routines depend on this
    assert motion["retrievable"] is True


def test_accept_grant_exchanges_and_stores_tokens_without_echoing_them(lf):
    reply = lf.lambda_handler(directive("Alexa.Authorization", "AcceptGrant", payload={
        "grant": {"type": "OAuth2.AuthorizationCode", "code": "auth-code"},
        "grantee": {"type": "BearerToken", "token": "t"}}))
    assert reply["event"]["header"]["name"] == "AcceptGrant.Response"
    assert lf._store.state["refresh_token"] == "refresh-2"
    assert b"grant_type=authorization_code" in lf.calls[0][1]
    assert "new-token" not in json.dumps(reply)


def test_accept_grant_failure_is_a_structured_refusal(lf, monkeypatch, capsys):
    monkeypatch.setattr(lf, "http_post", lambda *a: (400, b"client-secret leaked?"))
    reply = lf.lambda_handler(directive("Alexa.Authorization", "AcceptGrant", payload={"grant": {"code": "x"}}))
    assert reply["event"]["payload"]["type"] == "ACCEPT_GRANT_FAILED"
    assert "client-secret" not in capsys.readouterr().out


def test_accept_grant_failure_logs_amazons_reason_and_forgets_credentials(lf, monkeypatch, capsys):
    reply_body = json.dumps({"error": "invalid_grant",
                             "error_description": "code issued to another client, not client-secret"}).encode()
    monkeypatch.setattr(lf, "http_post", lambda *a: (400, reply_body))
    reply = lf.lambda_handler(directive("Alexa.Authorization", "AcceptGrant", payload={"grant": {"code": "x"}}))
    assert reply["event"]["payload"]["type"] == "ACCEPT_GRANT_FAILED"
    out = capsys.readouterr().out
    assert "status 400" in out and "invalid_grant" in out
    assert "client-secret" not in out
    assert "lwa_client_id" not in lf._secrets and "lwa_client_secret" not in lf._secrets
    assert lf._secrets["trigger_secret"] == SECRET


def test_report_state_returns_current_value_and_correlation_token(lf):
    lf._store.state["state"] = "DETECTED"
    reply = lf.lambda_handler(directive("Alexa", "ReportState", head={"correlationToken": "ct"},
                                        endpoint={"endpointId": lf.ENDPOINT_ID}, payload={}))
    assert reply["event"]["header"]["name"] == "StateReport"
    assert reply["event"]["header"]["correlationToken"] == "ct"
    values = {p["name"]: p["value"] for p in reply["context"]["properties"]}
    assert values == {"detectionState": "DETECTED", "connectivity": {"value": "OK"}}


def test_unknown_directive_is_refused(lf):
    reply = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn", payload={}))
    assert reply["event"]["payload"]["type"] == "INVALID_DIRECTIVE"


# -- signed trigger -----------------------------------------------------------


def test_unsigned_requests_never_reach_alexa(lf):
    event = signed("detected")
    event["headers"] = {}
    assert lf.lambda_handler(event)["statusCode"] == 401
    assert lf.calls == []


def test_wrong_secret_is_refused(lf):
    assert lf.lambda_handler(signed("detected", secret="guess"))["statusCode"] == 401
    assert lf.calls == []


def test_tampered_body_is_refused(lf):
    event = signed("ping")
    event["body"] = event["body"].replace("ping", "detected")
    assert lf.lambda_handler(event)["statusCode"] == 401


def test_replayed_old_request_is_refused(lf):
    assert lf.lambda_handler(signed("detected", ts=NOW - 120))["statusCode"] == 401
    assert lf.calls == []


def test_ping_has_no_side_effects(lf):
    reply = json.loads(lf.lambda_handler(signed("ping"))["body"])
    assert reply == {"ok": True, "linked": True, "sensor": "NOT_DETECTED"}
    assert lf.calls == []


def test_detected_sends_a_correct_change_report(lf):
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 200
    report = gateway_calls(lf)[0]
    assert report["event"]["header"]["name"] == "ChangeReport"
    assert report["event"]["endpoint"]["endpointId"] == lf.ENDPOINT_ID
    assert report["event"]["endpoint"]["scope"]["token"] == "good-token"
    change = report["event"]["payload"]["change"]
    assert change["properties"][0]["value"] == "DETECTED"
    assert change["properties"][0]["name"] == "detectionState"
    assert report["context"]["properties"][0]["name"] == "connectivity"
    assert lf._store.state["state"] == "DETECTED"


def test_base64_bodies_from_function_urls_work(lf):
    assert lf.lambda_handler(signed("ping", encode=True))["statusCode"] == 200


def test_cloud_floor_blocks_rapid_triggers_even_if_the_mac_misbehaves(lf):
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 200
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 429
    assert len(gateway_calls(lf)) == 1


def test_clear_respects_amazons_thirty_second_wait(lf):
    lf._store.state.update(state="DETECTED", state_changed_at=str(NOW - 10))
    assert lf.lambda_handler(signed("clear"))["statusCode"] == 429
    assert gateway_calls(lf) == []
    lf._store.state["state_changed_at"] = str(NOW - 31)
    assert lf.lambda_handler(signed("clear"))["statusCode"] == 200
    assert gateway_calls(lf)[0]["event"]["payload"]["change"]["properties"][0]["value"] == "NOT_DETECTED"


def test_a_lost_reset_is_repaired_before_the_next_detection(lf):
    lf._store.state.update(state="DETECTED", state_changed_at=str(NOW - 600), last_trigger=str(NOW - 600))
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 200
    values = [c["event"]["payload"]["change"]["properties"][0]["value"] for c in gateway_calls(lf)]
    assert values == ["NOT_DETECTED", "DETECTED"]


def test_not_linked_gives_guidance(lf):
    lf._store.state.pop("refresh_token")
    reply = lf.lambda_handler(signed("detected"))
    assert reply["statusCode"] == 409 and "Alexa app" in reply["body"]


def test_expired_token_is_refreshed_first(lf):
    lf._store.state["expires_at"] = str(NOW + 10)
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 200
    assert gateway_calls(lf)[0]["event"]["endpoint"]["scope"]["token"] == "new-token"


def test_rejected_token_is_refreshed_once(lf):
    lf.gateway_status = [401, 202]
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 200
    assert len(gateway_calls(lf)) == 2


def test_throttling_retries_at_most_three_times(lf):
    lf.gateway_status = [429]
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 502
    assert len(gateway_calls(lf)) == 3
    assert lf._store.state.get("state") != "DETECTED"


def test_hard_gateway_refusal_is_not_retried(lf):
    lf.gateway_status = [403]
    assert lf.lambda_handler(signed("detected"))["statusCode"] == 502
    assert len(gateway_calls(lf)) == 1


def test_tokens_never_appear_in_replies_or_logs(lf, capsys):
    lf.gateway_status = [403]
    reply = lf.lambda_handler(signed("detected"))
    output = capsys.readouterr().out + json.dumps(reply)
    assert "good-token" not in output and SECRET not in output


def test_unknown_action(lf):
    assert lf.lambda_handler(signed("explode"))["statusCode"] == 400


# -- custom skill -------------------------------------------------------------


def launch(application="amzn1.ask.skill.mine", kind="LaunchRequest"):
    return {"version": "1.0", "session": {}, "request": {"type": kind},
            "context": {"System": {"application": {"applicationId": application}}}}


def test_launch_plays_the_clip_and_ends_the_session(lf, monkeypatch):
    monkeypatch.setenv("SKILL_ID", "amzn1.ask.skill.mine")
    monkeypatch.setattr(lf, "presigned_clip_url", lambda: "https://b.s3.amazonaws.com/c.mp3?X=1&Y=2")
    response = lf.lambda_handler(launch())["response"]
    assert response["shouldEndSession"] is True
    assert response["outputSpeech"]["ssml"] == (
        '<speak><audio src="https://b.s3.amazonaws.com/c.mp3?X=1&amp;Y=2"/></speak>')


def test_missing_clip_says_so_instead_of_failing(lf, monkeypatch):
    monkeypatch.setattr(lf, "presigned_clip_url", lambda: None)
    assert "not set up" in lf.lambda_handler(launch())["response"]["outputSpeech"]["ssml"]


def test_other_skills_are_rejected(lf, monkeypatch):
    monkeypatch.setenv("SKILL_ID", "amzn1.ask.skill.mine")
    with pytest.raises(ValueError, match="different skill"):
        lf.lambda_handler(launch("amzn1.ask.skill.other"))


def test_session_end_is_acknowledged(lf):
    assert lf.lambda_handler(launch(kind="SessionEndedRequest")) == {"version": "1.0", "response": {}}


def test_unrecognized_events_fail_loudly(lf):
    with pytest.raises(ValueError):
        lf.lambda_handler({"surprise": True})


# -- deployment files ---------------------------------------------------------


def test_template_keeps_the_clip_private_and_permissions_narrow():
    text = (ROOT / "alexa" / "template.yaml").read_text()
    for line in ("BlockPublicAcls: true", "BlockPublicPolicy: true", "RestrictPublicBuckets: true"):
        assert line in text
    assert "s3:*" not in text and "dynamodb:*" not in text and "Action: \"*\"" not in text
    assert "EventSourceToken: !Ref SensorSkillId" in text
    assert "EventSourceToken: !Ref VoiceSkillId" in text


def test_public_permissions_only_work_through_the_function_url():
    text = (ROOT / "alexa" / "template.yaml").read_text()
    blocks = text.split("\n\n")
    public = [block for block in blocks if 'Principal: "*"' in block]
    assert len(public) == 2
    for block in public:
        if "Action: lambda:InvokeFunctionUrl" in block:
            assert "FunctionUrlAuthType: NONE" in block
        else:
            # Without this condition anyone with AWS credentials could invoke it directly.
            assert "Action: lambda:InvokeFunction\n" in block
            assert "InvokedViaFunctionUrl: true" in block


def test_interaction_model_has_a_valid_invocation_name():
    model = json.loads((ROOT / "alexa" / "interaction_model.json").read_text())
    name = model["interactionModel"]["languageModel"]["invocationName"]
    assert name == name.lower() and len(name.split()) >= 2
