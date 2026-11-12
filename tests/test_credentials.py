import pytest

from acacia.credentials import CameraCredentials, CredentialError, RtspUrl, load_credentials
from acacia.sanitize import clear_registered_secrets, scrub


@pytest.fixture(autouse=True)
def _clean():
    yield
    clear_registered_secrets()


def test_reserved_characters_are_percent_encoded():
    creds = CameraCredentials("me@home", "p@ss:w/rd?#%")
    url = RtspUrl("10.0.0.8", 554, "/stream2", creds)
    assert url.reveal() == "rtsp://me%40home:p%40ss%3Aw%2Frd%3F%23%25@10.0.0.8:554/stream2"


def test_url_text_forms_hide_credentials():
    url = RtspUrl("10.0.0.8", 554, "/stream2", CameraCredentials("user", "hunter2"))
    for text in (str(url), repr(url), f"{url}", url.redacted):
        assert "hunter2" not in text and "user" not in text
    assert "10.0.0.8" in str(url)


def test_credentials_repr_is_redacted():
    creds = CameraCredentials("user", "hunter2")
    assert "hunter2" not in repr(creds) and "hunter2" not in str(creds)


def write(path, text, mode):
    path.write_text(text)
    path.chmod(mode)


def test_file_backend_reads_owner_only_file(tmp_path):
    path = tmp_path / "s.toml"
    write(path, 'username = "u"\npassword = "secret-pw"\n', 0o600)
    creds = load_credentials("file", "svc", path)
    assert creds.password == "secret-pw"
    assert scrub("x secret-pw y") == "x *** y"  # loading registers it for redaction


def test_file_backend_refuses_group_or_world_readable(tmp_path):
    path = tmp_path / "s.toml"
    write(path, 'username = "u"\npassword = "p"\n', 0o644)
    with pytest.raises(CredentialError, match="chmod 600"):
        load_credentials("file", "svc", path)


def test_file_backend_never_echoes_content_on_parse_errors(tmp_path):
    path = tmp_path / "s.toml"
    write(path, 'password = "unterminated-secret\n', 0o600)
    with pytest.raises(CredentialError) as caught:
        load_credentials("file", "svc", path)
    assert "unterminated-secret" not in str(caught.value)


def test_missing_file_and_fields(tmp_path):
    with pytest.raises(CredentialError):
        load_credentials("file", "svc", tmp_path / "missing.toml")
    path = tmp_path / "s.toml"
    write(path, 'username = "u"\n', 0o600)
    with pytest.raises(CredentialError):
        load_credentials("file", "svc", path)


def test_unknown_backend():
    with pytest.raises(CredentialError):
        load_credentials("env", "svc", None)
