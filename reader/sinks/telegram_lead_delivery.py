import logging
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient

from reader.core.models import LeadEvent

logger = logging.getLogger(__name__)

_MAX_TEXT_LENGTH = 3000


@dataclass(frozen=True)
class ResolvedTarget:
    entity: Any
    label: str


def resolve_label(target: int | str) -> str:
    return f"@{target}" if isinstance(target, str) else str(target)


class TelegramLeadDelivery:
    """Единая логика доставки НАЙДЕННОГО лида одному Telegram-получателю:
    переслать оригинал сообщения + отправить контекст (группа/автор/
    ключевые слова/ссылка) reply'ем к пересланному; если форвард не
    удался — fallback: текстовая копия целиком.

    Вынесено из reader/sinks/telegram_sink.py::TelegramSink, чтобы этим же
    форматом/fallback-поведением мог переиспользоваться
    reader/sinks/lead_ai_sink.py::LeadAiSink — получатель, отфильтрованный
    AI, должен получать лид РОВНО в том же виде, что и получатели без
    AI-фильтра, а не хрупкую копию форматирования (см. задачу: "не
    копировать большую часть TelegramSink вручную"). TelegramSink
    по-прежнему сам резолвит/дедуплицирует СВОИХ (нескольких) получателей —
    сюда вынесена только доставка ОДНОМУ уже резолвленному target.

    deliver() никогда не бросает исключение — любая ошибка (forward,
    context, fallback-текст) только логируется, что и раньше было
    поведением TelegramSink.handle()."""

    def __init__(self, client: TelegramClient):
        self._client = client

    async def deliver(self, target: ResolvedTarget, event: LeadEvent) -> None:
        message = event.message
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
                    _format_context(event),
                    parse_mode="md",
                    link_preview=False,
                    reply_to=reply_to,
                )
            except Exception:
                logger.warning(
                    "Не удалось отправить контекст (группа/автор/ссылка) в %s",
                    target.label,
                )
            else:
                logger.info("✔ Лид доставлен в %s (оригинал + контекст)", target.label)

            return

        try:
            await self._client.send_message(target.entity, _format(event), link_preview=False)
        except Exception:
            logger.exception("Не удалось отправить лид в чат %s", target.label)
        else:
            logger.info("✔ Лид доставлен в %s (текстовая копия)", target.label)


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
