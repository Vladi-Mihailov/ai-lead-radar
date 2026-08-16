"""In-memory хранилище CheckoutState — сознательно не БД: состояние живёт
только пока процесс работает (см. задачу про код подтверждения — "не хранить
дольше, чем требуется для текущего checkout"; то же самое рассуждение
применимо и ко всему checkout в целом на этом этапе реализации, реального
банковского шага всё равно ещё нет, см. reader/checkout/payment_gateway.py).

Два индекса на одни и те же CheckoutState:
- по (chat_id, ocr_message_id) — куда отвечает оператор "pay"/исправлениями
  (см. reader/checkout/telegram_integration.py) и откуда берётся защита от
  двойного pay (см. CheckoutStatus/is_locked_status в reader/checkout/models.py);
- по (chat_id, code_prompt_message_id) — куда отвечает оператор кодом
  подтверждения (появляется только после request_confirmation_code(), см.
  reader/checkout/service.py)."""

from __future__ import annotations

from reader.checkout.models import CheckoutState

_Key = tuple[int, int]


class CheckoutStore:
    def __init__(self) -> None:
        self._by_ocr_message: dict[_Key, CheckoutState] = {}
        self._by_code_prompt_message: dict[_Key, CheckoutState] = {}
        self._by_id: dict[str, CheckoutState] = {}

    def get_by_ocr_message(self, chat_id: int, ocr_message_id: int) -> CheckoutState | None:
        return self._by_ocr_message.get((chat_id, ocr_message_id))

    def get_by_id(self, checkout_id: str) -> CheckoutState | None:
        return self._by_id.get(checkout_id)

    def save(self, state: CheckoutState) -> None:
        self._by_ocr_message[(state.chat_id, state.ocr_message_id)] = state
        self._by_id[state.id] = state

    def register_code_prompt_message(self, state: CheckoutState, code_prompt_message_id: int) -> None:
        state.code_prompt_message_id = code_prompt_message_id
        self._by_code_prompt_message[(state.chat_id, code_prompt_message_id)] = state

    def get_by_code_prompt_message(self, chat_id: int, message_id: int) -> CheckoutState | None:
        return self._by_code_prompt_message.get((chat_id, message_id))
