"""Тексты и подписи живого диалога с оператором — общие для @ProtocolGEbot и
@ProtocolTRbot (флаг и имя бота подставляет профиль, см. service.BotProfile)."""

OPERATOR_LABEL = "👨‍💼 Оператор"
END_DIALOG_LABEL = "❌ Завершить диалог"
SUPPORT_MAIN_MENU_LABEL = "🏠 Главное меню"
CLOSE_DIALOG_BUTTON = "✅ Закрыть диалог"

CONNECTED_TEXT = "👨‍💼 Вы подключены к оператору.\n\nНапишите ваш вопрос — оператор ответит вам здесь."
CLOSED_BY_USER_TEXT = (
    "✅ Диалог с оператором завершён.\n\nЕсли появится новый вопрос, снова нажмите «👨‍💼 Оператор»."
)
CLOSED_BY_MANAGER_TEXT = (
    "✅ Оператор завершил диалог.\n\nЕсли появится новый вопрос, снова нажмите «👨‍💼 Оператор»."
)
UNAVAILABLE_TEXT = "⚠️ Сейчас не удалось связаться с оператором. Попробуйте позже."
UNSUPPORTED_USER_TEXT = "⚠️ Оператору можно отправить текст, фото или документ."
OPERATOR_PREFIX = "👨‍💼 Оператор:"

# Для группы менеджеров.
DELIVERY_FAILED_TEXT = "⚠️ Не удалось доставить сообщение пользователю."
UNSUPPORTED_MANAGER_TEXT = "⚠️ Пользователю можно отправить текст, фото или документ."
DIALOG_CLOSED_NOTE = "✅ Диалог закрыт"


def format_user_card(*, flag: str, bot_username: str, display_name: str | None, username: str | None,
                     body: str, first: bool) -> str:
    """Карточка обращения в группе менеджеров. Телефон не показывается."""
    name = display_name or "—"
    handle = f"@{username}" if username else "—"
    head = (f"{flag} Новое обращение из @{bot_username}" if first
            else f"{flag} Сообщение из @{bot_username}")
    text = f"{head}\n\nИмя: {name}\nUsername: {handle}"
    return f"{text}\n\nСообщение:\n{body}" if body else text


def format_operator_reply(body: str) -> str:
    return f"{OPERATOR_PREFIX}\n\n{body}" if body else OPERATOR_PREFIX
