"""The trigger secret reaches AWS without appearing in arguments, output, or leftover files."""

import json
import os
import stat
import subprocess

from acacia.cli import check_alexa_pair, push_cue_secret, store_alexa_keys
from acacia.sanitize import clear_registered_secrets

SECRET = "test-only-trigger-value-1234567890"


class FakeAws:
    def __init__(self, returncode=0, stderr="", error=None):
        self.returncode, self.stderr, self.error = returncode, stderr, error
        self.args = None
        self.request = None
        self.mode = None
        self.path = None

    def __call__(self, args, **_kwargs):
        if self.error:
            raise self.error
        self.args = args
        self.path = args[-1].removeprefix("file://")
        self.mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.folder_mode = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
        with open(self.path) as handle:
            self.request = json.load(handle)
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def teardown_function():
    clear_registered_secrets()


def test_secret_goes_through_a_private_file_that_is_removed():
    aws = FakeAws()
    assert push_cue_secret(SECRET, run=aws) is None
    assert aws.args[:4] == ["aws", "ssm", "put-parameter", "--region"]
    assert all(SECRET not in arg for arg in aws.args)
    assert aws.request == {"Name": "/acacia/trigger_secret", "Type": "SecureString", "Value": SECRET,
                           "Overwrite": True}
    assert aws.mode == 0o600
    assert aws.folder_mode == 0o700
    assert not os.path.exists(os.path.dirname(aws.path))


def test_aws_error_is_shown_with_the_secret_scrubbed():
    aws = FakeAws(returncode=254, stderr=f"An error occurred (ExpiredToken) near {SECRET}\n")
    message = push_cue_secret(SECRET, run=aws)
    assert "ExpiredToken" in message
    assert SECRET not in message
    assert not os.path.exists(os.path.dirname(aws.path))


def test_missing_aws_cli_and_timeout_are_reported():
    assert "not found" in push_cue_secret(SECRET, run=FakeAws(error=FileNotFoundError()))
    timeout = FakeAws(error=subprocess.TimeoutExpired("aws", 60))
    assert "60 seconds" in push_cue_secret(SECRET, run=timeout)


# -- Alexa client ID and secret ------------------------------------------------

CLIENT_ID = "amzn1.application-oa2-client.0123456789abcdef0123456789abcdef"
CLIENT_SECRET = "amzn1.oa2-cs.v1.test-only-0123456789abcdef0123456789abcdef"


def answers(*values):
    queue = list(values)
    return lambda _prompt: queue.pop(0)


def recorder(stored, fail_on=None):
    def put(name, value):
        if name == fail_on:
            return "AccessDenied"
        stored[name] = value
        return None
    return put


def ok(_client_id, _client_secret):
    return None


def test_alexa_keys_go_to_the_two_parameters(capsys):
    stored = {}
    assert store_alexa_keys(answers(f" {CLIENT_ID} ", CLIENT_SECRET), recorder(stored), ok) == 0
    assert stored == {"/acacia/lwa_client_id": CLIENT_ID, "/acacia/lwa_client_secret": CLIENT_SECRET}
    out = capsys.readouterr().out
    assert CLIENT_SECRET not in out and CLIENT_ID not in out


def test_alexa_keys_refuse_obvious_paste_mistakes():
    for pasted in (("not-an-id", CLIENT_SECRET), (CLIENT_ID, CLIENT_ID), (CLIENT_ID, "short"),
                   (CLIENT_ID, "has a space in the middle of it 0123456789")):
        stored = {}
        assert store_alexa_keys(answers(*pasted), recorder(stored), ok) == 1
        assert stored == {}


def test_alexa_keys_report_an_aws_refusal():
    stored = {}
    put = recorder(stored, fail_on="/acacia/lwa_client_secret")
    assert store_alexa_keys(answers(CLIENT_ID, CLIENT_SECRET), put, ok) == 1


def amazon(status, error=None):
    body = json.dumps({"error": error} if error else {"access_token": "t"}).encode()
    return lambda form: (status, body)


def test_pair_check_tells_the_two_pairs_apart():
    assert check_alexa_pair(CLIENT_ID, CLIENT_SECRET, amazon(200)) is None
    assert "security profile" in check_alexa_pair(CLIENT_ID, CLIENT_SECRET, amazon(400, "invalid_scope"))
    assert "same page" in check_alexa_pair(CLIENT_ID, CLIENT_SECRET, amazon(401, "invalid_client"))
    assert "status 500" in check_alexa_pair(CLIENT_ID, CLIENT_SECRET, amazon(500))


def test_pair_check_asks_for_the_skill_messaging_scope():
    sent = []
    check_alexa_pair(CLIENT_ID, CLIENT_SECRET, lambda form: sent.append(form) or (200, b"{}"))
    assert b"scope=alexa%3Askill_messaging" in sent[0] and b"grant_type=client_credentials" in sent[0]


def test_a_refused_pair_is_not_stored(capsys):
    stored = {}
    check = lambda i, s: check_alexa_pair(i, s, amazon(400, "invalid_scope"))
    assert store_alexa_keys(answers(CLIENT_ID, CLIENT_SECRET), recorder(stored), check) == 1
    assert stored == {}
    out = capsys.readouterr().out
    assert "security profile" in out and CLIENT_SECRET not in out


def test_unreachable_amazon_is_reported_not_stored():
    def offline(form):
        raise OSError("no network")
    assert "Could not reach" in check_alexa_pair(CLIENT_ID, CLIENT_SECRET, offline)
