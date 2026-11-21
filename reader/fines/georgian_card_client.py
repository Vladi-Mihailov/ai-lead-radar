"""Сумма к оплате штрафа из платёжной страницы Georgian Card (см.
reader/fines/payable_amount.py и docs/georgia-payable-amount.md).

Повторяет ровно то, что делает браузер плательщика ДО ввода карты:

1. GET mpi.gc.ge/page1/?... — та же ссылка, что строит JavaScript police.ge
   для кнопки Visa/Mastercard (merch_id/page_id МВД — из config). Georgian
   Card создаёт транзакцию и спрашивает у МВД, можно ли оплатить и сколько.
2. Редиректы — только внутри хоста Georgian Card. Редирект на
   police.ge/protocol/fail.php означает отказ МВД (OUTCOME_REFUSED); туда
   запрос не отправляется. Любой другой внешний хост — ошибка.
3. Сумма — из страницы оплаты (кнопка «გადახდა (<сумма> ₾)») и/или из ответа
   POST /open/api//v4/{merchantId}/payment/{token}/start, который страница
   вызывает сама при загрузке (state "offer", amount, currency — без данных
   карты).

Чего этот модуль не делает никогда: не заполняет и не отправляет поля карты,
не вызывает оплату/авторизацию/подтверждение, не ходит на police.ge и на
любой хост, кроме Georgian Card. Это не соглашение, а проверка: каждый
запрос проходит через _guard_request — разрешены только GET к хосту
Georgian Card и единственный POST на .../start с пустым телом {}.

Ответ принимается, только если: МВД разрешило оплату (state "offer" или
страница с кнопкой оплаты), валюта GEL, квитанция названа в ответе (не в
наших же ссылках), сумма положительна и правдоподобна. Всё остальное —
исход без суммы (OUTCOME_*), который ничего не сохраняет и оплатой не
считается.

Формат страницы оплаты для police.ge подтверждён только для отказа (запись
2026-11-18); страница с суммой видена в другом сценарии (ОСАГО, reader/
checkout/payment_gateway.py). Поэтому любой незнакомый ответ — OUTCOME_
INVALID_RESPONSE с кодом в detail, а не догадка; формат сверяется пилотом на
одной квитанции (см. docs/georgia-payable-amount.md).

В логи и detail не попадают номер, квитанция, HTML, cookies и токены.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from reader.fines.payable_amount import (
    OUTCOME_BLOCKED,
    OUTCOME_CURRENCY_MISMATCH,
    OUTCOME_ERROR,
    OUTCOME_INVALID_RESPONSE,
    OUTCOME_OK,
    OUTCOME_RECEIPT_UNCONFIRMED,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
    round_amount,
)

logger = logging.getLogger(__name__)

POLICE_GE_SUCCESS_URL = "https://police.ge/protocol/success.php"
POLICE_GE_FAIL_URL = "https://police.ge/protocol/fail.php"
_POLICE_GE_HOSTS = frozenset({"police.ge", "www.police.ge"})

AMOUNT_UNIT_MAJOR = "major"  # GEL
AMOUNT_UNIT_MINOR = "minor"  # тетри (1/100 GEL)

_START_PATH_RE = re.compile(r"^/open/api//?v4/[A-Za-z0-9_-]{1,128}/payment/[A-Za-z0-9_-]{1,256}/start$")
# Где в странице/адресе может стоять пара merchantId/token для .../start.
_START_ENDPOINT_RE = re.compile(r"/open/api//?v4/([A-Za-z0-9_-]{1,128})/payment/([A-Za-z0-9_-]{1,256})/start")
_V4_PAYMENT_PATH_RE = re.compile(r"/v4/([A-Za-z0-9_-]{1,128})/payment/([A-Za-z0-9_-]{1,256})")
# Кнопка оплаты: «გადახდა (50.00 ₾)» / «გადახდა (1 250,00 ₾)».
_PAY_BUTTON_RE = re.compile(r"გადახდა\s*\(\s*([0-9][0-9\s., ]*)\s*₾\s*\)")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|[?&][\w.]+=\S*", re.IGNORECASE)
# Только буквы: значение state приходит от шлюза, цифры могли бы оказаться
# номером квитанции или госномера.
_STATE_SAFE_RE = re.compile(r"[^a-z_]")

_MAX_BODY_CHARS = 2_000_000
_START_BODY = b"{}"
# Правдоподобие суммы — защита от ошибки разбора/единиц (тетри вместо лари),
# а не расчёт начислений: сумма меньше половины исходной или больше
# 10×исходной + 1000 ₾ не сохраняется.
_MIN_RATIO = 0.5
_MAX_RATIO = 10.0
_MAX_EXTRA = 1000.0


class GeorgianCardSafetyError(RuntimeError):
    """Запрос не прошёл _guard_request — он не отправлен."""


@dataclass(frozen=True)
class GeorgianCardConfig:
    base_url: str = "https://mpi.gc.ge"
    merchant_id: str = ""
    page_id: str = ""
    amount_unit: str = AMOUNT_UNIT_MAJOR
    request_timeout_seconds: float = 20.0
    max_redirects: int = 6
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    @property
    def configured(self) -> bool:
        return bool(self.merchant_id and self.page_id)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""


@dataclass(frozen=True)
class AmountQuote:
    outcome: str
    amount: float | None = None
    currency: str | None = None
    # Короткий код причины без персональных данных (для журнала проверок).
    detail: str | None = None
    # False — ни одного запроса не отправлено (не настроено): сессии нет.
    request_sent: bool = True


class GeorgianCardClientLike(Protocol):
    @property
    def configured(self) -> bool: ...

    async def fetch_amount(self, *, car_number: str, protocol_no: str, base_amount: float | None) -> AmountQuote: ...


def build_page1_url(config: GeorgianCardConfig, *, car_number: str, protocol_no: str) -> str:
    """Та же ссылка, что у кнопки Visa/Mastercard на police.ge."""
    query = urlencode([
        ("lang_code", "ka"),
        ("page_id", config.page_id),
        ("merch_id", config.merchant_id),
        ("back_url_s", POLICE_GE_SUCCESS_URL),
        ("back_url_f", f"{POLICE_GE_FAIL_URL}?id={protocol_no}"),
        ("o.protocolId", protocol_no),
        ("o.vehicleNumber", car_number),
        ("o.resourceAlias", "1"),
    ])
    return f"{config.base_url.rstrip('/')}/page1/?{query}"


def _guard_request(method: str, url: str, *, gc_host: str, body: bytes | None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "") != gc_host:
        raise GeorgianCardSafetyError("host")
    if method == "GET":
        if body:
            raise GeorgianCardSafetyError("get_body")
        return
    if method == "POST" and _START_PATH_RE.match(parts.path) and not parts.query and body == _START_BODY:
        return
    raise GeorgianCardSafetyError("method_or_path")


def _classify_redirect(target: str, *, gc_host: str) -> str:
    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    if host in _POLICE_GE_HOSTS:
        path = parts.path.lower()
        if path.endswith("/fail.php"):
            return "refused"
        if path.endswith("/success.php"):
            return "success"
        return "foreign"
    if parts.scheme == "https" and host == gc_host:
        return "gc"
    return "foreign"


def _parse_number(text: str) -> float | None:
    cleaned = text.replace(" ", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _visible_text(html: str) -> str:
    """Текст страницы без скриптов, разметки и ссылок: квитанция в наших же
    параметрах (back_url_f, o.protocolId) подтверждением не считается."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return _URL_RE.sub(" ", text)


def _mentions_receipt(text: str, protocol_no: str) -> bool:
    pattern = rf"(?<![0-9A-Za-z]){re.escape(protocol_no)}(?![0-9A-Za-z])"
    return re.search(pattern, text) is not None


def _json_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _json_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _json_strings(nested)]
    return []


def _json_mentions_receipt(payload, protocol_no: str) -> bool:
    for text in _json_strings(payload):
        if "://" in text or "protocolId" in text or "id=" in text:
            continue
        if _mentions_receipt(text, protocol_no):
            return True
    return False


def _find_key(payload, names: tuple[str, ...]):
    """Первое значение по одному из ключей (без учёта регистра), в глубину."""
    if isinstance(payload, dict):
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for name in names:
            if name in lowered and not isinstance(lowered[name], (dict, list)):
                return lowered[name]
        for value in payload.values():
            found = _find_key(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_key(value, names)
            if found is not None:
                return found
    return None


def _extract_start_endpoint(final_url: str, html: str) -> tuple[str, str] | None:
    for source in (final_url, html):
        match = _START_ENDPOINT_RE.search(source) or _V4_PAYMENT_PATH_RE.search(source)
        if match:
            return match.group(1), match.group(2)
    return None


def _sanitize_state(value) -> str:
    return _STATE_SAFE_RE.sub("", str(value).lower())[:20] or "empty"


@dataclass(frozen=True)
class _ParsedStart:
    amount: float | None
    currency: str | None
    state: str | None
    mentions_receipt: bool


def _parse_start_payload(payload, *, protocol_no: str, amount_unit: str) -> _ParsedStart:
    raw_amount = _find_key(payload, ("amount", "totalamount", "total_amount", "sum"))
    amount = None
    if isinstance(raw_amount, bool):
        amount = None
    elif isinstance(raw_amount, (int, float)):
        amount = float(raw_amount)
    elif isinstance(raw_amount, str):
        amount = _parse_number(raw_amount)
    if amount is not None and amount_unit == AMOUNT_UNIT_MINOR:
        amount = amount / 100.0
    currency = _find_key(payload, ("currency", "currencycode", "currency_code", "ccy"))
    state = _find_key(payload, ("state", "status"))
    return _ParsedStart(
        amount=amount,
        currency=str(currency).strip().upper() if currency is not None else None,
        state=str(state).strip().lower() if state is not None else None,
        mentions_receipt=_json_mentions_receipt(payload, protocol_no),
    )


def _is_gel(currency: str | None) -> bool:
    return currency in ("GEL", "981")


def _plausible(amount: float, base_amount: float | None) -> bool:
    if amount <= 0:
        return False
    if base_amount is None or base_amount <= 0:
        return True
    return base_amount * _MIN_RATIO <= amount <= base_amount * _MAX_RATIO + _MAX_EXTRA


class GeorgianCardAmountClient:
    """Одна сессия на вызов fetch_amount: свой httpx-клиент (свои cookies),
    закрывается сразу после ответа. transport — для тестов (mock)."""

    def __init__(self, config: GeorgianCardConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self._config = config
        self._transport = transport

    @property
    def configured(self) -> bool:
        return self._config.configured

    async def fetch_amount(self, *, car_number: str, protocol_no: str, base_amount: float | None) -> AmountQuote:
        if not self._config.configured:
            return AmountQuote(OUTCOME_ERROR, detail="not_configured", request_sent=False)
        timeout = self._config.request_timeout_seconds
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                headers={"User-Agent": self._config.user_agent, "Accept-Language": "ka,en;q=0.8"},
            ) as client:
                return await asyncio.wait_for(
                    self._fetch(client, car_number=car_number, protocol_no=protocol_no, base_amount=base_amount),
                    timeout=timeout * 3,
                )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return AmountQuote(OUTCOME_TIMEOUT, detail="timeout")
        except GeorgianCardSafetyError as exc:
            logger.error("Georgian Card: запрос заблокирован проверкой безопасности (%s) — не отправлен", exc)
            return AmountQuote(OUTCOME_ERROR, detail=f"safety_{exc}")
        except httpx.HTTPError as exc:
            return AmountQuote(OUTCOME_ERROR, detail=f"transport_{type(exc).__name__}"[:60])

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, *, body: bytes | None = None):
        _guard_request(method, url, gc_host=self._config.host, body=body)
        headers = {"Content-Type": "application/json", "Accept": "application/json"} if method == "POST" else None
        return await client.request(method, url, content=body, headers=headers)

    async def _fetch(
        self, client: httpx.AsyncClient, *, car_number: str, protocol_no: str, base_amount: float | None,
    ) -> AmountQuote:
        gc_host = self._config.host
        response = await self._send(client, "GET", build_page1_url(
            self._config, car_number=car_number, protocol_no=protocol_no,
        ))
        for _ in range(self._config.max_redirects):
            if response.status_code in (403, 429):
                return AmountQuote(OUTCOME_BLOCKED, detail=f"http_{response.status_code}")
            if not response.is_redirect:
                break
            location = response.headers.get("location")
            if not location:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="redirect_without_location")
            target = urljoin(str(response.url), location)
            kind = _classify_redirect(target, gc_host=gc_host)
            if kind == "refused":
                return AmountQuote(OUTCOME_REFUSED, detail="fail_redirect")
            if kind == "success":
                # Без оплаты такого быть не может — ничего не сохраняем.
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="success_redirect")
            if kind == "foreign":
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="foreign_redirect")
            response = await self._send(client, "GET", target)
        else:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="too_many_redirects")

        if response.status_code in (403, 429):
            return AmountQuote(OUTCOME_BLOCKED, detail=f"http_{response.status_code}")
        if response.status_code >= 500:
            return AmountQuote(OUTCOME_ERROR, detail=f"http_{response.status_code}")
        if response.status_code != 200:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"http_{response.status_code}")

        html = response.text[:_MAX_BODY_CHARS]
        visible = _visible_text(html)
        html_amount = None
        button = _PAY_BUTTON_RE.search(visible)
        if button:
            html_amount = _parse_number(button.group(1))

        start: _ParsedStart | None = None
        endpoint = _extract_start_endpoint(str(response.url), html)
        if endpoint is not None:
            merchant, token = endpoint
            start_url = f"{self._config.base_url.rstrip('/')}/open/api//v4/{merchant}/payment/{token}/start"
            start_response = await self._send(client, "POST", start_url, body=_START_BODY)
            if start_response.status_code in (403, 429):
                return AmountQuote(OUTCOME_BLOCKED, detail=f"start_http_{start_response.status_code}")
            if start_response.status_code != 200:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"start_http_{start_response.status_code}")
            try:
                payload = start_response.json()
            except ValueError:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="start_not_json")
            start = _parse_start_payload(payload, protocol_no=protocol_no, amount_unit=self._config.amount_unit)
            if start.state is not None and start.state != "offer":
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"state_{_sanitize_state(start.state)}")

        if start is None and html_amount is None:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="amount_not_found")

        amount = start.amount if start is not None and start.amount is not None else html_amount
        if amount is None:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="amount_not_found")
        if start is not None and start.amount is not None and html_amount is not None:
            if abs(start.amount - html_amount) >= 0.005:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="amount_mismatch")

        if start is not None and start.amount is not None:
            if start.currency is None or not _is_gel(start.currency):
                return AmountQuote(OUTCOME_CURRENCY_MISMATCH, detail="currency")
            if start.state is None:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="state_missing")
        # Иначе сумма только из кнопки «გადახდა (N ₾)» — знак ₾ и есть валюта.

        receipt_confirmed = (start is not None and start.mentions_receipt) or _mentions_receipt(visible, protocol_no)
        if not receipt_confirmed:
            return AmountQuote(OUTCOME_RECEIPT_UNCONFIRMED, detail="receipt_not_in_response")
        if not _plausible(amount, base_amount):
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="implausible_amount")
        return AmountQuote(OUTCOME_OK, amount=round_amount(amount), currency="GEL")


def describe_response_shape(payload) -> str:
    """Ключи JSON без значений — для пилота (сверить формат ответа), без
    персональных данных."""
    if isinstance(payload, dict):
        return "{" + ",".join(sorted(str(key) for key in payload))[:300] + "}"
    return type(payload).__name__

