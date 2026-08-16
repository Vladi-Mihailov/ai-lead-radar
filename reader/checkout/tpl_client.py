"""HTTP-клиент двух подтверждённых browser research'ом endpoint'ов tpl.ge —
ни один URL/метод/параметр здесь не придуман (см. итоговый research-отчёт
задачи):

1. POST https://web-back.tpl.ge/api/policies — создание заявки. Публичный,
   без авторизации/CSRF. Тело ответа при успехе ПУСТОЕ (см. research) —
   клиент сам генерирует uId ДО запроса (см. reader/checkout/service.py) и
   именно он остаётся идентификатором заявки, backend ничего встречного не
   возвращает.

2. GET https://ecommerce-api.tpl.ge/ecommerce/{bank}?... — получение ссылки
   на оплату. Отвечает 302 с Location на mpi.gc.ge (форма ввода карты банка-
   эквайера) — этот клиент НЕ переходит по редиректу и не взаимодействует с
   mpi.gc.ge вообще (см. задачу и research: дальше только браузер и реальный
   ввод карты/OTP, что явно вне границ этой реализации — см.
   reader/checkout/payment_gateway.py).

   Путь эквайера ("bog" для Bank of Georgia) подтверждён research'ом только
   для Bank of Georgia — см. reader/checkout/models.py::PaymentBank."""

from __future__ import annotations

import httpx

from reader.checkout.models import PaymentBank, TplPolicyPayload

_POLICIES_URL = "https://web-back.tpl.ge/api/policies"
_ECOMMERCE_BASE_URL = "https://ecommerce-api.tpl.ge/ecommerce"
_TPL_GE_BASE_URL = "https://tpl.ge/ka/policies"

# Единственное подтверждённое research'ом значение paymentType в запросе
# ecommerce/{bank} — назначение остальных возможных значений не исследовано.
_PAYMENT_TYPE = "O"

# Путь эквайера в ecommerce-api.tpl.ge/ecommerce/{path} — подтверждён
# research'ом только для Bank of Georgia (см. reader/checkout/models.py::PaymentBank,
# который сейчас и не содержит других значений).
_BANK_PATH_SEGMENT: dict[PaymentBank, str] = {
    PaymentBank.BANK_OF_GEORGIA: "bog",
}

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class TplGeClientError(Exception):
    """Сбой обращения к tpl.ge (сеть/HTTP статус/отсутствие Location) —
    str(exc) НЕ должен попадать оператору дословно без контекста (см.
    reader/checkout/service.py), т.к. может содержать технические детали."""


class TplGeClient:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def create_policy(self, payload: TplPolicyPayload) -> None:
        """Ничего не возвращает — см. docstring модуля про пустой ответ.
        Успех = HTTP 2xx."""
        try:
            response = await self._client.post(_POLICIES_URL, json=payload.to_json())
        except httpx.HTTPError as exc:
            raise TplGeClientError(f"Сетевая ошибка при создании заявки на tpl.ge: {exc}") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TplGeClientError(
                f"tpl.ge отклонил создание заявки (HTTP {response.status_code})"
            ) from exc

    async def get_payment_redirect_url(
        self,
        *,
        u_id: str,
        bank: PaymentBank,
        payer_title: str,
        payer_identification_number: str,
    ) -> str:
        """Возвращает URL из заголовка Location (страница ввода карты банка-
        эквайера, mpi.gc.ge, см. модуль docstring) — сам по нему не переходит."""
        bank_path = _BANK_PATH_SEGMENT.get(bank)
        if bank_path is None:
            raise TplGeClientError(
                f"Банк '{bank}' не поддержан — путь эквайера не подтверждён research'ом."
            )

        params = {
            "lang": "ka",
            "policyUId": u_id,
            "paymentType": _PAYMENT_TYPE,
            "payerTitle": payer_title,
            "payerIdentificationNumber": payer_identification_number,
            "returnUrl": f"{_TPL_GE_BASE_URL}/{u_id}/success",
            "errorUrl": f"{_TPL_GE_BASE_URL}/{u_id}/error",
        }

        try:
            response = await self._client.get(f"{_ECOMMERCE_BASE_URL}/{bank_path}", params=params)
        except httpx.HTTPError as exc:
            raise TplGeClientError(f"Сетевая ошибка при получении ссылки на оплату: {exc}") from exc

        if response.status_code not in _REDIRECT_STATUS_CODES:
            raise TplGeClientError(
                f"tpl.ge не вернул redirect на оплату (HTTP {response.status_code})"
            )

        location = response.headers.get("location")
        if not location:
            raise TplGeClientError("tpl.ge вернул redirect без заголовка Location")

        return location
