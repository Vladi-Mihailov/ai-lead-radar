"""HTTP-транспорт dijital.gib.gov.tr (GIB) — аналог
reader/fines/police_ge_session.py, но с CAPTCHA вместо csrf_token.

Ключевое отличие от police.ge (см. design report Stage 1, живое
исследование): cookie 'TS...' (bot-mitigation, F5-подобная) РОТИРУЕТСЯ на
КАЖДЫЙ ответ апигейта — новое значение приходит в Set-Cookie каждый раз.
Один и тот же httpx.AsyncClient на весь цикл ОДНОЙ проверки (start ->
refresh_captcha? -> submit) обязателен: его persistent cookie jar сам
подхватывает новое значение и посылает его в следующем запросе — вручную
эту ротацию нигде переносить не нужно и не следует (см. GibSession.__init__
докстрок).

Полностью независимо от reader/fines/* — ничего оттуда не импортируется,
Турция не переиспользует грузинский провайдер/сессию (см. design report:
"новый отдельный процесс/приложение").

URL'ы и форма запроса подтверждены живым исследованием (см. design report
Stage 1): базовый путь апигейта вычитан из реального JS-бандла фронтенда
(https://dijital.gib.gov.tr/apigateway/ + relative url), сама CAPTCHA
получена и сохранена локально как доказательство. Тело with-mys-borc-list —
из проверочного payload, предоставленного заказчиком, и совпадает с тем,
что реально строит фронтенд (см. design report).
"""

import base64
import logging

import httpx

from reader.turkey_bot_test.gib.models import CaptchaChallenge

logger = logging.getLogger(__name__)

_PAGE_URL = "https://dijital.gib.gov.tr/hizliOdemeler/yabanciAracOdemeleri"
_APIGATEWAY_BASE = "https://dijital.gib.gov.tr/apigateway/"
_CAPTCHA_URL = _APIGATEWAY_BASE + "captcha/getnewcaptcha"
_SUBMIT_URL = _APIGATEWAY_BASE + "payment/verification/with-mys-borc-list"

_API_HEADERS = {
    "Accept-Language": "tr-TR",
    "Accept": "application/json, text/plain, */*",
}


class GibTransportError(Exception):
    """Сбой транспорта (сеть/DNS/таймаут, либо ответ, который в принципе
    невозможно разобрать как JSON) — НЕ то же самое, что "CAPTCHA
    отклонена"/"штрафов нет" и т.п. (см. reader/turkey_bot_test/gib/models.py::
    GibSubmitKind) — те бизнес-исходы разбирает parser.py из валидного
    JSON-ответа, сюда не попадают."""


def _decode_captcha_payload(payload: object) -> tuple[str, bytes]:
    if not isinstance(payload, dict):
        raise GibTransportError("GIB captcha response is not a JSON object")

    image_b64 = payload.get("captchaImgBase64")
    image_id = payload.get("cid")
    if not image_b64 or not isinstance(image_b64, str):
        raise GibTransportError("GIB captcha response missing captchaImgBase64")
    if not image_id or not isinstance(image_id, str):
        raise GibTransportError("GIB captcha response missing cid")

    try:
        image_png = base64.b64decode(image_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise GibTransportError("GIB captcha image is not valid base64") from exc

    return image_id, image_png


class GibSession:
    """Один экземпляр — один httpx.AsyncClient — один цикл проверки одного
    номера (см. design report: не переиспользуется между разными
    пользователями/проверками, в отличие от долгоживущего
    PoliceGeSession). Вызывающий код (manual_test.py, позже — Stage 3
    conversation.py) отвечает за создание и закрытие httpx.AsyncClient."""

    def __init__(self, client: httpx.AsyncClient, *, request_timeout: float = 30.0):
        self._client = client
        self._timeout = request_timeout

    async def start(self) -> CaptchaChallenge:
        """Полный цикл установки сессии: сначала GET самой страницы (те же
        cookies, что видит реальный браузер, см. design report Stage 1 —
        подтверждено, что это НЕ строго обязательно для самого API, но
        соответствует тому, что делает браузер, и снижает риск более
        строгой bot-mitigation в будущем), затем CAPTCHA в рамках той же
        сессии."""
        try:
            page_response = await self._client.get(_PAGE_URL, timeout=self._timeout)
            page_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GibTransportError("failed to load GIB page") from exc

        return await self.refresh_captcha()

    async def refresh_captcha(self) -> CaptchaChallenge:
        """Новая CAPTCHA в рамках УЖЕ существующей сессии (те же cookies) —
        вызывается и из start(), и повторно после submit() с исходом
        "rejected" (см. design report: "если код неверный/истёк — запросить
        новую CAPTCHA и спросить снова").

        Статус ответа НЕ проверяется через raise_for_status() — см.
        submit() ниже про то же самое решение и его обоснование (реальный
        код фронтенда парсит JSON независимо от HTTP-статуса, см. design
        report Stage 2)."""
        try:
            response = await self._client.get(
                _CAPTCHA_URL, headers=_API_HEADERS, timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GibTransportError("failed to request GIB captcha") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise GibTransportError("GIB captcha response is not valid JSON") from exc

        image_id, image_png = _decode_captcha_payload(payload)
        return CaptchaChallenge(image_id=image_id, image_png=image_png)

    async def submit(self, *, plate: str, image_id: str, captcha_code: str) -> dict:
        """POST payment/verification/with-mys-borc-list. Возвращает СЫРОЙ
        распарсенный JSON как есть — интерпретацию (no_debt/has_debt/
        rejected/unexpected) делает ИСКЛЮЧИТЕЛЬНО
        reader/turkey_bot_test/gib/parser.py (см. design report: "не гадать
        схему здесь", session.py — только транспорт).

        raise_for_status() НЕ вызывается: реальный код фронтенда (см.
        design report Stage 1, модуль 14317 общей fetch-обёртки апигейта)
        читает response.json() независимо от HTTP-статуса и передаёт его
        вызывающему коду в обоих случаях (200 и не-200) — то есть тело с
        "messages" может прийти и при не-200 статусе, и его нельзя терять,
        трактуя как непременно транспортную ошибку.

        НИКОГДА не логирует captcha_code (securityCode) и не логирует
        cookies/заголовки запроса целиком (см. задачу) — исключения ниже
        содержат только тип сбоя, не тело запроса/ответа."""
        body = {
            "meta": {"pagination": {"pageNo": 1, "pageSize": 15}},
            "data": {
                "plaka": plate,
                "pasaport": "",
                "ulkeKodu": "",
                "sposEkranTipi": "0",
                "kkOrtam": "TDVD_WB_OUT",
                "imageId": image_id,
                "securityCode": captcha_code,
            },
        }
        try:
            response = await self._client.post(
                _SUBMIT_URL, json=body, headers=_API_HEADERS, timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("GIB submit: transport error (%s)", type(exc).__name__)
            raise GibTransportError("failed to submit GIB check") from exc

        try:
            return response.json()
        except ValueError as exc:
            logger.warning(
                "GIB submit: response is not valid JSON (status=%s)", response.status_code,
            )
            raise GibTransportError("GIB submit response is not valid JSON") from exc
