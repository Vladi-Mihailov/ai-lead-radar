"""Ожидаемый подвал с ресурсами в end-to-end тестах ЛС-черновиков: тот же
код, что в processor (reply_style.resource_footer), для даты тестовых часов
(T0 = 2026-10-06 -> «вчера» относительно оформления 2026-10-05)."""

from datetime import date

from reader.dm_campaigns.reply_style import resource_footer

RESOURCES = ("@tplgee", "@ProtocolGEbot")
TODAY = date(2026, 10, 6)


def footer(seed: int, source: str, replies: tuple[str, ...] = ()) -> str:
    """Подвал, который decorate() добавит после ответа: «<tplgee>\\n<protocol>»."""
    return "\n".join(resource_footer(source, replies, campaign_resources=RESOURCES, seed=seed, today=TODAY))
