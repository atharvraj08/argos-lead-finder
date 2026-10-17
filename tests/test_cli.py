"""CLI: argument parsing and dispatch for run / collect / notify-failure, and the
stdlib-only failure notice."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from argos_leads import __main__ as cli
from argos_leads import context as context_mod
from argos_leads import notify_failure, pipeline
from argos_leads.config import CONFIG_DIR, load_config
from argos_leads.output import email

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def calls(monkeypatch, tmp_path):
    rec = SimpleNamespace(build=[], run=[], collect=[], test_email=[], notify=[], dry=[])

    def build_context(config_dir=None, db_path=None, dry_run=False):
        ctx = SimpleNamespace(db_path=Path(db_path), dry_run=dry_run)
        rec.build.append(ctx)
        return ctx

    monkeypatch.setattr(context_mod, "build_context", build_context)
    monkeypatch.setattr(pipeline, "run_weekly", lambda ctx, segment=None: rec.run.append((ctx, segment)) or {})
    monkeypatch.setattr(pipeline, "collect_daily", lambda ctx: rec.collect.append(ctx) or {})
    monkeypatch.setattr(email, "send_test", lambda ctx: rec.test_email.append(ctx))
    monkeypatch.setattr(notify_failure, "send", lambda msg: rec.notify.append(msg))
    monkeypatch.setattr("argos_leads.report.print_dry_run", lambda ctx, stats: rec.dry.append(ctx))
    monkeypatch.setattr(context_mod, "setup_logging", lambda level="INFO": None)
    rec.db = tmp_path / "argos.db"
    return rec


def test_run_all_segments(calls):
    assert cli.main(["--db", str(calls.db), "run"]) == 0
    assert len(calls.run) == 1
    ctx, segment = calls.run[0]
    assert segment is None and ctx.dry_run is False and ctx.db_path == calls.db
    assert not calls.test_email and not calls.dry


def test_run_one_segment(calls):
    cli.main(["--db", str(calls.db), "run", "--segment", "jpop"])
    assert calls.run[0][1] == "jpop"


def test_run_dry_run_uses_temp_copy(calls):
    calls.db.write_bytes(b"original")
    cli.main(["--db", str(calls.db), "run", "--dry-run", "--segment", "kpop"])
    ctx, segment = calls.run[0]
    assert ctx.dry_run is True and segment == "kpop"
    assert ctx.db_path != calls.db and ctx.db_path.read_bytes() == b"original"
    assert calls.dry == [ctx]
    assert calls.db.read_bytes() == b"original"


def test_send_test_email_does_not_run_pipeline(calls):
    assert cli.main(["--db", str(calls.db), "run", "--send-test-email"]) == 0
    assert len(calls.test_email) == 1 and not calls.run


def test_collect(calls):
    assert cli.main(["--db", str(calls.db), "collect"]) == 0
    assert len(calls.collect) == 1 and not calls.run
    assert calls.build[0].dry_run is False


def test_notify_failure_dispatch(calls):
    assert cli.main(["notify-failure", "--message", "step X failed"]) == 0
    assert calls.notify == ["step X failed"]
    assert not calls.build  # no context, config or DB needed


@pytest.mark.parametrize("argv", [[], ["bogus"], ["notify-failure"], ["run", "--segment"]])
def test_bad_arguments_exit(argv, calls, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


# ------------------------------------------------------------ stdlib-only notify-failure

CHILD = textwrap.dedent("""
    import importlib.abc, json, os, sys
    BLOCKED = {"yaml", "pydantic", "pydantic_core", "anthropic", "requests", "gspread", "bs4", "feedparser"}

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("blocked third-party import: " + name)
            return None

    sys.meta_path.insert(0, Block())
    sys.path.insert(0, sys.argv[1])
    os.environ.update(GMAIL_SENDER="bot@example.com", GMAIL_APP_PASSWORD="pw")

    import smtplib
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append({"host": host, "port": port})
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, pw):
            sent.append({"login": user})
        def send_message(self, msg, *a, **kw):
            sent.append({"to": msg["To"], "subject": msg["Subject"], "body": msg.get_content()})

    smtplib.SMTP_SSL = FakeSMTP
    from argos_leads.__main__ import main
    rc = main(["notify-failure", "--message", "pip install failed"])
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    print(json.dumps({"rc": rc, "sent": sent, "loaded": loaded}))
""")


def test_notify_failure_stdlib_only_subprocess():
    proc = subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["rc"] == 0 and out["loaded"] == []
    expected = load_config(CONFIG_DIR).email
    sent = out["sent"]
    assert sent[0] == {"host": "smtp.gmail.com", "port": 465}
    assert sent[1] == {"login": "bot@example.com"}
    assert sent[2]["to"] == ", ".join(expected.recipients)
    assert sent[2]["subject"] == f"{expected.subject_prefix} Weekly run FAILED"
    assert "pip install failed" in sent[2]["body"]


def test_email_yaml_line_parser_matches_yaml_loader():
    recipients, method, prefix = notify_failure._read_email_yaml()
    cfg = load_config(CONFIG_DIR).email
    assert recipients == cfg.recipients and method == cfg.method and prefix == cfg.subject_prefix


def test_email_yaml_line_parser_variants(tmp_path):
    p = tmp_path / "email.yaml"
    p.write_text('recipients: ["a@x.com", b@y.com, \'c@z.org\']   # team\nmethod: resend  # alt\n'
                 'subject_prefix: "[QA]"\n')
    assert notify_failure._read_email_yaml(p) == (["a@x.com", "b@y.com", "c@z.org"], "resend", "[QA]")
    p.write_text("method: gmail_smtp\n")
    with pytest.raises(ValueError, match="no recipients"):
        notify_failure._read_email_yaml(p)


def test_notify_failure_resend(tmp_path, monkeypatch):
    p = tmp_path / "email.yaml"
    p.write_text("recipients: [a@x.com]\nmethod: resend\n")
    monkeypatch.setenv("RESEND_API_KEY", "re_key")
    monkeypatch.setenv("RESEND_FROM", "bot@example.com")
    with mock.patch("urllib.request.urlopen") as urlopen:
        notify_failure.send("boom", path=p)
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://api.resend.com/emails"
    body = json.loads(req.data)
    assert body["to"] == ["a@x.com"] and "boom" in body["text"]
    assert req.headers["Authorization"] == "Bearer re_key"
