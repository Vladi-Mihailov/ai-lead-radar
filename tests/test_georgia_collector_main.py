"""Tests for reader/georgia_collector/__main__.py's load_timeouts() -- the
only piece of that entrypoint module that was pulled out into a directly
testable function (см. задачу "fix the remaining issues discovered during
live testing": __main__.py previously had zero unit tests at all, since all
config-parsing lived inline in async main())."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.georgia_collector.__main__ import load_timeouts


def test_load_timeouts_defaults_when_env_unset():
    request_timeout, submission_timeout = load_timeouts(env={})
    assert request_timeout == 30.0
    assert submission_timeout == 180.0


def test_load_timeouts_reads_overrides_from_given_env():
    request_timeout, submission_timeout = load_timeouts(
        env={
            "GEORGIA_COLLECTOR_REQUEST_TIMEOUT": "45",
            "GEORGIA_COLLECTOR_SUBMISSION_TIMEOUT": "240",
        }
    )
    assert request_timeout == 45.0
    assert submission_timeout == 240.0


def test_load_timeouts_partial_override_keeps_other_default():
    request_timeout, submission_timeout = load_timeouts(
        env={"GEORGIA_COLLECTOR_SUBMISSION_TIMEOUT": "90"}
    )
    assert request_timeout == 30.0
    assert submission_timeout == 90.0


def test_load_timeouts_falls_back_to_os_environ_when_env_omitted(monkeypatch):
    monkeypatch.setenv("GEORGIA_COLLECTOR_REQUEST_TIMEOUT", "12")
    monkeypatch.setenv("GEORGIA_COLLECTOR_SUBMISSION_TIMEOUT", "99")
    request_timeout, submission_timeout = load_timeouts()
    assert request_timeout == 12.0
    assert submission_timeout == 99.0
