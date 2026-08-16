"""Получение PDF полиса после успешной оплаты — см. задачу: "после успешной
оплаты нужно получить PDF полиса и отправить его оператору в Telegram".

ГРАНИЦА/TODO: реальный механизм получения PDF (endpoint tpl.ge? письмо на
email страхователя? страница success на web-front.tpl.ge, куда ведёт
returnUrl из reader/checkout/tpl_client.py?) НЕ исследован browser research'ом
— research останавливался на форме ввода карты (mpi.gc.ge), до какого-либо
успешного завершения оплаты и tpl.ge-редиректа "success" дело не доходило
(см. итоговый research-отчёт). Придумывать endpoint здесь означало бы
угадывать API — вместо этого NotImplementedPolicyDocumentProvider честно
поднимает PolicyDocumentError, а reader/checkout/service.py не считает это
поводом откатить уже подтверждённую успешную оплату (см. _apply_bank_result)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reader.checkout.models import CheckoutState


class PolicyDocumentError(Exception):
    """PDF полиса получить не удалось/механизм не реализован — str(exc) не
    должен содержать секретов (карта/OTP здесь и так недоступны — этот
    этап случается уже после оплаты)."""


@dataclass(frozen=True)
class PolicyDocument:
    filename: str
    content: bytes
    mime_type: str = "application/pdf"


class PolicyDocumentProvider(Protocol):
    async def fetch(self, state: CheckoutState) -> PolicyDocument: ...


class NotImplementedPolicyDocumentProvider:
    """Default — используется, пока реальный источник PDF не подтверждён
    (см. docstring модуля). CheckoutService ловит PolicyDocumentError и
    просто не прикладывает файл к сообщению об успехе — оплата всё равно
    считается завершённой успешно (см. reader/checkout/service.py)."""

    async def fetch(self, state: CheckoutState) -> PolicyDocument:
        raise PolicyDocumentError(
            "Получение PDF полиса не реализовано — механизм не подтверждён research'ом "
            "(см. reader/checkout/policy_document.py)."
        )
