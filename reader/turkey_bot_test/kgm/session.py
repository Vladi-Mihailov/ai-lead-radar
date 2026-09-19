"""HTTP-транспорт webihlaltakip.kgm.gov.tr (KGM — Karayolları Genel
Müdürlüğü, агрегатор задолженностей по платным дорогам/мостам Турции,
включая Avrasya Tüneli) — см. design report "KGM investigation"
(research-only, реальный HAR fixture, plate M295YB196) и "Реализация KGM
provider".

Полностью независимо от reader/turkey_bot_test/gib/* и
reader/turkey_bot_test/avrasya/* (см. задачу: "Не использовать существующий
reader/turkey_bot_test/avrasya parser для HTML KGM... Это два независимых
источника") — ничего оттуда не импортируется, только parser.py этого же
пакета (см. ниже).

Ключевое архитектурное отличие от GibSession/AvrasyaSession: страница —
классическая ASP.NET WebForms + AJAX UpdatePanel, а НЕ REST API (см.
design report) — submit() — это НАСТОЯЩИЙ async postback (см. design
report: ScriptManager1/__ASYNCPOST/X-MicrosoftAjax, все поля подтверждены
вживую по реальному HAR). __VIEWSTATE/__VIEWSTATEGENERATOR/
__VIEWSTATEENCRYPTED/hdnx — НЕ хардкодятся (см. задачу) — захватываются из
КАЖДОГО ответа (первый GET — обычная HTML-страница, каждый последующий
submit()/refresh_captcha() — ASP.NET AJAX delta-ответ, см. parser.py::
parse_delta_response, который здесь ПЕРЕИСПОЛЬЗУЕТСЯ, а не
переизобретается заново) и переиспользуются в СЛЕДУЮЩЕМ запросе ЭТОЙ ЖЕ
сессии."""

import asyncio
import logging
import re
import time

import httpx

from reader.turkey_bot_test.kgm.models import KgmCaptchaChallenge
from reader.turkey_bot_test.kgm.parser import parse_delta_response

logger = logging.getLogger(__name__)

_PAGE_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Sayfalar/Sorgulama.aspx?lang=tr"
_CAPTCHA_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Captcha/CaptchaImage.aspx"

_COMMON_HEADERS = {"Referer": _PAGE_URL}

# См. design report п.2 — реально увиденные вживую заголовки настоящего
# async postback (HAR, plate M295YB196): без них сервер трактует запрос
# как обычный синхронный postback и отдаёт полную HTML-страницу вместо
# delta-конвертика (подтверждено вживую в этой же сессии исследования).
_ASYNC_POST_HEADERS = {
    **_COMMON_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-MicrosoftAjax": "Delta=true",
    "X-Requested-With": "XMLHttpRequest",
}

# Консервативно — реального лимита у KGM (в отличие от Avrasya, см.
# avrasya/session.py) вживую не наблюдалось, но тот же принцип
# "throttle перед каждым запросом" применяется по умолчанию (не нагружать
# государственный сайт без нужды).
_MIN_REQUEST_INTERVAL_SECONDS = 1.5

_HIDDEN_INPUT_RE_TEMPLATE = r'name="{name}"[^>]*\bvalue="([^"]*)"'


def _extract_hidden_value(html: str, name: str) -> str | None:
    """Универсальный разбор ОДНОГО скрытого ASP.NET-поля — работает и на
    обычной HTML-странице (первый GET), и на HTML-фрагменте внутри
    delta-чанка ("updatePanel", "pnlSayfa") (см. design report: hdnx живёт
    ИМЕННО там, а не отдельным top-level hiddenField-чанком, подтверждено
    вживую по реальному HAR). None — поле не найдено (страница не
    содержит его вовсе, например __VIEWSTATEENCRYPTED обычно пуст, но
    ПРИСУТСТВУЕТ — None здесь означает "не нашли тег", а не "нашли и он
    пуст", вызывающий код (_capture_state) различает это, см. ниже)."""
    match = re.search(_HIDDEN_INPUT_RE_TEMPLATE.format(name=re.escape(name)), html)
    return match.group(1) if match else None


class KgmTransportError(Exception):
    """Сбой транспорта (сеть/DNS/таймаут, неожиданный HTTP-статус/
    Content-Type) — НЕ то же самое, что kind="unexpected" (см.
    reader/turkey_bot_test/kgm/models.py::KgmSubmitOutcome) — тот разбирает
    parser.py из УЖЕ полученного текста ответа (см. модуль docstring
    parser.py: "Любой malformed/unexpected transport => unexpected" —
    относится к ФОРМЕ успешно полученного ответа, не к транспортным
    сбоям, которые попадают сюда)."""


class KgmSession:
    """Один экземпляр — один httpx.AsyncClient — один цикл проверки
    одного номера (см. reader/turkey_bot_test/avrasya/session.py::
    AvrasyaSession — тот же принцип, не переиспользуется между разными
    пользователями/проверками)."""

    def __init__(self, client: httpx.AsyncClient, *, request_timeout: float = 30.0):
        self._client = client
        self._timeout = request_timeout
        self._last_request_at: float | None = None
        # Захватываются из реальных ответов (см. модуль docstring) — НЕ
        # хардкодятся (см. задачу). Пустая строка по умолчанию — то же
        # значение, которое реально несёт __VIEWSTATEENCRYPTED (см.
        # design report: поле всегда присутствует, но пусто).
        self._viewstate = ""
        self._viewstate_generator = ""
        self._viewstate_encrypted = ""
        self._hdnx = ""

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _capture_state_from_html_page(self, html: str) -> None:
        """Захват ASP.NET-полей из ОБЫЧНОЙ HTML-страницы (см. модуль
        docstring) — вызывается ТОЛЬКО после первого GET Sorgulama.aspx
        (см. start())."""
        for attr, name in (
            ("_viewstate", "__VIEWSTATE"),
            ("_viewstate_generator", "__VIEWSTATEGENERATOR"),
            ("_viewstate_encrypted", "__VIEWSTATEENCRYPTED"),
            ("_hdnx", "hdnx"),
        ):
            value = _extract_hidden_value(html, name)
            if value is not None:
                setattr(self, attr, value)

    def _capture_state_from_delta(self, delta_text: str) -> None:
        """Захват ASP.NET-полей из ASP.NET AJAX delta-ответа (см. модуль
        docstring) — вызывается после КАЖДОГО submit() (см. ниже), чтобы
        последующий refresh_captcha()/повторный submit() (после
        "rejected") использовал АКТУАЛЬНЫЕ __VIEWSTATE/hdnx, а не
        протухшие значения с первого GET (см. задачу: "НЕ хардкодить
        динамические ViewState/hdnx/cookies"). Переиспользует
        parser.parse_delta_response — та же функция, что и бизнес-разбор
        (см. parser.py docstring), не переизобретается заново здесь.
        Молча не обновляет состояние, если delta-конвертик не разобрался
        (malformed) — business-level parser.parse_submit_response
        отдельно классифицирует этот же текст как "unexpected", здесь
        достаточно просто не затирать последнее известное валидное
        состояние."""
        chunks = parse_delta_response(delta_text)
        if chunks is None:
            return

        viewstate = chunks.get(("hiddenField", "__VIEWSTATE"))
        if viewstate is not None:
            self._viewstate = viewstate
        generator = chunks.get(("hiddenField", "__VIEWSTATEGENERATOR"))
        if generator is not None:
            self._viewstate_generator = generator
        encrypted = chunks.get(("hiddenField", "__VIEWSTATEENCRYPTED"))
        if encrypted is not None:
            self._viewstate_encrypted = encrypted

        panel_html = chunks.get(("updatePanel", "pnlSayfa"))
        if panel_html is not None:
            hdnx = _extract_hidden_value(panel_html, "hdnx")
            if hdnx is not None:
                self._hdnx = hdnx

    async def start(self) -> KgmCaptchaChallenge:
        """Полный цикл установки сессии — GET самой страницы (те же
        cookies, что видит реальный браузер — ASP.NET_SessionId, см.
        design report), захват ASP.NET-состояния, затем CAPTCHA в рамках
        той же сессии (см. reader/turkey_bot_test/avrasya/session.py::
        AvrasyaSession.start — тот же принцип)."""
        await self._throttle()
        try:
            response = await self._client.get(_PAGE_URL, headers=_COMMON_HEADERS, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KgmTransportError("failed to load KGM page") from exc

        self._capture_state_from_html_page(response.text)
        return await self.refresh_captcha()

    async def refresh_captcha(self) -> KgmCaptchaChallenge:
        """Новая CAPTCHA в рамках УЖЕ существующей сессии (см.
        AvrasyaSession.refresh_captcha — тот же принцип: молча делает
        недействительным код, ожидавшийся для предыдущего fetch, вызывающий
        код обязан показать человеку ИМЕННО картинку из этого вызова)."""
        await self._throttle()
        try:
            response = await self._client.get(_CAPTCHA_URL, headers=_COMMON_HEADERS, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KgmTransportError("failed to request KGM captcha") from exc

        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            raise KgmTransportError(
                f"KGM captcha response has unexpected content-type: {content_type!r}"
            )
        return KgmCaptchaChallenge(image_png=response.content)

    async def submit(self, *, plate: str, captcha_code: str) -> str:
        """Настоящий ASP.NET AJAX async postback (см. модуль docstring и
        design report п.2 — ВСЕ поля ниже реально увидены вживую в HAR,
        НИ ОДНО не придумано). Возвращает СЫРОЙ текст ответа (delta-
        конвертик) — интерпретацию (rejected/has_debt/no_debt/unexpected)
        делает ИСКЛЮЧИТЕЛЬНО reader/turkey_bot_test/kgm/parser.py (тот же
        принцип, что и у GibSession/AvrasyaSession — session.py только
        транспорт).

        ГЕНУИННО динамические поля (__VIEWSTATE*/hdnx) берутся из
        self._viewstate*/self._hdnx (захвачены предыдущим ответом, см.
        _capture_state_from_html_page/_capture_state_from_delta) — НЕ
        хардкодятся (см. задачу). chkGuvenlikUyari/txtGuvenlikCheck —
        фиксированное подтверждение информационного текста ("ОКУДУМ
        АНЛАДЫМ" — те же два значения, что и у реального клика по
        чекбоксу, см. design report: это НЕ CAPTCHA и не анти-бот
        механизм, а обычное обязательное поле формы)."""
        form = {
            "ScriptManager1": "pnlUpdate|btnSorgula",
            "txtPlk": plate,
            "txtimgcode": captcha_code,
            "chkGuvenlikUyari": "on",
            "txtGuvenlikCheck": "Okay",
            "lblPlakaText": plate,
            "lblPlakaText_show": plate,
            "hdnx": self._hdnx,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": self._viewstate,
            "__VIEWSTATEGENERATOR": self._viewstate_generator,
            "__VIEWSTATEENCRYPTED": self._viewstate_encrypted,
            "__ASYNCPOST": "true",
            "btnSorgula": "Sorgula",
        }

        await self._throttle()
        try:
            response = await self._client.post(
                _PAGE_URL, data=form, headers=_ASYNC_POST_HEADERS, timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("KGM submit: transport error (%s)", type(exc).__name__)
            raise KgmTransportError("failed to submit KGM check") from exc

        content_type = response.headers.get("content-type", "")
        if "text" not in content_type:
            raise KgmTransportError(
                f"KGM submit response has unexpected content-type: {content_type!r}"
            )

        self._capture_state_from_delta(response.text)
        return response.text
