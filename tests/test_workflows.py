"""Static checks on the GitHub Actions workflows."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SECRETS = [
    "ANTHROPIC_API_KEY",
    "SEATGEEK_CLIENT_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SHEET_ID",
    "GMAIL_SENDER",
    "GMAIL_APP_PASSWORD",
    "RESEND_API_KEY",
    "RESEND_FROM",
]
EMAIL_SECRETS = ["GMAIL_SENDER", "GMAIL_APP_PASSWORD", "RESEND_API_KEY", "RESEND_FROM"]


def load(name):
    text = (WORKFLOWS / name).read_text()
    data = yaml.safe_load(text)
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    triggers = data.get("on", data.get(True))
    return text, data, triggers


def steps(data, job):
    return data["jobs"][job]["steps"]


def run_text(data, job):
    return "\n".join(s.get("run", "") for s in steps(data, job))


@pytest.fixture(scope="module")
def weekly():
    return load("weekly.yml")


@pytest.fixture(scope="module")
def collect():
    return load("collect.yml")


def test_weekly_crons_cover_edt_and_est(weekly):
    _, _, on = weekly
    crons = {c["cron"] for c in on["schedule"]}
    assert crons == {"0 12 * * 1", "0 13 * * 1"}


def test_weekly_gate_skips_wrong_dst_trigger(weekly):
    _, data, _ = weekly
    gate = run_text(data, "gate")
    assert "America/New_York" in gate
    assert "0 12 * * 1" in gate and "-0400" in gate
    assert "0 13 * * 1" in gate and "-0500" in gate
    assert data["jobs"]["run"]["needs"] == "gate"
    assert "needs.gate.outputs.run == 'true'" in data["jobs"]["run"]["if"]


def test_weekly_dispatch_inputs(weekly):
    _, _, on = weekly
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["segment"]["type"] == "string"
    assert inputs["segment"].get("required") is False
    for name in ("dry_run", "send_test_email"):
        assert inputs[name]["type"] == "boolean"
        assert inputs[name]["default"] is False


def test_weekly_runs_cli_with_flags(weekly):
    _, data, _ = weekly
    run = run_text(data, "run")
    assert "python -m argos_leads run" in run
    for flag in ("--segment", "--dry-run", "--send-test-email"):
        assert flag in run


def test_collect_cron_and_command(collect):
    _, data, on = collect
    assert [c["cron"] for c in on["schedule"]] == ["30 11 * * *"]
    assert "workflow_dispatch" in on
    assert "python -m argos_leads collect" in run_text(data, "collect")


@pytest.mark.parametrize("name", ["weekly.yml", "collect.yml"])
def test_permissions_and_shared_concurrency(name):
    _, data, _ = load(name)
    assert data["permissions"]["contents"] == "write"
    assert data["concurrency"] == {"group": "argos-db", "cancel-in-progress": False}


@pytest.mark.parametrize(("name", "job"), [("weekly.yml", "run"), ("collect.yml", "collect")])
def test_secrets_referenced(name, job):
    _, data, _ = load(name)
    env = data["jobs"][job]["env"]
    required = SECRETS if name == "weekly.yml" else ["ANTHROPIC_API_KEY", *EMAIL_SECRETS]
    for secret in required:
        assert env[secret] == f"${{{{ secrets.{secret} }}}}"


@pytest.mark.parametrize(("name", "job"), [("weekly.yml", "run"), ("collect.yml", "collect")])
def test_setup_python_and_pinned_actions(name, job):
    _, data, _ = load(name)
    uses = [s["uses"] for s in steps(data, job) if "uses" in s]
    assert any(u.startswith("actions/checkout@v") for u in uses)
    setup = next(s for s in steps(data, job) if s.get("uses", "").startswith("actions/setup-python@v"))
    assert setup["with"]["python-version"] == "3.12"
    assert setup["with"]["cache"] == "pip"
    assert any("pip install -r requirements.txt" in s.get("run", "") for s in steps(data, job))


@pytest.mark.parametrize(("name", "job"), [("weekly.yml", "run"), ("collect.yml", "collect")])
def test_commit_step_always_runs(name, job):
    _, data, _ = load(name)
    step = next(s for s in steps(data, job) if s.get("name") == "Commit lead database")
    assert step["if"] == "always()"
    run = step["run"]
    assert "git add data/argos.db" in run
    assert 'git commit -m "chore: update lead database [skip ci]"' in run
    assert "git pull --rebase" in run and "git push" in run
    assert "for attempt in" in run


@pytest.mark.parametrize(("name", "job"), [("weekly.yml", "run"), ("collect.yml", "collect")])
def test_failure_notice_is_last(name, job):
    _, data, _ = load(name)
    step = steps(data, job)[-1]
    assert step["if"] == "failure()"
    assert "python -m argos_leads notify-failure --message" in step["run"]
    assert "RUN_URL" in step["run"] and "actions/runs/${{ github.run_id }}" in step["env"]["RUN_URL"]
