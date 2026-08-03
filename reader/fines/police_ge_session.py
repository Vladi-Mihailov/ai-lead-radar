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

from reader.fines.provider import FineProviderError

_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]*)"')

_SEARCH_URL_PATH = "index.php?url=protocols/searchByAuto"


class PoliceGeSession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        page_url: str,
        request_timeout: float,
    ):
        self._client = client
        self._page_url = page_url
        self._timeout = request_timeout
        self._csrf_token: str | None = None

    async def search_by_plate(self, plate: str) -> dict:
        """Вернуть уже распарсенный JSON-ответ searchByAuto для номера.

        Один GET на всю HTTP-сессию (переиспользуется между вызовами этого
        метода — вызывающий код может делать серию POST на разные номера в
        рамках одного прохода scheduler'а), плюс один повторный GET+POST,
        если очередной запрос не удался (success:false или невалидный JSON).
        """
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
        response = await self._client.get(self._page_url, timeout=self._timeout)
        response.raise_for_status()

        match = _CSRF_RE.search(response.text)
        if match is None:
            raise FineProviderError("Не удалось найти csrf_token на странице police.ge")

        self._csrf_token = match.group(1)

    async def _try_search(self, plate: str) -> dict | None:
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

        try:
            data = response.json()
        except ValueError:
            # Протухшая сессия/csrf — сервер вместо JSON молча отдаёт всю
            # HTML-страницу формы (проверено вживую при разработке интеграции).
            return None

        if not isinstance(data, dict) or data.get("success") is not True:
            return None

        return data
