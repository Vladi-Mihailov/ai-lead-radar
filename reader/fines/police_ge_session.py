"""HTTP-механика police.ge: сессия (cookies через httpx.AsyncClient),
csrf_token и повторный GET при протухшей сессии. Ни парсинга доменной
модели, ни знания про SQLite/Scheduler/Telegram здесь нет — это только
транспорт. Основано на реальном исследовании сайта:
GET страницы даёт PHPSESSID (cookies самого httpx.AsyncClient) + csrf_token
в скрытом поле формы; POST на index.php?url=protocols/searchByAuto с этим
токеном возвращает JSON. Один и тот же csrf_token обычно годится на серию
POST подряд, но иногда сервер начинает отвечать success:false — тогда
помогает один повторный GET (новый токен) + повтор POST.
"""

import re

import httpx

from reader.fines.police_ge_circuit_breaker import PoliceGeCircuitBreaker
from reader.fines.provider import FineProviderError, PoliceGeUnavailableError

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]*)"')

_SEARCH_URL_PATH = "index.php?url=protocols/searchByAuto"


class PoliceGeSession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        page_url: str,
        request_timeout: float,
        circuit_breaker: PoliceGeCircuitBreaker | None = None,
    ):
        self._client = client
        self._page_url = page_url
        self._timeout = request_timeout
        self._csrf_token: str | None = None
        # None — опционально (см. задачу "back off when police.ge is
        # unavailable"): существующие вызывающие коды/тесты, не
        # передающие circuit_breaker, продолжают работать без изменений
        # (is_available()/allow_request() просто не проверяются вовсе).
        self._circuit_breaker = circuit_breaker

    def is_available(self) -> bool:
        """См. FineProvider.is_available() — используется scheduled-
        джобами ЗАРАНЕЕ, чтобы пропустить задачу целиком без единого
        обращения сюда, когда circuit breaker уже OPEN."""
        if self._circuit_breaker is None:
            return True
        return self._circuit_breaker.allow_request()

    async def search_by_plate(self, plate: str) -> dict:
        """Вернуть уже распарсенный JSON-ответ searchByAuto для номера.

        Один GET на всю HTTP-сессию (переиспользуется между вызовами этого
        метода — вызывающий код может делать серию POST на разные номера в
        рамках одного прохода scheduler'а), плюс один повторный GET+POST,
        если очередной запрос не удался (success:false или невалидный JSON).

        Circuit breaker (см. задачу "back off when police.ge is
        unavailable", production incident 2026-10-21) — ПЕРВАЯ проверка в
        методе, ДО единого обращения к police.ge: если источник уже
        признан недоступным по предыдущим transport-сбоям, бросает
        PoliceGeUnavailableError СРАЗУ, без ожидания request_timeout."""
        if self._circuit_breaker is not None and not self._circuit_breaker.allow_request():
            raise PoliceGeUnavailableError(
                "police.ge временно недоступен (circuit breaker открыт после повторных сбоев соединения)"
            )

        if self._csrf_token is None:
            await self._refresh_csrf_token()

        result = await self._try_search(plate)
        if result is not None:
            return result

        await self._refresh_csrf_token()
        result = await self._try_search(plate)
        if result is not None:
            return result

        raise FineProviderError(
            f"police.ge вернул некорректный ответ для номера {plate} "
            f"даже после обновления сессии (csrf_token)"
        )

    async def _refresh_csrf_token(self) -> None:
        # Production bug (O127PX62, 2026-10-21): необёрнутый httpx.ConnectTimeout
        # (police.ge временно недоступен) проходил мимо check_task()'s
        # except FineProviderError и падал unhandled до самого Telethon-
        # диспетчера — пользователь не получал вообще никакого ответа. ТОТ
        # ЖЕ приём, что и у Turkey-провайдеров (см. reader/turkey_bot/gib/
        # session.py и соседние — except httpx.HTTPError, оборачивают в
        # provider-specific error) — здесь единый FineProviderError, как и
        # требует докстрок FineProvider.search_by_plate ("Бросает
        # FineProviderError при сбое запроса к источнику" — сеть ИЛИ
        # формат, см. police_ge_provider.py докстрок).
        try:
            response = await self._client.get(self._page_url, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Именно TRANSPORT-сбой (см. задачу п.2: "Only network/
            # connectivity failures should affect circuit state") —
            # csrf_token-не-найден ниже НЕ считается (страница ведь
            # реально загрузилась) — это parser/business-ошибка.
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_transport_failure()
            raise FineProviderError(f"Не удалось загрузить страницу police.ge: {exc}") from exc

        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        match = _CSRF_RE.search(response.text)
        if match is None:
            raise FineProviderError("Не удалось найти csrf_token на странице police.ge")

        self._csrf_token = match.group(1)

    async def _try_search(self, plate: str) -> dict | None:
        try:
            response = await self._client.post(
                _SEARCH_URL_PATH,
                data={
                    "firstResult": 0,
                    "protocolAuto": plate,
                    "csrf_token": self._csrf_token,
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._page_url,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_transport_failure()
            raise FineProviderError(f"Не удалось выполнить запрос к police.ge: {exc}") from exc

        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

        try:
            data = response.json()
        except ValueError:
            # Протухшая сессия/csrf — сервер вместо JSON молча отдаёт всю
            # HTML-страницу формы (проверено вживую при разработке интеграции).
            return None

        if not isinstance(data, dict) or data.get("success") is not True:
            return None

        return data
