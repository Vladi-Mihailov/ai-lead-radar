import logging

from telethon import TelegramClient

from reader.core.models import LeadEvent
from reader.sinks.base import BaseSink
from reader.sinks.telegram_lead_delivery import ResolvedTarget, TelegramLeadDelivery, resolve_label

logger = logging.getLogger(__name__)


class TelegramSink(BaseSink):
    def __init__(self, client: TelegramClient, forward_to: list[int | str]):
        self._client = client
        self._forward_to = forward_to
        self._resolved: list[ResolvedTarget] = []
        self._delivery = TelegramLeadDelivery(client)

    async def start(self) -> None:
        # Дедупликация ПОСЛЕ резолва, по entity.id — а не по сырой строке
        # из forward_to: одна и та же цель может быть указана в разных
        # формах (регистр username, с "@"/без, username и её же numeric
        # id) и всё равно резолвится в одну и ту же Telegram-сущность.
        # Сравнение сырых строк такие дубликаты не поймало бы, а без этой
        # проверки один и тот же получатель получал бы каждый лид дважды
        # (см. задачу про расширение LEAD_FORWARD_TO до трёх получателей).
        seen_entity_ids: set[int] = set()
        for target in self._forward_to:
            label = resolve_label(target)
            try:
                entity = await self._client.get_entity(target)
            except Exception:
                # Fail-soft, НЕ RuntimeError: LEAD_FORWARD_TO — избыточный,
                # необязательный канал (лиды в любом случае уже пишутся в
                # ConsoleSink/FileSink), а получатель, заданный username,
                # может быть переименован на стороне Telegram в любой
                # момент (см. задачу-инцидент: @ali_na_l_i переименован,
                # уронил весь reader.main — fine monitor, OCR, checkout —
                # хотя сломан был только один из нескольких lead-forward
                # получателей). Резолв стабильным numeric id (см.
                # telegram_accounts.telegram_user_id/reader/inviter/
                # identity.py — тот же принцип для inviter-аккаунтов)
                # такого разрыва не имеет; username здесь — display-only
                # ярлык, а не обязательный для резолва идентификатор.
                logger.warning(
                    "✖ Получатель %s не найден (переименован/удалён?) — "
                    "пропущен, доставка остальным получателям продолжится",
                    label,
                )
                continue

            entity_id = getattr(entity, "id", None)
            if entity_id is not None and entity_id in seen_entity_ids:
                logger.info(
                    "— Получатель %s — дубликат уже добавленного, пропущен", label
                )
                continue
            if entity_id is not None:
                seen_entity_ids.add(entity_id)

            self._resolved.append(ResolvedTarget(entity=entity, label=label))
            logger.info("✔ Получатель %s найден", label)

        if self._forward_to and not self._resolved:
            logger.error(
                "✖ Ни один из %d получателей LEAD_FORWARD_TO не резолвился — "
                "пересылка лидов в Telegram отключена для этого запуска "
                "(лиды остаются в ConsoleSink/FileSink)",
                len(self._forward_to),
            )

    async def handle(self, event: LeadEvent) -> None:
        for target in self._resolved:
            await self._delivery.deliver(target, event)
