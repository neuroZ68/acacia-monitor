"""One Lambda behind the private Acacia skill.

It answers three callers, told apart by the shape of the event:

- Alexa smart home directives, for the virtual "Acacia Couch" motion sensor.
- Signed HTTPS calls from the Mac (a Lambda Function URL), which flip that sensor.
- Alexa custom skill requests, which play the voice clip from a private bucket.

An Alexa routine connects the sensor to the skill. This function cannot make an
Echo speak by itself, and it does nothing unless the Mac sends a signed request.

Standard library plus boto3, which the Lambda runtime provides. Secrets live in
SSM Parameter Store and are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape

ENDPOINT_ID = "acacia-couch-sensor"
FRIENDLY_NAME = "Acacia Couch"
EVENT_GATEWAY = "https://api.amazonalexa.com/v3/events"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

MAX_CLOCK_SKEW_S = 60  # a signed request older than this is refused
MIN_TRIGGER_GAP_S = 60  # a cloud-side floor, whatever the Mac asks for
MIN_REPORT_GAP_S = 30  # Amazon's required wait between DETECTED and NOT_DETECTED
TOKEN_MARGIN_S = 60
PRESIGN_EXPIRY_S = 60
RETRYABLE = (429, 500, 503)
MAX_ATTEMPTS = 3

DETECTED = "DETECTED"
NOT_DETECTED = "NOT_DETECTED"

_store = None
_secrets: dict[str, str] = {}


# -- infrastructure ---------------------------------------------------------


class DynamoStore:
    """All state fits in one item. Numbers are strings to avoid Decimal handling."""

    def __init__(self, table_name: str):
        import boto3

        self.table = boto3.resource("dynamodb").Table(table_name)

    def load(self) -> dict:
        return self.table.get_item(Key={"pk": "alexa"}).get("Item", {})

    def save(self, **fields: str) -> None:
        item = self.load()
        item.update(fields)
        item["pk"] = "alexa"
        self.table.put_item(Item=item)


def store():
    global _store
    if _store is None:
        _store = DynamoStore(os.environ["TABLE_NAME"])
    return _store


def secret(name: str) -> str:
    if name not in _secrets:
        import boto3

        reply = boto3.client("ssm").get_parameter(Name=f"/acacia/{name}", WithDecryption=True)
        _secrets[name] = reply["Parameter"]["Value"]
    return _secrets[name]


def presigned_clip_url() -> str | None:
    key = os.environ.get("CLIP_KEY", "")
    if not key:
        return None
    import boto3

    from botocore.config import Config

    # Signature Version 4 explicitly: the older default is being retired for S3, and a
    # link that stopped working would silence the cue without any visible error.
    client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                          config=Config(signature_version="s3v4"))
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": os.environ["CLIP_BUCKET"], "Key": key}, ExpiresIn=PRESIGN_EXPIRY_S)


def http_post(url: str, data: bytes, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def now() -> float:
    return time.time()


def timestamp() -> str:
    moment = datetime.fromtimestamp(now(), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 10000:02d}Z"


# -- Login with Amazon tokens ------------------------------------------------


def token_error(status: int, body: bytes) -> str:
    """Amazon's error code and description, for the log. Neither contains the credentials."""
    try:
        data = json.loads(body)
        detail = f"{str(data.get('error', 'unknown'))[:64]}: {str(data.get('error_description', ''))[:200]}"
    except (ValueError, AttributeError):
        detail = "unreadable reply"
    return f"token request failed with status {status} ({detail})"


def request_tokens(params: dict) -> str:
    form = {**params, "client_id": secret("lwa_client_id"), "client_secret": secret("lwa_client_secret")}
    status, body = http_post(LWA_TOKEN_URL, urllib.parse.urlencode(form).encode(),
                             {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    if status != 200:
        message = token_error(status, body)
        # Forget the cached credentials, so corrected ones in Parameter Store apply on the next try.
        for name in ("lwa_client_id", "lwa_client_secret"):
            value = _secrets.pop(name, "")
            if value:
                message = message.replace(value, "***")
        raise RuntimeError(message)
    data = json.loads(body)
    store().save(access_token=data["access_token"], refresh_token=data["refresh_token"],
                 expires_at=str(now() + int(data["expires_in"])))
    return data["access_token"]


def access_token(force_refresh: bool = False) -> str:
    state = store().load()
    if not state.get("refresh_token"):
        raise LookupError("not linked: enable the skill in the Alexa app first")
    fresh = float(state.get("expires_at", "0")) - now() > TOKEN_MARGIN_S
    if fresh and not force_refresh:
        return state["access_token"]
    return request_tokens({"grant_type": "refresh_token", "refresh_token": state["refresh_token"]})


# -- smart home --------------------------------------------------------------


def header(namespace: str, name: str, correlation_token: str | None = None) -> dict:
    result = {"namespace": namespace, "name": name, "messageId": str(uuid.uuid4()), "payloadVersion": "3"}
    if correlation_token:
        result["correlationToken"] = correlation_token
    return result


def sensor_property(value: str) -> dict:
    return {"namespace": "Alexa.MotionSensor", "name": "detectionState", "value": value,
            "timeOfSample": timestamp(), "uncertaintyInMilliseconds": 0}


def health_property() -> dict:
    return {"namespace": "Alexa.EndpointHealth", "name": "connectivity", "value": {"value": "OK"},
            "timeOfSample": timestamp(), "uncertaintyInMilliseconds": 0}


def discovery_endpoint() -> dict:
    reported = {"proactivelyReported": True, "retrievable": True}
    return {
        "endpointId": ENDPOINT_ID,
        "manufacturerName": "Acacia Camera",
        "description": "Virtual sensor driven by the Acacia Camera app",
        "friendlyName": FRIENDLY_NAME,
        "displayCategories": ["MOTION_SENSOR"],
        "capabilities": [
            {"type": "AlexaInterface", "interface": "Alexa.MotionSensor", "version": "3",
             "properties": {"supported": [{"name": "detectionState"}], **reported}},
            {"type": "AlexaInterface", "interface": "Alexa.EndpointHealth", "version": "3.1",
             "properties": {"supported": [{"name": "connectivity"}], **reported}},
            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
        ],
    }


def handle_directive(event: dict) -> dict:
    directive = event["directive"]
    head = directive["header"]
    namespace, name = head["namespace"], head["name"]
    print(f"directive {namespace}.{name}")  # names only; payloads can carry grant codes and tokens

    if namespace == "Alexa.Discovery" and name == "Discover":
        return {"event": {"header": header("Alexa.Discovery", "Discover.Response"),
                          "payload": {"endpoints": [discovery_endpoint()]}}}

    if namespace == "Alexa.Authorization" and name == "AcceptGrant":
        try:
            request_tokens({"grant_type": "authorization_code", "code": directive["payload"]["grant"]["code"]})
        except Exception as exc:  # noqa: BLE001 - Alexa needs a structured refusal
            # Only token_error messages are logged in full; other errors could carry anything.
            detail = f": {exc}" if isinstance(exc, RuntimeError) else ""
            print(f"AcceptGrant failed: {type(exc).__name__}{detail}")
            return {"event": {"header": header("Alexa.Authorization", "ErrorResponse"),
                              "payload": {"type": "ACCEPT_GRANT_FAILED", "message": "Token exchange failed"}}}
        return {"event": {"header": header("Alexa.Authorization", "AcceptGrant.Response"), "payload": {}}}

    if namespace == "Alexa" and name == "ReportState":
        value = store().load().get("state", NOT_DETECTED)
        return {
            "context": {"properties": [sensor_property(value), health_property()]},
            "event": {"header": header("Alexa", "StateReport", head.get("correlationToken")),
                      "endpoint": directive.get("endpoint", {"endpointId": ENDPOINT_ID}), "payload": {}},
        }

    return {"event": {"header": header("Alexa", "ErrorResponse", head.get("correlationToken")),
                      "payload": {"type": "INVALID_DIRECTIVE", "message": f"Unsupported: {namespace}.{name}"}}}


def change_report(value: str, token: str) -> dict:
    return {
        "context": {"properties": [health_property()]},
        "event": {
            "header": header("Alexa", "ChangeReport"),
            "endpoint": {"scope": {"type": "BearerToken", "token": token}, "endpointId": ENDPOINT_ID},
            "payload": {"change": {"cause": {"type": "PHYSICAL_INTERACTION"}, "properties": [sensor_property(value)]}},
        },
    }


def send_change(value: str) -> None:
    token = access_token()
    for attempt in range(MAX_ATTEMPTS):
        status, body = http_post(EVENT_GATEWAY, json.dumps(change_report(value, token)).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        print(f"change report {value}: status {status} {body[:200].decode('utf-8', 'replace')}".rstrip())
        if status == 202:
            store().save(state=value, state_changed_at=str(now()))
            return
        if status == 401 and attempt == 0:
            token = access_token(force_refresh=True)
            continue
        if status not in RETRYABLE:
            raise RuntimeError(f"event gateway refused the report with status {status}")
        time.sleep(1)  # Amazon asks for at least one second between retries
    raise RuntimeError("event gateway did not accept the report")


# -- signed calls from the Mac ------------------------------------------------


def reply(status: int, **fields) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(fields)}


def handle_trigger(event: dict) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    expected = hmac.new(secret("trigger_secret").encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(headers.get("x-acacia-signature", ""), expected):
        return reply(401, error="bad signature")

    try:
        message = json.loads(body)
        age = abs(now() - float(message["ts"]))
        action = message["action"]
    except (ValueError, KeyError, TypeError):
        return reply(400, error="bad request")
    if age > MAX_CLOCK_SKEW_S:
        return reply(401, error="stale request")

    state = store().load()
    current = state.get("state", NOT_DETECTED)
    if action == "ping":
        return reply(200, ok=True, linked=bool(state.get("refresh_token")), sensor=current)

    since_change = now() - float(state.get("state_changed_at", "0"))
    try:
        if action == "detected":
            if now() - float(state.get("last_trigger", "0")) < MIN_TRIGGER_GAP_S:
                return reply(429, error="too soon")
            if current == DETECTED:  # an earlier reset was lost, so re-arm the sensor first
                if since_change < MIN_REPORT_GAP_S:
                    return reply(429, error="too soon")
                send_change(NOT_DETECTED)
            send_change(DETECTED)
            store().save(last_trigger=str(now()))
            return reply(200, ok=True, sensor=DETECTED)
        if action == "clear":
            if current == DETECTED:
                if since_change < MIN_REPORT_GAP_S:
                    return reply(429, error="too soon")
                send_change(NOT_DETECTED)
            return reply(200, ok=True, sensor=NOT_DETECTED)
    except LookupError as exc:
        return reply(409, error=str(exc))
    except RuntimeError as exc:
        print(f"trigger failed: {exc}")
        return reply(502, error=str(exc))
    return reply(400, error="unknown action")


# -- custom skill -------------------------------------------------------------


def speech(ssml: str) -> dict:
    return {"version": "1.0", "response": {"outputSpeech": {"type": "SSML", "ssml": f"<speak>{ssml}</speak>"},
                                           "shouldEndSession": True}}


def handle_skill(event: dict) -> dict:
    application = event.get("context", {}).get("System", {}).get("application", {}).get("applicationId")
    expected = os.environ.get("SKILL_ID")
    if expected and application != expected:
        raise ValueError("request is from a different skill")
    if event["request"]["type"] == "SessionEndedRequest":
        return {"version": "1.0", "response": {}}
    url = presigned_clip_url()
    if not url:
        return speech("The Acacia voice clip is not set up yet.")
    return speech(f'<audio src="{escape(url, {chr(34): "&quot;"})}"/>')


def lambda_handler(event: dict, context=None) -> dict:
    if "directive" in event:
        return handle_directive(event)
    if "http" in event.get("requestContext", {}):
        return handle_trigger(event)
    if "request" in event:
        return handle_skill(event)
    raise ValueError("unrecognized event")
