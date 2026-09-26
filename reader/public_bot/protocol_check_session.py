"""HTTP-транспорт для videos.police.ge (см. задачу "Проверить протокол") —
ОТДЕЛЬНЫЙ внешний сайт от police.ge/protocol (reader/fines/
police_ge_session.py), поэтому отдельный httpx.AsyncClient (пинится на
base_url="https://videos.police.ge" в reader/public_bot/main.py, тот же
приём "один httpx.AsyncClient на внешний сайт", что и там) и отдельный
модуль — НЕ переиспользование PoliceGeSession.

READ-ONLY диагностика (см. эту же задачу, разделы "SITE FLOW"/"переверка
CAPTCHA") — оба факта ниже подтверждены реальными запросами, не
предположением:

- reCAPTCHA v3, подключённая на странице index.php, сервером
  submit-index.php НЕ проверяется — два реальных POST без
  g-recaptcha-response прошли штатно и вернули корректный
  "not found"-результат. Здесь она поэтому вообще не воспроизводится.
- csrf_token, наоборот, ДЕЙСТВИТЕЛЬНО проверяется — неверный/просроченный
  токен возвращает "Session expired please try again", а НЕ страницу с
  результатом. Это единственная session-механика, которую нужно
  обрабатывать (получить свежий токен + ровно один retry, см. задачу п.6:
  "не делать бесконечных retries").

Securimage (см. диагностику) к этой форме не относится — не
воспроизводится вовсе.

Ничего из введённых пользователем значений (fields) сюда не логируется и
не попадает в текст исключений (см. задачу п.4)."""

import re

import httpx

from reader.public_bot.protocol_check_models import ProtocolCheckSessionError

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]*)"')
_SESSION_EXPIRED_MARKER = "Session expired please try again"

_INDEX_PATH = "/index.php?lang=en"
_SUBMIT_PATH = "/submit-index.php"

# Форма videos.police.ge всегда шлёт все 4 поля, пустой строкой — для
# неиспользуемого варианта (см. задачу п.5, HTTP contract) — validateForm()
# на самом сайте требует ИМЕННО такую пару "оба заполнены или оба пусты".
_EMPTY_FIELDS = {"protocolNo": "", "personalNo": "", "documentNo": "", "vehicleNo2": ""}


class VideosPoliceGeSession:
    def __init__(self, client: httpx.AsyncClient, *, request_timeout: float):
        self._client = client
        self._request_timeout = request_timeout

    async def _fetch_csrf_token(self) -> str:
        response = await self._client.get(_INDEX_PATH, timeout=self._request_timeout)
        response.raise_for_status()
        match = _CSRF_RE.search(response.text)
        if not match:
            raise ProtocolCheckSessionError("videos.police.ge: csrf_token не найден на странице index.php")
        return match.group(1)

    async def _submit(self, fields: dict[str, str], *, csrf_token: str) -> str:
        body = {**_EMPTY_FIELDS, **fields, "lang": "en", "csrf_token": csrf_token}
        # follow_redirects=True — POST -> 302 -> GET index.php?lang=en
        # (результат рендерится на редиректнутой странице, не в теле
        # ответа самого POST, см. задачу п.5: "После POST обработать 302 и
        # получить redirected index.php?lang=en в той же session") —
        # httpx.AsyncClient хранит cookie jar между запросами одного
        # клиента автоматически, отдельной передачи PHPSESSID не нужно.
        response = await self._client.post(
            _SUBMIT_PATH, data=body, timeout=self._request_timeout, follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    async def search(self, fields: dict[str, str]) -> str:
        """fields — ТОЛЬКО одна непустая пара (см. вызывающий код —
        ProtocolCheckProvider.check_vehicle/check_protocol собирают ровно
        vehicleNo2+documentNo либо protocolNo+personalNo, никогда обе
        пары сразу). Одна попытка + РОВНО один retry при известном
        "Session expired" marker'е (см. задачу п.6), затем
        ProtocolCheckSessionError — вызывающий код (ProtocolCheckProvider)
        превращает её в typed ERROR, никогда не пробрасывает наверх как
        есть."""
        csrf_token = await self._fetch_csrf_token()
        html = await self._submit(fields, csrf_token=csrf_token)
        if _SESSION_EXPIRED_MARKER in html:
            csrf_token = await self._fetch_csrf_token()
            html = await self._submit(fields, csrf_token=csrf_token)
            if _SESSION_EXPIRED_MARKER in html:
                raise ProtocolCheckSessionError(
                    "videos.police.ge: сессия истекла даже после повторной попытки",
                )
        return html
