"""Сумма к оплате штрафа из платёжной страницы Georgian Card (см.
reader/fines/payable_amount.py и docs/georgia-payable-amount.md).

Страница оплаты mpi.gc.ge/page1/ — JavaScript-приложение (статическая
оболочка + settings.js + бандл). Сумму оно получает само, двумя вызовами
своего API (разобрано по коду приложения 2026-11-21):

1. POST {openApi.baseUrl}/{version}/{portal}/token -> {"token": ...};
2. POST .../payment/{token}/start с параметрами страницы: merchantId
   (merch_id МВД), back_url_s/back_url_f, lang, "3ds2.supported",
   "params.protocolId"/"params.vehicleNumber"/"params.resourceAlias" (из
   o.* ссылки police.ge), returnUrl. На этом шаге Georgian Card спрашивает
   МВД (check), и ответ — state "offer" с amount (в тетри: приложение
   показывает amount/100) и currency, либо отказ.

Оба вызова приложение шлёт формой (перехватчик axios: Wa.stringify(data),
Content-Type application/x-www-form-urlencoded; charset=UTF-8) с заголовком
X-Requested-With: XMLHttpRequest; /token — без тела.

Клиент повторяет ровно шаги браузера до формы карты: GET page1 с той же
ссылкой, что строит police.ge (merch_id/page_id МВД — из config), GET
settings.js (адрес API и portal), POST /token, POST /start. Карта не
вводится, оплата, авторизация, подтверждение, отклонение (accept/decline/
3ds/googlepay/...) не вызываются, police.ge и любые другие хосты не
запрашиваются. Это не соглашение, а проверка: каждый запрос проходит через
_guard_request — разрешены только GET к хосту Georgian Card, POST /token
без тела и POST /start с телом только из разрешённых полей.

Ответ принимается, только если: state "offer", валюта GEL (GEL/981),
квитанция названа в самом ответе (не в наших же ссылках), сумма
положительна и правдоподобна. Всё остальное — исход без суммы (OUTCOME_*),
который ничего не сохраняет и оплатой не считается. Ограничения шлюза
(Cloudflare, WAF, 403/429) не обходятся: такой ответ — OUTCOME_BLOCKED.

AmountQuote.trace — обезличенный ход сессии (шаги, коды, имена полей ответа,
state/currency/amount) для пилота; печатает его только операторский скрипт.
В логи и detail не попадают номер, квитанция, HTML, cookies и токены.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

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

# Единственный хост, куда клиент вообще может отправить запрос (в том числе
# при опечатке в payable_amount_gc_base_url).
ALLOWED_GC_HOSTS = frozenset({"mpi.gc.ge"})

POLICE_GE_SUCCESS_URL = "https://police.ge/protocol/success.php"
POLICE_GE_FAIL_URL = "https://police.ge/protocol/fail.php"
_POLICE_GE_HOSTS = frozenset({"police.ge", "www.police.ge"})

AMOUNT_UNIT_MAJOR = "major"  # GEL
AMOUNT_UNIT_MINOR = "minor"  # тетри (1/100 GEL) — так отвечает API страницы

_API_ID = r"[A-Za-z0-9_-]{1,128}"
_TOKEN_PATH_RE = re.compile(rf"^/open/api//?v[0-9]{{1,2}}/{_API_ID}/token$")
_START_PATH_RE = re.compile(rf"^/open/api//?v[0-9]{{1,2}}/{_API_ID}/payment/[A-Za-z0-9_-]{{8,256}}/start$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
# Поля тела /start — ровно те, что собирает страница до формы карты.
_START_BODY_KEYS = frozenset({
    "merchantId", "state.redirect", "state.in_progress", "back_url_s", "back_url_f", "lang", "3ds2.supported",
    "params.protocolId", "params.vehicleNumber", "params.resourceAlias", "returnUrl",
})
_SETTINGS_BASE_RE = re.compile(r"openApi\s*:\s*\{[^}]*?baseUrl\s*:\s*[\"']([^\"']+)[\"']", re.DOTALL)
_SETTINGS_VERSION_RE = re.compile(r"openApi\s*:\s*\{[^}]*?version\s*:\s*[\"'](v[0-9]{1,2})[\"']", re.DOTALL)
_SETTINGS_PORTAL_RE = re.compile(rf"openApi\s*:\s*\{{[^}}]*?portal\s*:\s*[\"']({_API_ID})[\"']", re.DOTALL)
_WAF_MARKERS = ("The URL you requested has been blocked", "Attack ID")
_URL_RE = re.compile(r"https?://\S+|[?&][\w.]+=\S*", re.IGNORECASE)
# Только буквы: коды приходят от шлюза, цифры могли бы оказаться номером
# квитанции или госномера.
_STATE_SAFE_RE = re.compile(r"[^a-z_]")
_ERROR_SAFE_RE = re.compile(r"[^A-Za-z_]")

_GC_LOG_MARKER = "gc.ge"
_GC_URL_RE = re.compile(r"(https?://[^/\s\"']*gc\.ge)[^\s\"']*", re.IGNORECASE)


class _RedactGeorgianCardUrlFilter(logging.Filter):
    """httpx пишет на INFO "HTTP Request: GET <полный URL>": в ссылке page1 —
    госномер и квитанция, в адресах Georgian Card — токен сессии. От URL
    Georgian Card в записи остаётся только хост."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(_redact_gc_url(value) for value in record.args)
            if isinstance(record.msg, str) and _GC_LOG_MARKER in record.msg:
                record.msg = _GC_URL_RE.sub(r"\1/<redacted>", record.msg)
        except Exception:
            return True
        return True


def _redact_gc_url(value: object) -> object:
    text = str(value)
    if _GC_LOG_MARKER not in text:
        return value
    return _GC_URL_RE.sub(r"\1/<redacted>", text)


def _install_gc_url_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactGeorgianCardUrlFilter) for f in target.filters):
            target.addFilter(_RedactGeorgianCardUrlFilter())


_install_gc_url_log_redaction()

_MAX_BODY_CHARS = 2_000_000
# Правдоподобие суммы — защита от ошибки разбора/единиц (тетри вместо лари),
# а не расчёт начислений: сумма меньше половины исходной или больше
# 6×исходной + 300 ₾ не сохраняется (тетри, прочитанные как лари, — это
# 100×исходной: отсекается для любой квитанции дороже ~3 ₾).
_MIN_RATIO = 0.5
_MAX_RATIO = 6.0
_MAX_EXTRA = 300.0


class GeorgianCardSafetyError(RuntimeError):
    """Запрос не прошёл _guard_request — он не отправлен."""


@dataclass(frozen=True)
class GeorgianCardConfig:
    base_url: str = "https://mpi.gc.ge"
    merchant_id: str = ""
    page_id: str = ""
    amount_unit: str = AMOUNT_UNIT_MINOR
    request_timeout_seconds: float = 20.0
    max_redirects: int = 6
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    @property
    def configured(self) -> bool:
        parts = urlsplit(self.base_url)
        return bool(
            self.merchant_id and self.page_id and parts.scheme == "https" and parts.port is None
            and (parts.hostname or "") in ALLOWED_GC_HOSTS
        )

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
    # Обезличенный ход сессии для пилота (см. модульный докстрок).
    trace: tuple[str, ...] = ()


class GeorgianCardClientLike(Protocol):
    @property
    def configured(self) -> bool: ...

    async def fetch_amount(self, *, car_number: str, protocol_no: str, base_amount: float | None) -> AmountQuote: ...


def _q(value: str) -> str:
    return quote(value, safe="")


def build_page1_url(config: GeorgianCardConfig, *, car_number: str, protocol_no: str) -> str:
    """Та же ссылка, что строит payment(protocolNo, protocolAuto) на police.ge
    (простая склейка строк; браузер кодирует только не-ASCII символы)."""
    return (
        f"{config.base_url.rstrip('/')}/page1/?lang_code=ka"
        f"&page_id={_q(config.page_id)}"
        f"&merch_id={_q(config.merchant_id)}"
        f"&back_url_s={POLICE_GE_SUCCESS_URL}"
        f"&back_url_f={POLICE_GE_FAIL_URL}?id={_q(protocol_no)}"
        f"&o.protocolId={_q(protocol_no)}"
        f"&o.vehicleNumber={_q(car_number)}"
        "&o.resourceAlias=1"
    )


def build_start_body(config: GeorgianCardConfig, *, car_number: str, protocol_no: str, return_url: str) -> dict:
    """Тело /start — как его собирает страница (dT() + returnUrl)."""
    return {
        "merchantId": config.merchant_id,
        "state.redirect": "post_params",
        "state.in_progress": "no",
        "back_url_s": POLICE_GE_SUCCESS_URL,
        "back_url_f": f"{POLICE_GE_FAIL_URL}?id={protocol_no}",
        "lang": "ka",
        "3ds2.supported": True,
        "params.protocolId": protocol_no,
        "params.vehicleNumber": car_number,
        "params.resourceAlias": "1",
        "returnUrl": return_url,
    }


def _form_value(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def encode_form(fields: dict) -> str:
    """Как qs.stringify у страницы: ключ=значение через &, всё, кроме
    -_.~ и латиницы с цифрами, — в %XX (UTF-8)."""
    return "&".join(
        f"{quote(str(key), safe='-_.~')}={quote(_form_value(value), safe='-_.~')}" for key, value in fields.items()
    )


def _guard_request(method: str, url: str, *, gc_host: str, json_body: dict | None) -> None:
    parts = urlsplit(url)
    if gc_host not in ALLOWED_GC_HOSTS:
        raise GeorgianCardSafetyError("config_host")
    if parts.scheme != "https" or (parts.hostname or "") != gc_host or parts.port not in (None, 443):
        raise GeorgianCardSafetyError("host")
    if method == "GET":
        if json_body is not None:
            raise GeorgianCardSafetyError("get_body")
        return
    if method != "POST" or parts.query:
        raise GeorgianCardSafetyError("method_or_path")
    if _TOKEN_PATH_RE.match(parts.path):
        if json_body is not None:
            raise GeorgianCardSafetyError("token_body")
        return
    if _START_PATH_RE.match(parts.path):
        if not isinstance(json_body, dict) or not set(json_body) <= _START_BODY_KEYS:
            raise GeorgianCardSafetyError("start_body")
        if not all(isinstance(value, (str, bool)) for value in json_body.values()):
            raise GeorgianCardSafetyError("start_body_value")
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
    """Квитанция названа в значении ответа — не в ссылке (back_url_f,
    returnUrl — наши же параметры)."""
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


def _sanitize_state(value) -> str:
    return _STATE_SAFE_RE.sub("", str(value).lower())[:20] or "empty"


def _sanitize_error(value) -> str:
    return _ERROR_SAFE_RE.sub("", str(value))[:40] or "empty"


def _key_shape(payload, prefix: str = "", depth: int = 0) -> list[str]:
    """Имена полей ответа (без значений) — для пилота."""
    if not isinstance(payload, dict) or depth > 2:
        return []
    keys = []
    for key, value in payload.items():
        name = f"{prefix}{key}"
        keys.append(name)
        keys.extend(_key_shape(value, f"{name}.", depth + 1))
    return keys[:60]


def _is_gel(currency: str | None) -> bool:
    return currency in ("GEL", "981")


def _plausible(amount: float, base_amount: float | None) -> bool:
    if amount <= 0:
        return False
    if base_amount is None or base_amount <= 0:
        return True
    return base_amount * _MIN_RATIO <= amount <= base_amount * _MAX_RATIO + _MAX_EXTRA


def _amount_from(raw, unit: str) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        amount = float(raw)
    elif isinstance(raw, str):
        amount = _parse_number(raw)
    else:
        return None
    if amount is not None and unit == AMOUNT_UNIT_MINOR:
        amount = amount / 100.0
    return amount


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
        trace: list[str] = []
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                headers={"User-Agent": self._config.user_agent, "Accept-Language": "ka,en;q=0.8"},
            ) as client:
                quote_ = await asyncio.wait_for(
                    self._fetch(client, trace, car_number=car_number, protocol_no=protocol_no,
                                base_amount=base_amount),
                    timeout=timeout * 4,
                )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            quote_ = AmountQuote(OUTCOME_TIMEOUT, detail="timeout")
        except GeorgianCardSafetyError as exc:
            logger.error("Georgian Card: запрос заблокирован проверкой безопасности (%s) — не отправлен", exc)
            quote_ = AmountQuote(OUTCOME_ERROR, detail=f"safety_{exc}")
        except httpx.HTTPError as exc:
            quote_ = AmountQuote(OUTCOME_ERROR, detail=f"transport_{type(exc).__name__}"[:60])
        return AmountQuote(
            quote_.outcome, amount=quote_.amount, currency=quote_.currency, detail=quote_.detail,
            request_sent=quote_.request_sent, trace=tuple(trace),
        )

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, *, json_body: dict | None = None,
                    referer: str | None = None):
        """json_body — поля тела; уходят формой, как у страницы (см. модульный
        докстрок). Проверка _guard_request — по этим же полям, до отправки."""
        _guard_request(method, url, gc_host=self._config.host, json_body=json_body)
        if method != "POST":
            return await client.request(method, url)
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": self._config.base_url.rstrip("/"),
        }
        if referer:
            headers["Referer"] = referer
        content = encode_form(json_body).encode("utf-8") if json_body is not None else None
        return await client.request(method, url, content=content, headers=headers)

    async def _fetch(
        self, client: httpx.AsyncClient, trace: list[str], *, car_number: str, protocol_no: str,
        base_amount: float | None,
    ) -> AmountQuote:
        gc_host = self._config.host
        page_url = build_page1_url(self._config, car_number=car_number, protocol_no=protocol_no)

        # 1. Страница — как у браузера (оболочка приложения, без транзакции).
        response = await self._send(client, "GET", page_url)
        for hop in range(self._config.max_redirects + 1):
            if response.status_code in (403, 429):
                trace.append(f"page1 http {response.status_code}")
                return AmountQuote(OUTCOME_BLOCKED, detail=f"http_{response.status_code}")
            if not response.is_redirect:
                break
            if hop == self._config.max_redirects:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="too_many_redirects")
            location = response.headers.get("location")
            if not location:
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="redirect_without_location")
            target = urljoin(str(response.url), location)
            kind = _classify_redirect(target, gc_host=gc_host)
            trace.append(f"page1 redirect -> {kind}")
            if kind == "refused":
                return AmountQuote(OUTCOME_REFUSED, detail="fail_redirect")
            if kind == "success":
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="success_redirect")
            if kind == "foreign":
                return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="foreign_redirect")
            response = await self._send(client, "GET", target)
        html = response.text[:_MAX_BODY_CHARS]
        trace.append(f"page1 http {response.status_code}, {len(html)} chars")
        if any(marker in html for marker in _WAF_MARKERS):
            return AmountQuote(OUTCOME_BLOCKED, detail="waf_block")
        if response.status_code >= 500:
            return AmountQuote(OUTCOME_ERROR, detail=f"http_{response.status_code}")
        if response.status_code != 200:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"http_{response.status_code}")

        # 2. Адрес API и portal — из settings.js, который загружает страница.
        settings_response = await self._send(client, "GET", f"{self._config.base_url.rstrip('/')}/page1/settings.js")
        if settings_response.status_code in (403, 429):
            return AmountQuote(OUTCOME_BLOCKED, detail=f"settings_http_{settings_response.status_code}")
        settings_text = settings_response.text[:200_000]
        base = _SETTINGS_BASE_RE.search(settings_text)
        version = _SETTINGS_VERSION_RE.search(settings_text)
        portal = _SETTINGS_PORTAL_RE.search(settings_text)
        trace.append(f"settings http {settings_response.status_code}, api {'found' if base and version and portal else 'not found'}")
        if settings_response.status_code != 200 or not (base and version and portal):
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="settings_unrecognized")
        api_base = f"{base.group(1)}/{version.group(1)}/{portal.group(1)}"
        if (urlsplit(api_base).hostname or "") != gc_host:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="settings_foreign_api")

        # 3. Токен платёжной сессии.
        token_response = await self._send(client, "POST", f"{api_base}/token", referer=page_url)
        if token_response.status_code in (403, 429):
            trace.append(f"token http {token_response.status_code}")
            return AmountQuote(OUTCOME_BLOCKED, detail=f"token_http_{token_response.status_code}")
        try:
            token_payload = token_response.json()
        except ValueError:
            token_payload = None
        trace.append(f"token http {token_response.status_code}, keys {_key_shape(token_payload)}")
        if token_response.status_code != 200 or not isinstance(token_payload, dict):
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"token_http_{token_response.status_code}")
        token = token_payload.get("token")
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="token_missing")

        # 4. Старт: здесь Georgian Card спрашивает МВД, можно ли оплатить и сколько.
        body = build_start_body(
            self._config, car_number=car_number, protocol_no=protocol_no, return_url=f"{page_url}#{token}",
        )
        start_response = await self._send(
            client, "POST", f"{api_base}/payment/{token}/start", json_body=body, referer=page_url,
        )
        if start_response.status_code in (403, 429):
            trace.append(f"start http {start_response.status_code}")
            return AmountQuote(OUTCOME_BLOCKED, detail=f"start_http_{start_response.status_code}")
        try:
            payload = start_response.json()
        except ValueError:
            trace.append(f"start http {start_response.status_code}, not json")
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="start_not_json")
        state = _find_key(payload, ("state",))
        raw_amount = _find_key(payload, ("amount",))
        currency = _find_key(payload, ("currency",))
        currency = str(currency).strip().upper() if currency is not None else None
        amount = _amount_from(raw_amount, self._config.amount_unit)
        receipt_in_answer = _json_mentions_receipt(payload, protocol_no)
        trace.append(
            f"start http {start_response.status_code}, keys {_key_shape(payload)}, "
            f"state {_sanitize_state(state) if state is not None else '-'}, "
            f"amount {raw_amount if isinstance(raw_amount, (int, float)) and not isinstance(raw_amount, bool) else '-'}, "
            f"currency {currency if currency and re.fullmatch(r'[A-Z0-9]{3}', currency) else '-'}, receipt in answer {'yes' if receipt_in_answer else 'no'}"
        )
        if start_response.status_code != 200:
            error = _find_key(payload, ("error", "code"))
            return AmountQuote(
                OUTCOME_INVALID_RESPONSE,
                detail=f"start_http_{start_response.status_code}_{_sanitize_error(error) if error else 'noerror'}"[:60],
            )
        if state is None:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="state_missing")
        state_text = str(state).strip().lower()
        if state_text == "redirect":
            url = _find_key(payload, ("url",))
            if isinstance(url, str) and _classify_redirect(url, gc_host=gc_host) == "refused":
                return AmountQuote(OUTCOME_REFUSED, detail="state_redirect_fail")
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="state_redirect")
        if state_text == "result":
            # Без карты оплаты быть не может: это отказ МВД/шлюза, не оплата.
            return AmountQuote(OUTCOME_REFUSED, detail="state_result")
        if state_text != "offer":
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail=f"state_{_sanitize_state(state_text)}")
        if amount is None:
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="amount_not_found")
        if currency is None or not _is_gel(currency):
            return AmountQuote(OUTCOME_CURRENCY_MISMATCH, detail="currency")
        if not receipt_in_answer:
            return AmountQuote(OUTCOME_RECEIPT_UNCONFIRMED, detail="receipt_not_in_response")
        if not _plausible(amount, base_amount):
            return AmountQuote(OUTCOME_INVALID_RESPONSE, detail="implausible_amount")
        return AmountQuote(OUTCOME_OK, amount=round_amount(amount), currency="GEL")


def describe_response_shape(payload) -> str:
    """Ключи JSON без значений — для пилота, без персональных данных."""
    return "{" + ",".join(_key_shape(payload))[:300] + "}"
