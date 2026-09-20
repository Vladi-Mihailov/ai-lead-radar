"""Тексты уведомлений мониторинга (см. design report п.11 задачи
"Перестроить UX Turkey test bot") — построены ТОЛЬКО из уже нормализованных
ChangeDetector.ProviderChange, никогда из сырых provider-ответов напрямую
(тот же принцип "не выводить сырые данные пользователю", что и в
reader/turkey_bot_test/texts.py)."""

from __future__ import annotations

from decimal import Decimal

from reader.turkey_bot_test.monitoring.change_detector import ChangeType, ProviderChange

_PROVIDER_DISPLAY_NAMES = {
    "gib": "🚔 Штрафы",
    "avrasya": "🚇 Avrasya Tüneli",
    "kgm": "🛣 Все дороги и мосты (KGM)",
}


def _format_try_amount(amount: Decimal) -> str:
    """Тот же формат, что и reader/turkey_bot_test/texts.py::
    _format_try_amount (не импортирую оттуда напрямую, чтобы
    monitoring/ не зависел от texts.py, но алгоритм намеренно идентичен —
    единственный источник истины для формата сумм в проекте)."""
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}".replace(",", " ") + " ₺"
    formatted = f"{quantized:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} ₺"


def format_change_notification(plate: str, change: ProviderChange) -> str:
    provider_name = _PROVIDER_DISPLAY_NAMES.get(change.provider, change.provider)

    if change.change_type == ChangeType.NEW_DEBT:
        header = "🔔 Новая задолженность"
    elif change.change_type == ChangeType.DEBT_RESOLVED:
        header = "✅ Задолженность погашена"
    else:
        header = "🔔 Изменение задолженности"

    lines = [header, "", f"🚗 {plate}", provider_name, ""]

    if change.change_type == ChangeType.NEW_DEBT:
        lines.append(f"Сумма: {_format_try_amount(change.current_total)}")
    elif change.change_type == ChangeType.DEBT_RESOLVED:
        lines.append(f"Было: {_format_try_amount(change.previous_total)}")
    else:
        diff = change.current_total - change.previous_total
        change_str = f"+{_format_try_amount(diff)}" if diff >= 0 else f"-{_format_try_amount(-diff)}"
        lines.append(f"Было: {_format_try_amount(change.previous_total)}")
        lines.append(f"Стало: {_format_try_amount(change.current_total)}")
        lines.append(f"Изменение: {change_str}")

    return "\n".join(lines)
