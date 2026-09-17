"""M11 regression tests: the operational gaps, made checkable.

Three M11 items are documentation or tooling rather than application code:
the DR runbook, the load test, and the observability contract itself. A plan
that cannot be checked rots silently - the runbook quietly stops matching the
commands it tells an operator to run, and the load test stops being runnable
after a refactor.

These tests are deliberately *structural*. They cannot prove the runbook was
rehearsed or that a load test met its target; nothing in this repository can.
What they do prove is that the procedures still reference things that exist, so
an operator following them at 3am does not hit a typo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

NEXUS_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = NEXUS_ROOT / "docs" / "DR_RUNBOOK.md"
LOAD_TEST = NEXUS_ROOT / "scripts" / "load_test.py"
RESET_SCRIPT = NEXUS_ROOT / "scripts" / "reset_local_identity.py"


def test_runbook_exists() -> None:
    assert RUNBOOK.exists(), (
        "no DR runbook. The M11 item is not satisfied by an undocumented "
        "procedure, and the one scenario with no clean recovery "
        "(NEXUS_IDENTITY_KEY loss) is exactly the one nobody improvises."
    )


def test_runbook_keeps_the_two_databases_separate() -> None:
    """Both stores must be backed up - they are separate services by design."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "nexus_gateway" in text, "the gateway database is not mentioned"
    assert "pg_dump" in text
    assert "pg_restore" in text


def test_runbook_names_every_secret_that_must_be_backed_up() -> None:
    """A data-only backup is not a recovery plan.

    `NEXUS_IDENTITY_KEY` is an AES key for the agent private keys: the rows
    restore perfectly and are unusable without it.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    for secret in (
        "NEXUS_IDENTITY_KEY",
        "NEXUS_SESSION_KEY",
        "NEXUS_GATEWAY_SECRET",
        "NEXUS_LLM_API_KEY",
    ):
        assert secret in text, f"{secret} is not listed as needing a backup"


def test_runbook_warns_that_identity_key_loss_is_unrecoverable() -> None:
    """The one irreversible failure, stated as such."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Irrecoverable" in text or "irrecoverable" in text
    # And the correct recovery, which is NOT "disable the integrity check".
    assert "reset_local_identity" in text, (
        "the runbook does not point at the opt-in identity reset, so the "
        "tempting fix is to disable the integrity check instead"
    )
    assert "refuses to boot" in text or "refuse to start" in text or "refuses to start" in text


def test_runbook_verifies_readiness_not_just_liveness() -> None:
    """`/health` returning 200 proves nothing about a restored database.

    The distinction is the whole reason both endpoints exist, so the runbook has
    to use the right one.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "/readyz" in text
    assert "indisvalid" in text, (
        "the runbook does not verify the vector index survived the restore; a "
        "restored index can be present and invalid"
    )


def test_runbook_commands_reference_files_that_exist() -> None:
    """A procedure naming a script that was renamed is a procedure that fails.

    Checked by extracting `python -m scripts.x` / `scripts/x.py` mentions.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    referenced = set(re.findall(r"scripts/([a-z_]+)\.py", text))
    referenced |= set(re.findall(r"python -m scripts\.([a-z_]+)", text))
    assert referenced, "the runbook references no scripts at all"

    missing = [
        name
        for name in sorted(referenced)
        if not (NEXUS_ROOT / "scripts" / f"{name}.py").exists()
    ]
    assert not missing, f"the runbook points at script(s) that do not exist: {missing}"


def test_runbook_states_what_it_does_not_cover() -> None:
    """An honest scope note is what stops a runbook being trusted past its limits."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "does not cover" in text.lower()
    assert "not been rehearsed" in text.lower() or "has not been rehearsed" in text.lower(), (
        "the runbook must say it is unproven; presenting an unrehearsed plan as "
        "verified is the failure this whole audit was about"
    )


# ---------------------------------------------------------------------------
# The load test
# ---------------------------------------------------------------------------


def test_load_test_is_runnable_and_self_documenting() -> None:
    """`--help` must work: a load tool nobody can invoke is not a load test."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.load_test", "--help"],
        cwd=NEXUS_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--base-url", "--concurrency", "--duration", "--rate", "--p95-budget-ms"):
        assert flag in result.stdout, f"{flag} is missing from the load test's options"


def test_load_test_refuses_to_measure_a_broken_target() -> None:
    """It must not report numbers for a replica that is not accepting traffic.

    A load test that happily drives a 503 produces a beautiful report and zero
    information. The refusal is checked here against a port nothing listens on.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.load_test",
            "--base-url",
            "http://127.0.0.1:9",  # discard port; nothing is listening
            "--duration",
            "1",
            "--concurrency",
            "1",
        ],
        cwd=NEXUS_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 2, (
        f"expected exit 2 (setup problem), got {result.returncode}.\n"
        f"stdout={result.stdout[:400]}\nstderr={result.stderr[:400]}"
    )


def test_load_test_checks_the_app_side_counter() -> None:
    """The "no unbounded growth" witness must be the app's own accounting.

    Otherwise a benchmark measures the client and calls it a server result.
    """
    source = LOAD_TEST.read_text(encoding="utf-8")
    assert "nexus_http_requests_total" in source
    assert "min_counted_ratio" in source
    # And it must fail rather than warn when the accounting does not add up.
    assert "FAIL: the app counted only" in source


# ---------------------------------------------------------------------------
# The identity reset escape hatch
# ---------------------------------------------------------------------------


def test_identity_reset_is_opt_in_and_guards_remote_hosts() -> None:
    """The escape hatch for a lost key must not be a footgun.

    It deletes the deployment's identity, so it must refuse to run without an
    explicit acknowledgement, and refuse to run against a non-local host unless
    told twice.
    """
    source = RESET_SCRIPT.read_text(encoding="utf-8")
    assert "--yes" in source
    assert "Refusing to run without --yes" in source
    assert "--i-know-what-i-am-doing" in source
    assert "Refusing to run against host" in source


def test_identity_reset_refuses_without_yes() -> None:
    """Behavioural, not just structural: the guard must actually fire."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.reset_local_identity",
            "--database-url",
            "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus",
        ],
        cwd=NEXUS_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    assert "--yes" in result.stderr


def test_identity_reset_refuses_a_remote_host() -> None:
    """A copy-pasted command must not be able to wipe a hosted deployment."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.reset_local_identity",
            "--database-url",
            "postgresql+asyncpg://u:p@db.production.example.com/x",
            "--yes",
        ],
        cwd=NEXUS_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3, result.stdout
    assert "not a local address" in result.stderr


# ---------------------------------------------------------------------------
# The observability contract, as operators consume it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting",
    ["nexus_log_format", "nexus_service_name", "nexus_log_level"],
)
def test_observability_settings_are_declared(setting: str) -> None:
    """Documented in .env.example, so a deployment can actually configure them."""
    example = NEXUS_ROOT / ".env.example"
    assert example.exists(), ".env.example is missing"
    text = example.read_text(encoding="utf-8")
    assert setting.upper() in text, (
        f"{setting.upper()} is not in .env.example, so it is undiscoverable "
        "outside the source"
    )


def test_error_envelope_codes_are_documented() -> None:
    """The code map that turns a status into a stable code must stay exhaustive.

    `/readyz` returning 503 and a domain `ServiceUnavailableError` must produce
    the same code, or a client switching on `code` breaks on one of them.
    """
    from app.main import _HTTP_STATUS_TO_CODE
    from app.errors import ErrorKind

    assert _HTTP_STATUS_TO_CODE[503] == "service_unavailable"
    assert _HTTP_STATUS_TO_CODE[404] == "not_found"
    assert _HTTP_STATUS_TO_CODE[422] == "validation_error"
    # Every mapped code must be a real value of the vocabulary, or the map has
    # invented a code no client was told about.
    import app.errors as errors

    known = {
        value
        for name, value in vars(errors).items()
        if name.isupper() and isinstance(value, str)
    }
    unknown = {
        code for code in _HTTP_STATUS_TO_CODE.values() if code not in known
    }
    assert not unknown, (
        f"_HTTP_STATUS_TO_CODE invents code(s) {sorted(unknown)} that are not in "
        "app.errors; a client cannot switch on a code it was never given"
    )
    assert ErrorKind is not None
