import subprocess

from acacia.secret_scan import scan


def repo(tmp_path, name, text):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / name).write_text(text)
    return tmp_path


def test_finds_a_url_with_credentials_without_printing_it(tmp_path):
    findings = scan(repo(tmp_path, "a.py", 'U = "rtsp://admin:hunter2@10.0.0.8/stream2"\n'))
    assert findings == ["a.py:1: URL with embedded credentials"]
    assert "hunter2" not in findings[0]


def test_finds_a_hardcoded_password(tmp_path):
    assert scan(repo(tmp_path, "b.toml", 'password = "hunter2"\n'))


def test_clean_file_and_ignored_file(tmp_path):
    root = repo(tmp_path, "c.py", 'url = "rtsp://10.0.0.8:554/stream2"\n')
    (root / ".gitignore").write_text("secrets.local.toml\n")
    (root / "secrets.local.toml").write_text('password = "hunter2"\n')
    assert scan(root) == []
