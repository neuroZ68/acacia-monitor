from pathlib import Path

import pytest

from acacia.config import Config, ConfigError, apply_overrides, load_config, tunable_values, validate

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def base(extra=""):
    return f'[camera]\nhost = "192.0.2.1"\n{extra}'


def test_example_config_parses_and_needs_a_host(tmp_path):
    path = write(tmp_path, (ROOT / "config.example.toml").read_text())
    with pytest.raises(ConfigError, match="camera.host"):
        load_config(path)


def test_example_config_holds_only_placeholders():
    text = (ROOT / "config.example.toml").read_text()
    assert "10.0.0." not in text
    assert "@" not in text


def test_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, base()))
    assert cfg.server.host == "127.0.0.1"


def test_unknown_keys_are_errors(tmp_path):
    with pytest.raises(ConfigError, match="Unknown key"):
        load_config(write(tmp_path, base("hots = 1\n")))
    with pytest.raises(ConfigError, match="Unknown section"):
        load_config(write(tmp_path, base() + "\n[nope]\nx = 1\n"))


def test_wrong_types_are_errors(tmp_path):
    with pytest.raises(ConfigError, match="port"):
        load_config(write(tmp_path, base('port = "554"\n')))


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.146", "example.com", "::"])
def test_ui_cannot_bind_beyond_loopback(tmp_path, host):
    with pytest.raises(ConfigError, match="loopback"):
        load_config(write(tmp_path, base() + f'\n[server]\nhost = "{host}"\n'))


def test_credentials_in_the_host_field_are_refused(tmp_path):
    text = '[camera]\nhost = "user:pass@10.0.0.8"\n'
    with pytest.raises(ConfigError, match="bare host"):
        load_config(write(tmp_path, text))


def test_hold_must_cover_clear_confirm(tmp_path):
    with pytest.raises(ConfigError, match="hold_s"):
        load_config(write(tmp_path, base() + "\n[state]\nhold_s = 1.0\nclear_confirm_s = 5.0\n"))


def test_overrides_are_range_checked():
    cfg = Config()
    assert apply_overrides(cfg, {"state.on_confirm_s": 4}).state.on_confirm_s == 4.0
    with pytest.raises(ConfigError, match="between"):
        apply_overrides(cfg, {"state.on_confirm_s": -1})
    with pytest.raises(ConfigError, match="not a runtime"):
        apply_overrides(cfg, {"server.host": 1})
    with pytest.raises(ConfigError, match="number"):
        apply_overrides(cfg, {"state.on_confirm_s": True})


def test_integer_tunables_stay_integers():
    assert apply_overrides(Config(), {"retention.max_events": 5000}).retention.max_events == 5000


def test_tunable_values_cover_required_settings():
    values = tunable_values(Config())
    for key in ("detector.min_confidence", "state.on_confirm_s", "state.clear_confirm_s",
                "state.stale_timeout_s", "detector.inference_interval_s", "retention.max_events"):
        assert key in values


def test_unknown_context_class_is_caught_at_startup(tmp_path):
    text = base() + '\n[detector]\ncontext_classes = ["wolf"]\n'
    with pytest.raises(ConfigError, match="wolf"):
        load_config(write(tmp_path, text))


def test_validate_rejects_bad_transport():
    from dataclasses import replace

    cfg = Config()
    cfg = replace(cfg, camera=replace(cfg.camera, host="192.0.2.1", transport="carrier-pigeon"))
    with pytest.raises(ConfigError, match="transport"):
        validate(cfg)
