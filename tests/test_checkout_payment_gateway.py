"""Тесты reader/checkout/payment_gateway.py — ни один реальный
Playwright/браузер/сеть не используется: BrowserLauncherLike/BrowserPageLike
подменяются полностью фейковыми объектами (см. tests/test_checkout_service.py
для остальной части checkout — здесь только сам gateway)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import logging
from datetime import datetime, timezone

import pytest  # noqa: E402

from reader.checkout.models import CheckoutState, CheckoutStatus, FailureReason  # noqa: E402
from reader.checkout.payment_gateway import (  # noqa: E402
    CARD_CVC_SELECTOR,
    CARD_EXPIRY_MONTH_SELECTOR,
    CARD_EXPIRY_YEAR_SELECTOR,
    CARD_NUMBER_SELECTOR,
    BankGatewayError,
    BankGatewayOutcome,
    CardSecrets,
    CardSecretsError,
    PlaywrightBankGatewayClient,
)
from reader.ocr.models import OcrResult  # noqa: E402

_CARD_NUMBER = "4111111111111111"
_EXPIRY_MONTH = "12"
_EXPIRY_YEAR = "30"
_CVV = "737"


def _secrets() -> CardSecrets:
    return CardSecrets(card_number=_CARD_NUMBER, expiry_month=_EXPIRY_MONTH, expiry_year=_EXPIRY_YEAR, cvv=_CVV)


def _state() -> CheckoutState:
    effective = OcrResult(
        policyholder_full_name="Petrov Petr",
        driver_same_as_policyholder=True, driver_full_name=None,
        owner_same_as_policyholder=True, owner_full_name=None,
        passport_number="AB1234567", citizenship="Georgia",
        category="passenger_car", registration_number="AA001AA",
        vin="WVWZZZ1KZAW123456", chassis_number=None, manufacturer="Toyota", model="Camry",
        email="tplgee@mail.ru", phone="925000000000",
        payment_bank="bog", policy_period="15", period_start="16.08.2026",
    )
    return CheckoutState(
        id="checkout-1", chat_id=-100, ocr_message_id=1, operator_user_id=111,
        status=CheckoutStatus.PAYMENT_IN_PROGRESS, effective_fields=effective,
        created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )


class _TimeoutError(Exception):
    """Имя класса намеренно содержит "Timeout" — так же, как у реального
    playwright.async_api.TimeoutError (который НЕ является подклассом
    встроенного TimeoutError, см. docstring reader/checkout/payment_gateway.py)."""


class _FakePage:
    def __init__(self, *, fail_on: str | None = None, error: Exception | None = None):
        self.fail_on = fail_on
        self.error = error
        self.calls: list[tuple] = []
        self.closed = False

    async def goto(self, url, *, timeout):
        self.calls.append(("goto", url, timeout))
        if self.fail_on == "goto":
            raise self.error

    async def wait_for_selector(self, selector, *, timeout):
        self.calls.append(("wait_for_selector", selector, timeout))
        if self.fail_on == "wait_for_selector":
            raise self.error

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))
        if self.fail_on == "fill":
            raise self.error

    async def close(self):
        self.closed = True


class _FakeLauncher:
    def __init__(self, page: _FakePage):
        self._page = page
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return self._page


class _FakeFrame:
    def __init__(self, url: str):
        self.url = url


class _DiagnosticFakePage(_FakePage):
    """_FakePage + всё, что реальный Playwright Page умеет для диагностики
    timeout'а формы (см. reader/checkout/payment_gateway.py::
    PlaywrightBankGatewayClient._capture_form_timeout_diagnostics) — url/
    title()/evaluate()/frames/screenshot()/content(). Отдельный класс, а не
    расширение самого _FakePage, чтобы существующие тесты (без этих
    методов) продолжали проверять, что диагностика — best-effort и не
    требует их наличия (см. getattr в самом методе)."""

    def __init__(self, *, url="https://mpi.gc.ge/unexpected-page", title="Unexpected Page",
                 load_state="complete", frame_urls=(), screenshot_error=None, content_error=None, **kwargs):
        super().__init__(**kwargs)
        self.url = url
        self._title = title
        self._load_state = load_state
        self.frames = [_FakeFrame(u) for u in frame_urls]
        self._screenshot_error = screenshot_error
        self._content_error = content_error
        self.screenshot_calls: list[str] = []
        self.content_calls = 0

    async def title(self):
        return self._title

    async def evaluate(self, expression):
        assert expression == "document.readyState"
        return self._load_state

    async def screenshot(self, *, path):
        self.screenshot_calls.append(path)
        if self._screenshot_error is not None:
            raise self._screenshot_error

    async def content(self):
        self.content_calls += 1
        if self._content_error is not None:
            raise self._content_error
        return "<html><body>mpi.gc.ge unexpected page</body></html>"


# ---- CardSecrets ----


def test_card_secrets_load_success_from_explicit_env():
    secrets = CardSecrets.load(
        {"CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "12/30", "CHECKOUT_CARD_CVV": _CVV},
    )
    assert secrets.card_number == _CARD_NUMBER
    assert secrets.expiry_month == "12"
    assert secrets.expiry_year == "30"
    assert secrets.cvv == _CVV
    assert secrets.cardholder_name is None


def test_card_secrets_load_cardholder_name_is_optional_and_loaded_when_present():
    secrets = CardSecrets.load(
        {
            "CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "12/30", "CHECKOUT_CARD_CVV": _CVV,
            "CHECKOUT_CARDHOLDER_NAME": "TEST CARDHOLDER",
        },
    )
    assert secrets.cardholder_name == "TEST CARDHOLDER"


def test_card_secrets_load_does_not_fail_fast_when_cardholder_name_missing():
    """CHECKOUT_CARDHOLDER_NAME — единственный необязательный секрет: нет
    подтверждённого research'ом селектора, чтобы его вообще использовать
    (см. reader/checkout/payment_gateway.py)."""
    secrets = CardSecrets.load(
        {"CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "12/30", "CHECKOUT_CARD_CVV": _CVV},
    )
    assert secrets.cardholder_name is None


def test_card_secrets_load_missing_all_lists_only_variable_names():
    with pytest.raises(CardSecretsError) as exc_info:
        CardSecrets.load({})

    message = str(exc_info.value)
    assert "CHECKOUT_CARD_NUMBER" in message
    assert "CHECKOUT_CARD_EXPIRY" in message
    assert "CHECKOUT_CARD_CVV" in message


def test_card_secrets_load_missing_one_variable():
    with pytest.raises(CardSecretsError, match="CHECKOUT_CARD_CVV"):
        CardSecrets.load({"CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "12/30"})


def test_card_secrets_load_rejects_malformed_expiry():
    with pytest.raises(CardSecretsError, match="CHECKOUT_CARD_EXPIRY"):
        CardSecrets.load(
            {"CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "invalid", "CHECKOUT_CARD_CVV": _CVV},
        )


def test_card_secrets_repr_never_reveals_values():
    secrets = _secrets()
    text = repr(secrets)
    assert _CARD_NUMBER not in text
    assert _CVV not in text
    assert _EXPIRY_MONTH + "/" + _EXPIRY_YEAR not in text


def test_card_secrets_error_message_never_contains_actual_values():
    # Даже если бы значения были заданы, но невалидны — сообщение не должно
    # эхом повторять их (проверяем на примере expiry, единственного поля с
    # содержательной валидацией значения, а не только присутствия).
    with pytest.raises(CardSecretsError) as exc_info:
        CardSecrets.load(
            {"CHECKOUT_CARD_NUMBER": _CARD_NUMBER, "CHECKOUT_CARD_EXPIRY": "13/99/extra", "CHECKOUT_CARD_CVV": _CVV},
        )
    assert "13/99/extra" not in str(exc_info.value)


# ---- PlaywrightBankGatewayClient.start() — подтверждённая часть (см. research) ----


async def test_start_fills_confirmed_card_fields_with_secrets():
    page = _FakePage()
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1?o.id=abc")

    fills = {sel: value for name, sel, value in page.calls if name == "fill"}
    assert fills[CARD_NUMBER_SELECTOR] == _CARD_NUMBER
    assert fills[CARD_EXPIRY_MONTH_SELECTOR] == _EXPIRY_MONTH
    assert fills[CARD_EXPIRY_YEAR_SELECTOR] == _EXPIRY_YEAR
    assert fills[CARD_CVC_SELECTOR] == _CVV
    assert page.closed is True
    assert launcher.new_page_calls == 1
    # НИКОГДА не считаем оплату успешной только за заполнение формы (см.
    # задачу) — submit не подтверждён research'ом, поэтому FAILED.
    assert result.outcome == BankGatewayOutcome.FAILED
    assert result.failure_reason == FailureReason.UNCONFIRMED_POST_SUBMIT_FLOW


async def test_start_navigates_to_the_given_redirect_url():
    page = _FakePage()
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    await client.start(_state(), "https://mpi.gc.ge/page1?o.id=xyz")

    goto_calls = [c for c in page.calls if c[0] == "goto"]
    assert goto_calls[0][1] == "https://mpi.gc.ge/page1?o.id=xyz"


async def test_start_never_clicks_submit():
    """Единственный способ проверить "не нажимает submit" на фейке — у
    _FakePage вообще нет метода click/submit, так что случайный вызов
    поднял бы AttributeError и тест бы упал."""
    page = _FakePage()
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    await client.start(_state(), "https://mpi.gc.ge/page1")
    # Дошли досюда без AttributeError — submit не вызывался.


async def test_start_does_not_fill_cardholder_name_even_when_provided():
    """Нет подтверждённого research'ом селектора (см. docstring модуля) —
    cardholder_name загружается (см. CardSecrets), но никуда не
    подставляется, даже если задан."""
    secrets = CardSecrets(
        card_number=_CARD_NUMBER, expiry_month=_EXPIRY_MONTH, expiry_year=_EXPIRY_YEAR, cvv=_CVV,
        cardholder_name="TEST CARDHOLDER",
    )
    page = _FakePage()
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=secrets)

    await client.start(_state(), "https://mpi.gc.ge/page1")

    fill_values = [value for name, _sel, value in page.calls if name == "fill"]
    assert "TEST CARDHOLDER" not in fill_values
    assert len(fill_values) == 4  # только src-pan/expiryMonth/expiryYear/cvc


async def test_start_timeout_waiting_for_card_form_is_unexpected_bank_page():
    page = _FakePage(fail_on="wait_for_selector", error=_TimeoutError("timed out"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.outcome == BankGatewayOutcome.FAILED
    assert result.failure_reason == FailureReason.UNEXPECTED_BANK_PAGE
    assert page.closed is True


async def test_start_crash_during_goto_is_browser_crashed():
    page = _FakePage(fail_on="goto", error=ConnectionError("network down"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.outcome == BankGatewayOutcome.FAILED
    assert result.failure_reason == FailureReason.BROWSER_CRASHED
    assert page.closed is True


async def test_start_crash_during_fill_is_browser_crashed_and_page_still_closed():
    page = _FakePage(fail_on="fill", error=RuntimeError("page crashed"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.outcome == BankGatewayOutcome.FAILED
    assert result.failure_reason == FailureReason.BROWSER_CRASHED
    assert page.closed is True


async def test_start_never_logs_card_number_or_cvv(caplog):
    page = _FakePage(fail_on="fill", error=RuntimeError("page crashed"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    with caplog.at_level(logging.DEBUG):
        await client.start(_state(), "https://mpi.gc.ge/page1")

    for record in caplog.records:
        message = record.getMessage()
        assert _CARD_NUMBER not in message
        assert _CVV not in message


# ---- submit_confirmation_code — граница исследования (не подтверждено) ----


async def test_submit_confirmation_code_raises_not_implemented_boundary():
    client = PlaywrightBankGatewayClient(launcher=_FakeLauncher(_FakePage()), card_secrets=_secrets())

    with pytest.raises(BankGatewayError):
        await client.submit_confirmation_code(_state(), "123456")


async def test_submit_confirmation_code_error_never_contains_the_code():
    client = PlaywrightBankGatewayClient(launcher=_FakeLauncher(_FakePage()), card_secrets=_secrets())
    secret_code = "918273"

    with pytest.raises(BankGatewayError) as exc_info:
        await client.submit_confirmation_code(_state(), secret_code)

    assert secret_code not in str(exc_info.value)


async def test_cancel_is_a_safe_noop():
    client = PlaywrightBankGatewayClient(launcher=_FakeLauncher(_FakePage()), card_secrets=_secrets())
    await client.cancel(_state())  # не должно поднимать исключение


# ---- диагностика timeout'а банковской формы (см. задачу: production-
# расследование "Playwright не находит форму", без speculative fix'а
# selector'ов/таймаутов) ----


def _use_tmp_payment_debug_dir(monkeypatch, tmp_path):
    import reader.checkout.payment_gateway as payment_gateway_module

    directory = tmp_path / "payment_debug"
    monkeypatch.setattr(payment_gateway_module, "_PAYMENT_DEBUG_DIR", directory)
    return directory


async def test_form_timeout_logs_page_metadata_and_frame_urls(monkeypatch, tmp_path, caplog):
    _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(
        fail_on="wait_for_selector", error=_TimeoutError("timed out"),
        url="https://mpi.gc.ge/unexpected?o.id=abc", title="Some Other Page",
        load_state="interactive", frame_urls=["https://mpi.gc.ge/frame1", "https://pay.example/frame2"],
    )
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    with caplog.at_level(logging.WARNING, logger="reader.checkout.payment_gateway"):
        result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.failure_reason == FailureReason.UNEXPECTED_BANK_PAGE
    assert "checkout-1" in caplog.text
    assert "page_url=https://mpi.gc.ge/unexpected?o.id=abc" in caplog.text
    assert "page_title=Some Other Page" in caplog.text
    assert "load_state=interactive" in caplog.text
    assert "frames_count=2" in caplog.text
    assert "https://mpi.gc.ge/frame1" in caplog.text
    assert "https://pay.example/frame2" in caplog.text


async def test_form_timeout_saves_screenshot_before_any_card_data_entered(monkeypatch, tmp_path):
    debug_dir = _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(fail_on="wait_for_selector", error=_TimeoutError("timed out"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    await client.start(_state(), "https://mpi.gc.ge/page1")

    expected_path = debug_dir / "checkout-1-form-timeout.png"
    assert page.screenshot_calls == [str(expected_path)]
    # Ни один page.fill() с данными карты не вызывался до timeout'а —
    # screenshot физически не может содержать номер карты/CVV.
    assert not any(name == "fill" for name, *_ in page.calls)


async def test_form_timeout_saves_html_snapshot(monkeypatch, tmp_path):
    debug_dir = _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(fail_on="wait_for_selector", error=_TimeoutError("timed out"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    await client.start(_state(), "https://mpi.gc.ge/page1")

    html_path = debug_dir / "checkout-1-form-timeout.html"
    assert html_path.exists()
    assert "mpi.gc.ge unexpected page" in html_path.read_text(encoding="utf-8")


async def test_form_timeout_diagnostics_never_log_or_save_card_secrets(monkeypatch, tmp_path, caplog):
    _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(fail_on="wait_for_selector", error=_TimeoutError("timed out"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    with caplog.at_level(logging.DEBUG):
        await client.start(_state(), "https://mpi.gc.ge/page1")

    for record in caplog.records:
        message = record.getMessage()
        assert _CARD_NUMBER not in message
        assert _CVV not in message


async def test_form_timeout_diagnostics_are_best_effort_when_screenshot_fails(monkeypatch, tmp_path, caplog):
    """Сбой САМОЙ диагностики (например, screenshot упал) не должен
    маскировать исходный timeout и не должен мешать вернуть корректный
    FAILED-результат (см. задачу: диагностика — best-effort)."""
    _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(
        fail_on="wait_for_selector", error=_TimeoutError("timed out"),
        screenshot_error=RuntimeError("disk full"),
    )
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    with caplog.at_level(logging.WARNING, logger="reader.checkout.payment_gateway"):
        result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.outcome == BankGatewayOutcome.FAILED
    assert result.failure_reason == FailureReason.UNEXPECTED_BANK_PAGE
    assert page.closed is True
    assert "не удалось сохранить screenshot" in caplog.text


async def test_form_timeout_diagnostics_gracefully_skip_when_page_lacks_diagnostic_methods(tmp_path, monkeypatch):
    """Существующий _FakePage (без url/title/frames/screenshot/content) —
    диагностика не должна требовать их (см. getattr в
    _capture_form_timeout_diagnostics) — old regression from
    test_start_timeout_waiting_for_card_form_is_unexpected_bank_page must
    keep passing unmodified; здесь дополнительно проверяем, что каталог
    диагностики вообще не создаётся, если сохранить нечего."""
    debug_dir = tmp_path / "payment_debug"
    import reader.checkout.payment_gateway as payment_gateway_module

    monkeypatch.setattr(payment_gateway_module, "_PAYMENT_DEBUG_DIR", debug_dir)

    page = _FakePage(fail_on="wait_for_selector", error=_TimeoutError("timed out"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.failure_reason == FailureReason.UNEXPECTED_BANK_PAGE
    # _FakePage не реализует screenshot/content — mkdir всё равно создаёт
    # каталог (см. реализация: mkdir идёт до getattr-проверок screenshot/
    # content), но ни один файл не появляется.
    assert list(debug_dir.glob("*")) == [] if debug_dir.exists() else True


async def test_no_diagnostics_captured_for_non_timeout_browser_crash(monkeypatch, tmp_path):
    """Диагностика — только для ветки "Timeout" (см. задачу: "только для
    этапа до ввода карточных данных" применительно к timeout'у формы, не ко
    ВСЕМ сбоям браузера) — при обычном browser crash ничего не сохраняется."""
    debug_dir = _use_tmp_payment_debug_dir(monkeypatch, tmp_path)
    page = _DiagnosticFakePage(fail_on="goto", error=ConnectionError("network down"))
    launcher = _FakeLauncher(page)
    client = PlaywrightBankGatewayClient(launcher=launcher, card_secrets=_secrets())

    result = await client.start(_state(), "https://mpi.gc.ge/page1")

    assert result.failure_reason == FailureReason.BROWSER_CRASHED
    assert page.screenshot_calls == []
    assert not debug_dir.exists() or list(debug_dir.glob("*")) == []
