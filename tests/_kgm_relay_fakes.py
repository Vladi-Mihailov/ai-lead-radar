"""Общие фейки для тестов KGM relay (tests/test_kgm_relay_*.py,
tests/test_kgm_route.py). Реальная сеть не используется.

FakeKgm — поддельный webihlaltakip.kgm.gov.tr поверх httpx.MockTransport,
который ведёт себя как ASP.NET WebForms: GET страницы выдаёт cookie
ASP.NET_SessionId и свежие __VIEWSTATE/__VIEWSTATEGENERATOR/hdnx, каждый
отклонённый submit — новые значения в delta-ответе, а POST принимается
только с ТЕМ ЖЕ cookie и ПОСЛЕДНИМИ выданными значениями (иначе HTTP 500,
как настоящий сайт на протухшее состояние). Так тесты доказывают, что
relay не теряет состояние сессии между вызовами."""

import asyncio
import itertools
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge, AvrasyaSubmitOutcome
from reader.turkey_bot.gib.models import CaptchaChallenge, GibFineRecord, GibSubmitOutcome
from reader.turkey_bot.kgm import relay_auth
from reader.turkey_bot.kgm.session import KgmSession

PAGE_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Sayfalar/Sorgulama.aspx?lang=tr"
CAPTCHA_URL = "https://webihlaltakip.kgm.gov.tr/WebIhlalSorgulama/Captcha/CaptchaImage.aspx"

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
HAS_DEBT_DELTA = (_FIXTURES_DIR / "kgm_sorgulama_m295yb196_delta.txt").read_text(encoding="utf-8")
NO_DEBT_DELTA = (_FIXTURES_DIR / "kgm_sorgulama_a123aa180_no_debt_delta.txt").read_text(encoding="utf-8")
# Итог фикстуры HAS_DEBT_DELTA (lblGenelCezaToplam "2.660,00 ₺").
HAS_DEBT_GRAND_TOTAL = Decimal("2660.00")

SECRET = "kgm-relay-test-secret-0123456789abcdef"
OTHER_SECRET = "another-relay-secret-fedcba9876543210xyz"
# Relay в тестах слушает 127.0.0.1 — сеть клиентов задаётся явно (в бою —
# сеть WireGuard 10.50.0.0/24).
LOOPBACK = ("127.0.0.0/8",)
# Заведомо выдуманные значения — по ним тесты ищут утечки в журналах/ответах.
TEST_PLATE = "34TST987"
ACCEPTED_CODE = "22 03713"
WRONG_CODE = "99 99999"

_COOKIE_RE = re.compile(r"ASP\.NET_SessionId=([^;\s]+)")


def delta(*chunks: tuple[str, str, str]) -> str:
    """ASP.NET AJAX delta "<length>|<type>|<id>|<content>|" (длина — в
    символах, как читает parser.parse_delta_response)."""
    return "".join(f"{len(content)}|{ctype}|{cid}|{content}|" for ctype, cid, content in chunks)


@dataclass
class AspNetState:
    number: int
    cookie: str
    viewstate: str
    generator: str
    hdnx: str
    captchas: int = 0
    submits: int = 0


def _page_html(state: AspNetState) -> str:
    return (
        '<html><body><form method="post" action="./Sorgulama.aspx?lang=tr" id="form1">'
        '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
        f'<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{state.viewstate}" />'
        f'<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="{state.generator}" />'
        '<input type="hidden" name="__VIEWSTATEENCRYPTED" id="__VIEWSTATEENCRYPTED" value="" />'
        f'<input type="hidden" name="hdnx" id="hdnx" value="{state.hdnx}" />'
        "</form></body></html>"
    )


def _rejected_delta(state: AspNetState) -> str:
    panel = (
        '<span id="lblHata" class="control-label vgRegEx_bt">metin hatası</span>'
        f'<input type="hidden" name="hdnx" id="hdnx" value="{state.hdnx}" />'
    )
    return delta(
        ("updatePanel", "pnlSayfa", panel),
        ("hiddenField", "__EVENTTARGET", ""),
        ("hiddenField", "__VIEWSTATE", state.viewstate),
        ("hiddenField", "__VIEWSTATEGENERATOR", state.generator),
        ("hiddenField", "__VIEWSTATEENCRYPTED", ""),
    )


def captcha_image(session_number: int, captcha_number: int) -> bytes:
    return b"\xff\xd8\xff\xe0" + f"FAKE-CAPTCHA-{session_number}-{captcha_number}".encode() + b"\xff\xd9"


class FakeKgm:
    """reject_first — сколько первых submit каждой сессии отклонить
    ("metin hatası") даже с верным кодом; page_error — класс httpx-исключения
    для GET страницы (KGM недоступен); captcha_failures_after — после
    стольких картинок CAPTCHA отвечать text/html; delay — пауза перед каждым
    ответом (для таймаутов и проверки семафора)."""

    def __init__(
        self,
        *,
        success_delta: str = HAS_DEBT_DELTA,
        reject_first: int = 0,
        page_error: type[httpx.HTTPError] | None = None,
        captcha_failures_after: int | None = None,
        delay: float = 0.0,
        track_clients: bool = True,
        record: bool = True,
    ):
        self._success_delta = success_delta
        self._reject_first = reject_first
        self._page_error = page_error
        self._captcha_failures_after = captcha_failures_after
        self.delay = delay
        self._track_clients = track_clients
        self._record = record
        self._numbers = itertools.count(1)
        self.states: dict[str, AspNetState] = {}
        self.requests: list[dict] = []
        self.clients: list[httpx.AsyncClient] = []
        self.created_clients = 0
        self.closed_clients = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def session_factory(self) -> tuple[httpx.AsyncClient, KgmSession]:
        client = _CountingClient(self, transport=httpx.MockTransport(self.handler))
        self.created_clients += 1
        if self._track_clients:
            self.clients.append(client)
        return client, KgmSession(client, request_timeout=5)

    def submits(self) -> list[dict]:
        return [r for r in self.requests if r["kind"] == "submit"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._respond(request)
        finally:
            self.in_flight -= 1

    def _log(self, kind: str, cookie: str | None, form: dict | None = None) -> None:
        if self._record:
            self.requests.append({"kind": kind, "cookie": cookie, "form": form})

    def _respond(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        match = _COOKIE_RE.search(request.headers.get("cookie", ""))
        cookie = match.group(1) if match else None

        if request.method == "GET" and url == PAGE_URL:
            self._log("page", cookie)
            if self._page_error is not None:
                raise self._page_error("simulated KGM failure", request=request)
            number = next(self._numbers)
            state = AspNetState(
                number=number, cookie=f"fakeaspnetsession{number:04d}",
                viewstate=f"VS-{number}-0", generator=f"GEN{number:04d}", hdnx=f"HX-{number}-0",
            )
            self.states[state.cookie] = state
            if not self._record and len(self.states) > 8:
                # Тест утечек памяти: старые ASP.NET-сессии фейку больше не нужны.
                del self.states[next(iter(self.states))]
            return httpx.Response(
                200, text=_page_html(state),
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "set-cookie": f"ASP.NET_SessionId={state.cookie}; path=/; HttpOnly",
                },
            )

        state = self.states.get(cookie) if cookie else None
        if request.method == "GET" and url == CAPTCHA_URL:
            self._log("captcha", cookie)
            if state is None:
                return httpx.Response(403, text="no session")
            state.captchas += 1
            if self._captcha_failures_after is not None and state.captchas > self._captcha_failures_after:
                return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})
            return httpx.Response(
                200, content=captcha_image(state.number, state.captchas), headers={"content-type": "image/jpeg"},
            )

        if request.method == "POST" and url == PAGE_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}
            self._log("submit", cookie, form)
            if (
                state is None
                or form.get("__VIEWSTATE") != state.viewstate
                or form.get("__VIEWSTATEGENERATOR") != state.generator
                or form.get("hdnx") != state.hdnx
            ):
                return httpx.Response(500, text="Validation of viewstate MAC failed.")
            state.submits += 1
            if state.submits <= self._reject_first or form.get("txtimgcode") != ACCEPTED_CODE:
                state.viewstate = f"VS-{state.number}-{state.submits}"
                state.hdnx = f"HX-{state.number}-{state.submits}"
                return httpx.Response(
                    200, text=_rejected_delta(state), headers={"content-type": "text/plain; charset=utf-8"},
                )
            return httpx.Response(
                200, text=self._success_delta, headers={"content-type": "text/plain; charset=utf-8"},
            )

        raise AssertionError(f"unexpected request {request.method} {url}")


class _CountingClient(httpx.AsyncClient):
    def __init__(self, fake: FakeKgm, **kwargs):
        super().__init__(**kwargs)
        self._fake = fake

    async def aclose(self) -> None:
        if not self.is_closed:
            self._fake.closed_clients += 1
        await super().aclose()


async def signed_request(
    client, method: str, path: str, *, body: bytes = b"", secret: str = SECRET, now: float | None = None,
):
    """Подписанный запрос через aiohttp.test_utils.TestClient; now — если
    relay в тесте работает с подменённым wall_clock."""
    headers = relay_auth.signed_headers(secret, method, path, body, now=now)
    if body:
        headers["Content-Type"] = "application/json"
    return await client.request(method, path, data=body or None, headers=headers)


# ---- фейки GİB/Avrasya и OCR для UnifiedTurkeyCheckService ----

class FakeCloseable:
    async def aclose(self) -> None:
        pass


class FakeGibProvider:
    def __init__(self, outcome: GibSubmitOutcome | None = None):
        self._outcome = outcome or GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)

    async def start(self):
        return CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG")

    async def refresh_captcha(self):
        return CaptchaChallenge(image_id="cid-2", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        return self._outcome


class FakeAvrasyaProvider:
    async def start(self):
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def refresh_captcha(self):
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def submit(self, *, plate, captcha_code):
        return AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)


def gib_has_debt_outcome(amount: Decimal = Decimal(237)) -> GibSubmitOutcome:
    return GibSubmitOutcome(
        kind="has_debt", messages=(), raw_data=None,
        fines=(GibFineRecord(
            protocol_no="P1", plate=TEST_PLATE, amount=amount, description="d",
            violation_date=None, authority=None, late_fee=None, discount=None,
        ),),
    )


class FixedCaptchaResolver:
    """Без OCR: KGM — ACCEPTED_CODE (его принимает FakeKgm), остальным — любой."""

    def __init__(self, kgm_code: str = ACCEPTED_CODE):
        self._kgm_code = kgm_code
        self.kgm_images: list[bytes] = []

    async def resolve(self, *, provider, image_png):
        if provider == "kgm":
            self.kgm_images.append(image_png)
            return self._kgm_code
        return "ABCDE"
