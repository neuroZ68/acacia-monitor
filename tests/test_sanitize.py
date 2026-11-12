import logging

from acacia.sanitize import ScrubFilter, clear_registered_secrets, register_secret, scrub


def test_url_userinfo_is_removed():
    text = "Server returned 401: 'rtsp://admin:hunter2@10.0.0.8:554/stream2'"
    assert scrub(text) == "Server returned 401: 'rtsp://***@10.0.0.8:554/stream2'"


def test_url_with_percent_encoded_password():
    assert "p%40ss" not in scrub("rtsp://u:p%40ss%2Fword@host/stream2")


def test_auth_headers_are_removed():
    assert "abc" not in scrub('Authorization: Digest username="me", response="abc"')


def test_url_without_credentials_is_unchanged():
    assert scrub("rtsp://10.0.0.8:554/stream2") == "rtsp://10.0.0.8:554/stream2"


def test_registered_secrets_are_scrubbed_anywhere():
    register_secret("s3cr3t/value")
    try:
        assert "s3cr3t" not in scrub("failed with s3cr3t/value in text")
        assert "s3cr3t" not in scrub("encoded s3cr3t%2Fvalue here")
    finally:
        clear_registered_secrets()


def test_log_filter_scrubs_arguments_and_tracebacks():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "open %s", ("rtsp://a:b@h/s",), None)
    assert ScrubFilter().filter(record)
    assert record.getMessage() == "open rtsp://***@h/s"

    try:
        raise OSError("rtsp://a:hunter2@h/s refused")
    except OSError as exc:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad", (), (type(exc), exc, exc.__traceback__))
    ScrubFilter().filter(record)
    assert "hunter2" not in record.exc_text
    assert record.exc_info is None
