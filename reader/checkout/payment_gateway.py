"""Банковский этап (Bank of Georgia, mpi.gc.ge) — см. итоговый research-отчёт
задачи. Подтверждено browser research'ом (реальный DOM, НЕ iframe):

- страница ввода карты — обычные HTML input'ы в основном документе (НЕ
  iframe: единственные iframe на странице — Google Pay payframe и пустой
  служебный iframe, поле карты — вне их):
    input#src-pan       (type=tel, "ბარათის ნომერი" — номер карты)
    input#expiryMonth   (type=tel, 2 цифры)
    input#expiryYear    (type=tel, 2 цифры)
    input#cvc           (type=tel, CVC/CVV)
  Поля cardholder name НА СТРАНИЦЕ НЕТ на том шаге, что мы видели (ни в
  форме, ни в тексте страницы) — но это могло быть прогрессивным раскрытием
  ПОСЛЕ ввода номера карты (мы это поле так и не заполнили вживую, см.
  research-отчёт), поэтому категорично утверждать "поля не существует
  вообще" нельзя. CHECKOUT_CARDHOLDER_NAME теперь загружается (см.
  CardSecrets) как ОПЦИОНАЛЬНЫЙ секрет (fail-fast по нему не срабатывает),
  но никуда не подставляется — нет подтверждённого research'ом селектора.
- сессия инициализируется до всякого ввода карты через
  POST /open/api//v4/{merchantId}/payment/{token}/start (см.
  reader/checkout/tpl_client.py — redirect на эту страницу уже приходит с
  готовым token'ом в URL) — ответ содержит amount/currency/merchant/state
  ("offer"), НИКАКИХ данных карты.
- submit — кнопка с текстом "გადახდა (<сумма> ₾)".

НЕ подтверждено (research был намеренно остановлен ДО реальной оплаты —
см. итоговый отчёт и задачу "не завершай реальную финансовую транзакцию"):
- что происходит СРАЗУ после клика по кнопке submit (остаётся на этой же
  странице / redirect / новый iframe);
- появляется ли OTP/3DS challenge, на каком домене (mpi.gc.ge/ACS банка/
  другой) и какими селекторами;
- как выглядит успешный/отклонённый/просроченный результат оплаты;
- появляется ли и где banking order id.

Поэтому PlaywrightBankGatewayClient.start() ОСТАНАВЛИВАЕТСЯ сразу после
заполнения полей карты, НЕ нажимая submit — см. докстрок класса. Всё, что
дальше (клик submit + распознавание OTP/успеха/отказа), требует ОТДЕЛЬНОГО
research с реальной картой (см. задачу: "Если для исследования понадобится
реальная карта ... — STOP") и должно быть добавлено отдельным изменением,
когда это подтверждено, а не угадано здесь.

Секреты карты — ТОЛЬКО из переменных окружения (см. задачу): нигде в этом
модуле, config.yaml или логах не хранится и не печатается ни card number,
ни CVV, ни OTP."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from reader.checkout.models import CheckoutState, FailureReason

logger = logging.getLogger(__name__)

# Каталог для production-диагностики timeout'а банковской формы (см.
# _capture_form_timeout_diagnostics ниже) — ТОЛЬКО метаданные страницы и
# screenshot/HTML ДО заполнения полей карты (см. docstring метода): ни один
# secrets/PII там появиться не может, т.к. до этой точки ни разу не
# вызывается page.fill() с данными карты. Относительно текущей рабочей
# директории процесса — тот же принцип, что и остальные data/* пути проекта
# (data/sessions, data/users.db).
_PAYMENT_DEBUG_DIR = Path("data/payment_debug")

# Подтверждены research'ом (см. docstring модуля) — реальные id полей формы
# оплаты на mpi.gc.ge.
CARD_NUMBER_SELECTOR = "#src-pan"
CARD_EXPIRY_MONTH_SELECTOR = "#expiryMonth"
CARD_EXPIRY_YEAR_SELECTOR = "#expiryYear"
CARD_CVC_SELECTOR = "#cvc"

_CARD_NUMBER_ENV = "CHECKOUT_CARD_NUMBER"
_CARD_EXPIRY_ENV = "CHECKOUT_CARD_EXPIRY"  # формат "MM/YY", см. CardSecrets.load()
_CARD_CVV_ENV = "CHECKOUT_CARD_CVV"
# Опционален (см. docstring модуля) — единственный из четырёх, без которого
# checkout НЕ отказывается стартовать: селектор для него не подтверждён.
_CARDHOLDER_NAME_ENV = "CHECKOUT_CARDHOLDER_NAME"


class BankGatewayError(Exception):
    """Банковский этап не может быть выполнен — str(exc) НЕ должен
    содержать код подтверждения/реквизиты карты ни при каких обстоятельствах."""


class CardSecretsError(BankGatewayError):
    """Секреты карты отсутствуют/некорректны — сообщение перечисляет ТОЛЬКО
    имена переменных окружения, никогда их значения (см. задачу: "При
    отсутствии необходимых secrets checkout должен fail-fast с понятной
    конфигурационной ошибкой, не раскрывающей значения")."""


@dataclass(frozen=True)
class CardSecrets:
    """Реквизиты карты — ТОЛЬКО из окружения (см. docstring модуля).
    cardholder_name — ОПЦИОНАЛЬНОЕ поле (None, если CHECKOUT_CARDHOLDER_NAME
    не задан): загружается на случай, если селектор для него подтвердится
    следующим research'ом, но PlaywrightBankGatewayClient.start() его пока
    никуда не подставляет (нет подтверждённого поля на исследованном шаге
    формы, см. docstring модуля)."""

    card_number: str
    expiry_month: str  # "MM"
    expiry_year: str  # "YY"
    cvv: str
    cardholder_name: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - защита от случайного логирования
        return "CardSecrets(***)"

    @staticmethod
    def load(env: dict[str, str] | None = None) -> CardSecrets:
        source = env if env is not None else os.environ
        card_number = source.get(_CARD_NUMBER_ENV)
        expiry = source.get(_CARD_EXPIRY_ENV)
        cvv = source.get(_CARD_CVV_ENV)
        cardholder_name = source.get(_CARDHOLDER_NAME_ENV) or None

        missing = [
            name
            for name, value in (
                (_CARD_NUMBER_ENV, card_number),
                (_CARD_EXPIRY_ENV, expiry),
                (_CARD_CVV_ENV, cvv),
            )
            if not value
        ]
        if missing:
            raise CardSecretsError(
                "Не заданы переменные окружения для оплаты картой: "
                + ", ".join(missing)
                + " (см. reader/checkout/payment_gateway.py::CardSecrets)"
            )

        month, sep, year = expiry.partition("/")
        if not sep or not month.strip().isdigit() or not year.strip().isdigit():
            raise CardSecretsError(
                f"{_CARD_EXPIRY_ENV} имеет неверный формат — ожидается 'MM/YY'"
            )

        return CardSecrets(
            card_number=card_number.strip(),
            expiry_month=month.strip(),
            expiry_year=year.strip(),
            cvv=cvv.strip(),
            cardholder_name=cardholder_name.strip() if cardholder_name else None,
        )


class BankGatewayOutcome(str, Enum):
    """Результат одного обращения к банковскому этапу — см.
    reader/checkout/service.py про то, как каждое значение отображается на
    CheckoutStatus."""

    AWAITING_CODE = "awaiting_code"  # банк показал OTP/3DS challenge
    COMPLETED = "completed"  # банк достоверно подтвердил успешную оплату
    RETRY_CODE = "retry_code"  # код неверный, но банк позволяет ввести другой
    FAILED = "failed"


@dataclass(frozen=True)
class BankGatewayResult:
    outcome: BankGatewayOutcome
    message: str
    failure_reason: FailureReason | None = None
    # Реальный order id/номер полиса от банка — только если ПОДТВЕРЖДЁН
    # источник (см. reader/checkout/models.py::CheckoutState.order_reference
    # и задачу: "добавь номер полиса только если он действительно получен").
    order_reference: str | None = None


class BankGatewayClient(Protocol):
    """Абстракция банковского этапа (mpi.gc.ge) — см. docstring модуля."""

    async def start(self, state: CheckoutState, redirect_url: str) -> BankGatewayResult: ...

    async def submit_confirmation_code(self, state: CheckoutState, code: str) -> BankGatewayResult:
        """code передаётся один раз, вызывающий код НИКОГДА не логирует его
        и не сохраняет (см. docstring модуля)."""
        ...

    async def cancel(self, state: CheckoutState) -> None:
        """Освободить браузерные ресурсы этого checkout (см. lifecycle в
        reader/checkout/service.py) — вызывается при FAILED/COMPLETED и при
        восстановлении после перезапуска (см. reader/checkout/lock_repository.py)."""
        ...


class NotImplementedBankGatewayClient:
    """Default-реализация BankGatewayClient — используется, если вызывающий
    код явно не передал реальный gateway (см. reader/checkout/service.py).
    Любой вызов честно сообщает, что дальше нужна конкретная реализация, а
    не молча "притворяется", что оплата прошла."""

    async def start(self, state: CheckoutState, redirect_url: str) -> BankGatewayResult:
        raise BankGatewayError(
            "Банковский gateway не сконфигурирован (см. reader/checkout/payment_gateway.py)."
        )

    async def submit_confirmation_code(self, state: CheckoutState, code: str) -> BankGatewayResult:
        raise BankGatewayError(
            "Банковский gateway не сконфигурирован — код подтверждения принять некому."
        )

    async def cancel(self, state: CheckoutState) -> None:
        return None


# ---------------------------------------------------------------------------
# Playwright-реализация — production browser boundary (см. задачу: "Если
# банковский flow требует браузера, используй Playwright"). Протоколы
# BrowserPage/BrowserLauncher — граница dependency injection для тестов (см.
# tests/test_checkout_payment_gateway.py, где вместо реального
# playwright.async_api подставляется фейк — ни один реальный браузер/сеть в
# тестах не используется).
# ---------------------------------------------------------------------------


class BrowserPageLike(Protocol):
    async def goto(self, url: str, *, timeout: float) -> None: ...

    async def wait_for_selector(self, selector: str, *, timeout: float) -> None: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def close(self) -> None: ...


class BrowserLauncherLike(Protocol):
    """Одна browser-сессия на один checkout (см. задачу: "Browser lifecycle
    должен быть управляемым: запуск, checkout session, cleanup")."""

    async def new_page(self) -> BrowserPageLike: ...


class PlaywrightBrowserLauncher:
    """Реальная реализация BrowserLauncherLike поверх
    playwright.async_api.async_playwright() — headless Chromium, одна
    browser-сессия на один checkout (не переиспользуется между заявками —
    см. задачу про managed lifecycle). Playwright импортируется лениво
    (внутри __init__), чтобы модуль оставался импортируемым и без пакета
    playwright установленным (например, в окружениях, где checkout вообще
    не поднят, см. reader/main.py)."""

    def __init__(self, *, headless: bool = True):
        self._headless = headless
        self._playwright = None
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        return self._browser

    async def new_page(self) -> BrowserPageLike:
        browser = await self._ensure_browser()
        context = await browser.new_context()
        return await context.new_page()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


# Таймауты браузерного этапа — намеренно небольшие константы модуля (не
# бизнес-значение, а инженерная настройка): если страница банка не
# отвечает/не показывает ожидаемую форму за это время, это ЛИБО реальный
# сбой банка, ЛИБО неожиданная страница — оба случая честно
# классифицируются, а не зависают навсегда (см. задачу про timeout/crash
# handling).
_NAVIGATION_TIMEOUT_SECONDS = 30.0
_CARD_FORM_TIMEOUT_SECONDS = 20.0


class PlaywrightBankGatewayClient:
    """Production BankGatewayClient — см. docstring модуля про то, что
    именно подтверждено, а что нет.

    start() доходит РОВНО до заполненной формы карты и останавливается
    ПЕРЕД нажатием submit (см. docstring модуля) — возвращает
    BankGatewayResult(FAILED, failure_reason=UNCONFIRMED_POST_SUBMIT_FLOW).
    Это осознанная граница, а не забытая реализация: нажатие submit — это
    реальная попытка платежа, а что происходит дальше (OTP/успех/отказ) не
    подтверждено ни одним селектором.

    submit_confirmation_code() поэтому недостижим в реальном потоке (start()
    никогда не возвращает AWAITING_CODE) — оставлен как явная граница,
    поднимающая BankGatewayError, а не пытающийся угадать OTP-форму."""

    def __init__(self, *, launcher: BrowserLauncherLike, card_secrets: CardSecrets):
        self._launcher = launcher
        self._card_secrets = card_secrets

    async def start(self, state: CheckoutState, redirect_url: str) -> BankGatewayResult:
        page: BrowserPageLike | None = None
        try:
            page = await self._launcher.new_page()
            await page.goto(redirect_url, timeout=_NAVIGATION_TIMEOUT_SECONDS)
            await page.wait_for_selector(CARD_NUMBER_SELECTOR, timeout=_CARD_FORM_TIMEOUT_SECONDS)

            await page.fill(CARD_NUMBER_SELECTOR, self._card_secrets.card_number)
            await page.fill(CARD_EXPIRY_MONTH_SELECTOR, self._card_secrets.expiry_month)
            await page.fill(CARD_EXPIRY_YEAR_SELECTOR, self._card_secrets.expiry_year)
            await page.fill(CARD_CVC_SELECTOR, self._card_secrets.cvv)
            # TODO(research): если/когда селектор для cardholder name
            # подтвердится (см. CardSecrets.cardholder_name и docstring
            # модуля), заполнить его здесь ДО перехода к следующему шагу —
            # намеренно не угадываем селектор сейчас.
        except Exception as exc:  # noqa: BLE001 — любой сбой браузера (timeout/crash/сеть/и т.п.)
            # Классификация по имени класса исключения, а не по конкретному
            # типу: реальный playwright.async_api.TimeoutError НЕ является
            # подклассом встроенного TimeoutError, а BrowserLauncherLike/
            # BrowserPageLike (см. выше) — протоколы, не привязанные к
            # конкретной библиотеке (тесты подставляют свои фейки) — единый
            # признак "не дождались формы вовремя" во всех реализациях это
            # то, что "Timeout" есть в имени класса исключения.
            if "Timeout" in type(exc).__name__:
                logger.warning(
                    "Checkout %s: банковская форма карты не появилась вовремя (%s)",
                    state.id, type(exc).__name__,
                )
                if page is not None:
                    # Диагностика — best-effort, никогда не должна маскировать
                    # исходный timeout (см. задачу: производственное
                    # расследование "почему форма не найдена", без speculative
                    # fix'а selector'ов/таймаутов).
                    try:
                        await self._capture_form_timeout_diagnostics(state, page)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Checkout %s: диагностика timeout формы упала неожиданно", state.id,
                        )
                await self._safe_close(page)
                return BankGatewayResult(
                    outcome=BankGatewayOutcome.FAILED,
                    message="Банк не показал ожидаемую форму оплаты вовремя.",
                    failure_reason=FailureReason.UNEXPECTED_BANK_PAGE,
                )
            # НЕ логируем str(exc) — может содержать URL с query-параметрами
            # заявки или содержимое страницы; тип исключения достаточно для
            # диагностики (см. задачу: секреты никогда не должны попасть в
            # exception text — то же самое применяем и к прочим деталям).
            logger.warning(
                "Checkout %s: браузерная сессия банка упала (%s)", state.id, type(exc).__name__,
            )
            await self._safe_close(page)
            return BankGatewayResult(
                outcome=BankGatewayOutcome.FAILED,
                message="Техническая ошибка при подключении к банку.",
                failure_reason=FailureReason.BROWSER_CRASHED,
            )

        # Форма заполнена — дальше (submit + OTP/результат) не подтверждено
        # research'ом, см. docstring модуля. Сессию закрываем сразу: держать
        # открытый браузер, зная, что мы всё равно не сможем интерпретировать
        # результат клика submit, бессмысленно и рискованно.
        await self._safe_close(page)
        return BankGatewayResult(
            outcome=BankGatewayOutcome.FAILED,
            message=(
                "Форма оплаты банка заполнена, но автоматическое подтверждение платежа "
                "ещё не реализовано (требуется отдельное исследование реальной формы "
                "после отправки карты — см. reader/checkout/payment_gateway.py)."
            ),
            failure_reason=FailureReason.UNCONFIRMED_POST_SUBMIT_FLOW,
        )

    async def submit_confirmation_code(self, state: CheckoutState, code: str) -> BankGatewayResult:
        # code сюда не попадает ни в одно поле/лог — см. докстрок класса.
        raise BankGatewayError(
            "Ввод кода подтверждения не реализован: банковская сессия не доходит до "
            "OTP-этапа (см. reader/checkout/payment_gateway.py::PlaywrightBankGatewayClient.start)."
        )

    async def cancel(self, state: CheckoutState) -> None:
        # Ни одна browser-сессия не переживает start() (см. выше — всегда
        # закрывается перед возвратом) — здесь для симметрии протокола и на
        # случай будущей реализации, где сессия остаётся открытой между
        # start() и submit_confirmation_code().
        return None

    @staticmethod
    async def _capture_form_timeout_diagnostics(state: CheckoutState, page: BrowserPageLike) -> None:
        """Production-диагностика ТОЛЬКО для этапа ДО ввода карточных данных
        (см. задачу) — вызывается ИСКЛЮЧИТЕЛЬНО из ветки "Timeout" в start(),
        до которой ни разу не выполняется page.fill() с данными карты. Никогда
        не логирует/не сохраняет card number/CVV/OTP/значения .env — только
        метаданные страницы (url/title/frames/load state) и screenshot/HTML
        ДО ввода. Каждый шаг best-effort и независим от остальных: реальный
        Playwright Page умеет всё нижеперечисленное, но метод намеренно не
        требует этого от BrowserPageLike-протокола (используется getattr —
        см. задачу: "не меняй selectors/timeouts/state-machine", протокол
        тоже не расширяем формально ради этого)."""
        page_url = None
        try:
            page_url = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001, S110 — диагностика best-effort, отсутствие значения видно ниже в логе как None
            pass

        page_title = None
        title_fn = getattr(page, "title", None)
        if title_fn is not None:
            try:
                page_title = await title_fn()
            except Exception:  # noqa: BLE001, S110
                pass

        load_state = None
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is not None:
            try:
                load_state = await evaluate_fn("document.readyState")
            except Exception:  # noqa: BLE001, S110
                pass

        frame_urls: list[str] = []
        frames = getattr(page, "frames", None)
        if frames is not None:
            try:
                for frame in frames:
                    try:
                        frame_urls.append(str(frame.url))
                    except Exception:  # noqa: BLE001
                        frame_urls.append("<unavailable>")
            except Exception:  # noqa: BLE001, S110
                pass

        logger.warning(
            "Checkout %s: диагностика timeout банковской формы\n"
            "page_url=%s\npage_title=%s\nload_state=%s\nframes_count=%d\nframe_urls=%s",
            state.id, page_url, page_title, load_state, len(frame_urls), frame_urls,
        )

        try:
            _PAYMENT_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            logger.warning("Checkout %s: не удалось создать каталог диагностики %s", state.id, _PAYMENT_DEBUG_DIR)
            return

        screenshot_fn = getattr(page, "screenshot", None)
        if screenshot_fn is not None:
            screenshot_path = _PAYMENT_DEBUG_DIR / f"{state.id}-form-timeout.png"
            try:
                await screenshot_fn(path=str(screenshot_path))
                logger.warning("Checkout %s: screenshot диагностики сохранён: %s", state.id, screenshot_path)
            except Exception:  # noqa: BLE001
                logger.warning("Checkout %s: не удалось сохранить screenshot диагностики", state.id)

        content_fn = getattr(page, "content", None)
        if content_fn is not None:
            html_path = _PAYMENT_DEBUG_DIR / f"{state.id}-form-timeout.html"
            try:
                html = await content_fn()
                html_path.write_text(html, encoding="utf-8")
                logger.warning("Checkout %s: HTML диагностики сохранён: %s", state.id, html_path)
            except Exception:  # noqa: BLE001
                logger.warning("Checkout %s: не удалось сохранить HTML диагностики", state.id)

    @staticmethod
    async def _safe_close(page: BrowserPageLike | None) -> None:
        if page is None:
            return
        try:
            await page.close()
        except Exception:  # noqa: BLE001 — cleanup не должен маскировать исходную ошибку
            logger.warning("Не удалось закрыть браузерную страницу checkout (cleanup)")
