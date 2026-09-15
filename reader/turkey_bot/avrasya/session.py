"""HTTP-транспорт avrasyatuneli.com (Avrasya Tüneli, ihlalli geçiş —
"нарушенный проезд"/неоплаченный проезд по платному тоннелю) — см. design
report Stage 1 (research-only): реальный флоу и все URL/поля подтверждены
живым исследованием (curl + чтение реального JS-бандла
/_assets/js/site/gecis_ihlali.js?p=v3), не догадка.

Полностью независимо от reader/turkey_bot/gib/* (см. задачу: "Avrasya
must be a separate provider implementation... do not put Avrasya
transport/parser logic inside gib/") и от reader/fines/* — ничего оттуда
не импортируется.

Ключевое архитектурное отличие от GibSession (см. design report Stage 1,
раздел "CAPTCHA endpoint and lifecycle"): у Avrasya CAPTCHA не имеет
собственного id — ожидаемый код живёт в серверной сессии
(ASP.NET_SessionId cookie) под фиксированным ключом uid=DebtQueryByPlate.
Один и тот же httpx.AsyncClient (с его persistent cookie jar) на ВЕСЬ
цикл одной проверки (start -> refresh_captcha? -> submit) обязателен —
как и у GIB, но submit() здесь НЕ принимает никакого image_id вовсе,
потому что передавать нечего.

Второе отличие (см. design report Stage 1, раздел "Confirmed response
states"): GIB заворачивает ЛЮБОЙ бизнес-исход в единый envelope
("messages") независимо от HTTP-статуса, поэтому GibSession игнорирует
сам HTTP-статус. У Avrasya HTTP-статус САМ ПО СЕБЕ несёт часть смысла
(подтверждено вживую: 400 = Captcha-ошибка, 429 = rate limit) — поэтому
submit()/refresh_captcha() здесь возвращают статус вызывающему коду
(parser.py), а не игнорируют его.

Третье — реально измеренное вживую (curl, design report Stage 1)
ограничение "API calls quota exceeded! maximum admitted 1 per Second." —
_MIN_REQUEST_INTERVAL_SECONDS ниже применяется КОНСЕРВАТИВНО (с запасом
над увиденным лимитом в 1 запрос/сек) перед КАЖДЫМ запросом на этой
сессии (включая самый первый — намеренно, чтобы одна и та же защита
работала одинаково для одиночного ручного теста и для будущей
последовательности запросов в рамках одного диалога, см. Stage 2B)."""

import asyncio
import logging
import time

import httpx

from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge

logger = logging.getLogger(__name__)

_PAGE_URL = "https://www.avrasyatuneli.com/ihlalli-gecis-odemesi/"
_CAPTCHA_URL = "https://www.avrasyatuneli.com/Captcha.ashx?uid=DebtQueryByPlate"
_QUERY_URL = "https://www.avrasyatuneli.com/api/debt/query"

# Referer подтверждён исследованием как то, что реально шлёт браузер (см.
# design report Stage 1) — не изолированно доказано как строго
# обязательный (WAF мог бы пропустить и без него), но сознательно
# сохраняется, чтобы не отличаться от настоящего браузера без причины
# (см. design report: "recommend keeping it... rather than testing its
# removal against production").
_COMMON_HEADERS = {
    "Referer": _PAGE_URL,
}

# lang/Authorization — РЕАЛЬНО отправляемые сайтом заголовки (см. design
# report Stage 1, controller.searchDebt в gecis_ihlali.js). "Bearer null" —
# буквальная строка, которую шлёт неавторизованный браузер
# (localStorage.getItem("access_token") тогда возвращает null,
# JS-конкатенация даёт именно "Bearer null") — подтверждено вживую, что
# анонимный запрос с этим значением проходит до бизнес-логики (получена
# Captcha-ошибка, а не ошибка авторизации).
_QUERY_HEADERS = {
    **_COMMON_HEADERS,
    "Content-Type": "application/json",
    "lang": "tr",
    "Authorization": "Bearer null",
}

# Консервативно больше увиденного вживую лимита ("maximum admitted 1 per
# Second", см. design report Stage 1) — запас на сетевую задержку/джиттер,
# не точная граница.
_MIN_REQUEST_INTERVAL_SECONDS = 1.5


class AvrasyaTransportError(Exception):
    """Сбой транспорта (сеть/DNS/таймаут, ответ без ожидаемого
    Content-Type/тела) — НЕ то же самое, что бизнес-исход (rejected/
    no_debt/has_debt/unexpected, см. reader/turkey_bot/avrasya/models.py)
    — те разбирает parser.py из валидного (status_code, body), сюда не
    попадают."""


class AvrasyaRateLimitedError(AvrasyaTransportError):
    """HTTP 429 — реально увиденный вживую (см. design report Stage 1)
    ответ "API calls quota exceeded! maximum admitted 1 per Second."
    (обычный текст, НЕ JSON). Это транспортное/операционное условие, а не
    бизнес-исход (см. задачу: "429 remains a transport/rate-limit error"):
    parser.py никогда не видит и не классифицирует такой ответ."""


class AvrasyaSession:
    """Один экземпляр — один httpx.AsyncClient — один цикл проверки
    одного номера (см. design report: тот же принцип, что и GibSession —
    не переиспользуется между разными пользователями/проверками).
    Вызывающий код (manual_test.py, позже — Stage 2B conversation.py)
    отвечает за создание и закрытие httpx.AsyncClient."""

    def __init__(self, client: httpx.AsyncClient, *, request_timeout: float = 30.0):
        self._client = client
        self._timeout = request_timeout
        self._last_request_at: float | None = None

    async def _throttle(self) -> None:
        """См. докстрок модуля про _MIN_REQUEST_INTERVAL_SECONDS —
        применяется перед КАЖДЫМ запросом этой сессии, включая первый."""
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    async def start(self) -> AvrasyaCaptchaChallenge:
        """Полный цикл установки сессии: сначала GET самой страницы (те же
        cookies, что видит реальный браузер — ASP.NET_SessionId, см.
        design report Stage 1), затем CAPTCHA в рамках той же сессии."""
        await self._throttle()
        try:
            page_response = await self._client.get(
                _PAGE_URL, headers=_COMMON_HEADERS, timeout=self._timeout,
            )
            page_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AvrasyaTransportError("failed to load Avrasya page") from exc

        return await self.refresh_captcha()

    async def refresh_captcha(self) -> AvrasyaCaptchaChallenge:
        """Новая CAPTCHA в рамках УЖЕ существующей сессии (те же cookies).

        ВАЖНО (см. design report Stage 1, подтверждено вживую): КАЖДЫЙ GET
        сюда молча делает недействительным код, ожидавшийся для
        предыдущего fetch этого же uid — вызывающий код обязан показать
        человеку ИМЕННО картинку из этого вызова и не запрашивать её
        повторно перед submit() того же попытки."""
        await self._throttle()
        try:
            response = await self._client.get(
                _CAPTCHA_URL, headers=_COMMON_HEADERS, timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AvrasyaTransportError("failed to request Avrasya captcha") from exc

        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            raise AvrasyaTransportError(
                f"Avrasya captcha response has unexpected content-type: {content_type!r}"
            )

        return AvrasyaCaptchaChallenge(image_png=response.content)

    async def submit(self, *, plate: str, captcha_code: str) -> tuple[int, object]:
        """POST /api/debt/query. Возвращает (status_code, body) СЫРЫМИ —
        интерпретацию (rejected/no_debt/has_debt/unexpected) делает
        ИСКЛЮЧИТЕЛЬНО reader/turkey_bot/avrasya/parser.py (см. design
        report: "не гадать схему здесь", session.py — только транспорт).

        body — распарсенный JSON, если Content-Type это позволяет,
        ИНАЧЕ — исходный текст ответа (см. design report Stage 1: HTTP 429
        приходит обычным текстом, не JSON — это НЕ повод считать ответ
        нечитаемым/транспортной ошибкой, только 429 сам по себе
        обрабатывается отдельно, см. ниже).

        HTTP 429 поднимается как AvrasyaRateLimitedError ДО возврата —
        вызывающий код (provider.py) не должен и не может передать его в
        parser.py как обычный бизнес-ответ (см. models.py про то, почему
        rate-limit не входит в AvrasyaSubmitKind).

        НИКОГДА не логирует captcha_code и не логирует cookies/заголовки
        запроса целиком (см. задачу) — исключения ниже содержат только
        тип сбоя, не тело запроса/ответа."""
        body = {
            "queryType": "ByPlate",
            "queryValue": plate,
            "queryValue2": "",
            "queryValue3": "",
            "captcha": captcha_code,
        }
        await self._throttle()
        try:
            response = await self._client.post(
                _QUERY_URL, json=body, headers=_QUERY_HEADERS, timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("Avrasya submit: transport error (%s)", type(exc).__name__)
            raise AvrasyaTransportError("failed to submit Avrasya check") from exc

        if response.status_code == 429:
            logger.warning("Avrasya submit: rate limited (429)")
            raise AvrasyaRateLimitedError("Avrasya API rate limit exceeded")

        try:
            response_body: object = response.json()
        except ValueError:
            response_body = response.text

        return response.status_code, response_body
