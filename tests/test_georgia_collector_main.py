"""Tests for reader/georgia_collector/__main__.py's load_timeouts() and
load_page_limits() -- the pieces of that entrypoint module pulled out into
directly testable functions (см. задачу "fix the remaining issues discovered during
live testing": __main__.py previously had zero unit tests at all, since all
config-parsing lived inline in async main())."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.georgia_collector.__main__ import load_page_limits, load_timeouts


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


def test_load_page_limits_defaults_when_env_unset():
    assert load_page_limits(env={}) == (300, 300)


def test_load_page_limits_reads_overrides_from_given_env():
    assert load_page_limits(env={
        "GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE": "120",
        "GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES": "250",
    }) == (120, 250)


@pytest.mark.parametrize("name", ["GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE", "GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES"])
@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_load_page_limits_rejects_invalid_values(name, value):
    with pytest.raises(ValueError):
        load_page_limits(env={name: value})


def test_load_page_limits_falls_back_to_os_environ_when_env_omitted(monkeypatch):
    monkeypatch.setenv("GEORGIA_COLLECTOR_MAX_PAGES_PER_CYCLE", "200")
    monkeypatch.delenv("GEORGIA_COLLECTOR_FIRST_RUN_MAX_PAGES", raising=False)
    assert load_page_limits() == (200, 300)
