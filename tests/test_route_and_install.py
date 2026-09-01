import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meetily_connector import route
from meetily_connector.route import ROUTE_NAME, merge_route

ROOT = Path(__file__).resolve().parents[1]


def test_merge_preserves_other_routes():
    existing = {"github-ci": {"secret": "other", "deliver": "log"},
                "alerts": {"secret": "x", "deliver": "telegram"}}
    merged, summary = merge_route(existing, "s3cret")
    assert set(merged) == {"github-ci", "alerts", ROUTE_NAME}
    assert merged["github-ci"] == existing["github-ci"]  # untouched
    assert merged[ROUTE_NAME]["deliver"] == "telegram"
    assert merged[ROUTE_NAME]["toolsets"] == ["hermesnag"]
    assert "github-ci" in summary and "s3cret" not in summary  # redacted


def test_merge_updates_existing_route_keeps_created_at():
    existing = {ROUTE_NAME: {"secret": "old", "deliver": "log",
                              "created_at": "2026-01-01T00:00:00Z"}}
    merged, summary = merge_route(existing, "newsecret")
    assert merged[ROUTE_NAME]["created_at"] == "2026-01-01T00:00:00Z"
    assert merged[ROUTE_NAME]["secret"] == "newsecret"
    assert "UPDATED" in summary and "REPLACED" in summary
    assert "newsecret" not in summary


def test_route_cli_secret_via_stdin(monkeypatch, capsys):
    stdin = io.StringIO("topsecret\n" + json.dumps({"other": {"secret": "x"}}))
    monkeypatch.setattr(sys, "stdin", stdin)
    rc = route.main(["merge"])
    assert rc == 0
    out = capsys.readouterr()
    merged = json.loads(out.out)
    assert merged[ROUTE_NAME]["secret"] == "topsecret"
    assert "other" in merged
    assert "topsecret" not in out.err  # summary on stderr is redacted


def test_route_cli_never_takes_secret_in_argv():
    # Contract check: the module rejects any argv beyond "merge".
    assert route.main(["merge", "--secret", "oops"]) == 2


def test_render_config_diff_mode(tmp_path, monkeypatch):
    from meetily_connector import install

    target = tmp_path / "config.toml"
    monkeypatch.setattr(install, "DEFAULT_CONFIG_PATH", target)
    rc = install.main(["render-config", "--local-port", "9999"])
    assert rc == 3  # differs (no file), not written without --write
    assert not target.exists()
    rc = install.main(["render-config", "--local-port", "9999", "--write"])
    assert rc == 3 and target.exists()
    assert oct(os.stat(target).st_mode & 0o777) == "0o600"
    text = target.read_text()
    assert 'webhook_url = "http://127.0.0.1:9999/webhooks/meetily-meeting"' in text
    assert 'trigger = "transcript"' in text
    # idempotent second run
    assert install.main(["render-config", "--local-port", "9999", "--write"]) == 0


def test_render_plists_substitutions(tmp_path, monkeypatch):
    from meetily_connector import install

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(install, "DEFAULT_CONFIG_DIR", tmp_path / ".meetily-connector")
    rc = install.main(["render-plists", "--ssh-host", "mybox",
                       "--python", "/usr/bin/python3",
                       "--local-port", "8645", "--remote-port", "8644", "--write"])
    assert rc == 3
    tunnel = (tmp_path / "Library" / "LaunchAgents" /
              "me.meetily.webhook-tunnel.plist").read_text()
    assert "127.0.0.1:8645:127.0.0.1:8644" in tunnel
    assert "<string>mybox</string>" in tunnel
    assert "__" not in tunnel  # all placeholders substituted
    watch = (tmp_path / "Library" / "LaunchAgents" /
             "ai.meetily.connector.plist").read_text()
    assert "/usr/bin/python3" in watch
    assert "__PYTHON__" not in watch


def test_smoke_verb_reports_secret_problem(cfg, capsys):
    from meetily_connector.cli import run_smoke

    cfg.secret_source = "env"
    cfg.secret_env = "DEFINITELY_UNSET_VAR_12345"
    assert run_smoke(cfg) == 2
    assert "cannot resolve webhook secret" in capsys.readouterr().err


def test_packaged_templates_exist():
    pkg = ROOT / "src" / "meetily_connector" / "templates"
    assert (pkg / "ai.meetily.connector.plist").exists()
    assert (pkg / "me.meetily.webhook-tunnel.plist").exists()
    # Templates in deploy/ and package must stay in sync.
    for name in ("ai.meetily.connector.plist", "me.meetily.webhook-tunnel.plist"):
        assert (pkg / name).read_text() == (ROOT / "deploy" / name).read_text()
