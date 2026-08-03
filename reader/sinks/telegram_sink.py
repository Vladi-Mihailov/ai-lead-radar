import logging
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient

from reader.core.models import LeadEvent
from reader.sinks.base import BaseSink

logger = logging.getLogger(__name__)

_MAX_TEXT_LENGTH = 3000


@dataclass(frozen=True)
class _ResolvedTarget:
    entity: Any
    label: str


class TelegramSink(BaseSink):
    def __init__(self, client: TelegramClient, forward_to: list[int | str]):
        self._client = client
        self._forward_to = forward_to
        self._resolved: list[_ResolvedTarget] = []

    async def start(self) -> None:
        for target in self._forward_to:
            label = self._label(target)
            try:
                entity = await self._client.get_entity(target)
            except Exception as exc:
                logger.error("✖ Получатель %s не найден", label)
                raise RuntimeError(f"Не найден получатель {label}") from exc

            self._resolved.append(_ResolvedTarget(entity=entity, label=label))
            logger.info("✔ Получатель %s найден", label)

    async def handle(self, event: LeadEvent) -> None:
        message = event.message
        for target in self._resolved:
            try:
                forwarded = await self._client.forward_messages(
                    target.entity,
                    messages=message.id,
                    from_peer=message.chat_id,
                )
            except Exception:
                logger.warning(
                    "Не удалось переслать оригинал сообщения в %s, отправляю текстовую копию",
                    target.label,
                )
            else:
                # forward_messages(messages=<int>) — единичный id, не список,
                # поэтому Telethon возвращает один Message (не список) для
                # только что созданного пересланного сообщения в target-чате.
                # reply_to к нему — чтобы контекст был явной веткой-ответом
                # под пересланным оригиналом, а не отдельным сообщением.
                reply_to = getattr(forwarded, "id", None)
                try:
                    await self._client.send_message(
                        target.entity,
                        self._format_context(event),
                        parse_mode="md",
                        link_preview=False,
                        reply_to=reply_to,
                    )
                except Exception:
                    logger.warning(
                        "Не удалось отправить контекст (группа/автор/ссылка) в %s",
                        target.label,
                    )

                continue

            try:
                await self._client.send_message(target.entity, self._format(event), link_preview=False)
            except Exception:
                logger.exception("Не удалось отправить лид в чат %s", target.label)

    @staticmethod
    def _label(target: int | str) -> str:
        return f"@{target}" if isinstance(target, str) else str(target)

    @staticmethod
    def _format_context(event: LeadEvent) -> str:
        message = event.message
        lines = [f"📍 **{message.chat_title}**"]

        if message.sender_username:
            lines.append(f"👤 @{message.sender_username}")
        elif message.sender_name:
            lines.append(f"👤 {message.sender_name}")

        # dict.fromkeys — убирает дубликаты, сохраняя порядок первого
        # появления; затем ограничиваем пятью, как и требуется.
        keywords = list(
            dict.fromkeys(kw for m in event.matches for kw in m.matched_keywords)
        )[:5]
        if keywords:
            lines.append(f"🎯 Совпадения: {', '.join(keywords)}")

        if message.link:
            lines.append(f"🔗 [Открыть оригинал]({message.link})")

        return "\n".join(lines)

    @staticmethod
    def _format(event: LeadEvent) -> str:
        message = event.message
        scenario_names = ", ".join(m.scenario_name for m in event.matches)
        keywords = ", ".join(sorted({kw for m in event.matches for kw in m.matched_keywords}))
        author = message.sender_username or message.sender_name or message.sender_id or "неизвестно"

        body = message.text
        if len(body) > _MAX_TEXT_LENGTH:
            body = body[:_MAX_TEXT_LENGTH] + "…"

        lines = [
            f"Группа: {message.chat_title}",
            f"Автор: {author}",
            "",
            body,
            "",
            f"Причина: {scenario_names} ({keywords})",
        ]
        if message.link:
            lines.append(f"Ссылка: {message.link}")

        return "\n".join(lines)
