"""ConversationController — пошаговый Add Car flow и "Мои авто" @GEShtrafbot,
поверх BotConversationStateRepository + SubscriptionService.

Намеренно НИЧЕГО не знает про Telethon — принимает уже извлечённые из
события значения (sender_id/username/first_name/last_name/text/chat_id),
возвращает BotReply (что показать пользователю). Тонкий Telethon-адаптер,
который извлекает эти значения из реальных событий — reader/public_bot/
handlers.py. Такое разделение позволяет тестировать весь flow без
реального Telegram-подключения (тот же приём, что и CommandContext/
CommandResult в reader/commands/base.py для операторских команд).

Security-инвариант (см. design report): единственный источник identity —
telegram_user_id, полученный ВЫЗЫВАЮЩИМ кодом из event.sender_id. Callback
периода (см. handle_period_choice) несёт только выбранное значение дней —
кому это применить, решает ИСКЛЮЧИТЕЛЬНО conversation_state (ключ — chat_id,
сверка владения — telegram_user_id), а не сам callback.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from reader.fines.validation import FineValidationError, normalize_car_number
from reader.public_bot import texts
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.public_bot.validation import UsernameValidationError, normalize_telegram_username

STEP_AWAITING_CAR_NUMBER = "awaiting_car_number"
STEP_AWAITING_USERNAME = "awaiting_username"
STEP_AWAITING_PERIOD = "awaiting_period"

PERIOD_CHOICES = (30, 90, 180, 365)


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю — Telethon-адаптер (handlers.py) решает,
    какую клавиатуру приложить к тексту, исходя из этих двух флагов."""

    text: str
    show_main_menu: bool = False
    show_period_buttons: bool = False


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: BotConversationStateRepository,
        subscription_service: SubscriptionService,
        *,
        tz: ZoneInfo,
    ):
        self._states = conversation_state_repository
        self._subscriptions = subscription_service
        self._tz = tz

    def _today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self._tz).date()

    # ---- /start и главное меню ----

    def start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """/start — сбрасывает ЛЮБОЙ незавершённый диалог этого chat_id и
        показывает главное меню (см. design: "/start и повторное нажатие
        Добавить авто очищают старое незавершённое состояние")."""
        self._states.clear(chat_id)
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    def _handle_menu_label(
        self, stripped_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """None, если stripped_text не совпадает ни с одним пунктом меню —
        вызывающий код (handle_text) тогда трактует текст как ввод текущего
        шага диалога, а не команду меню."""
        if stripped_text == "/start":
            return self.start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped_text == texts.ADD_CAR_LABEL:
            # Любое незавершённое состояние этого chat_id отбрасывается —
            # новый flow начинается с чистого листа.
            self._states.clear(chat_id)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CAR_NUMBER,
            )
            return BotReply(text=texts.CAR_NUMBER_PROMPT)

        if stripped_text == texts.MY_CARS_LABEL:
            self._states.clear(chat_id)
            subscriptions = self._subscriptions.list_my_cars(telegram_user_id)
            return BotReply(text=texts.format_my_cars(subscriptions, self._today()))

        if stripped_text in (texts.CHECK_NOW_LABEL, texts.STOP_LABEL):
            # Реализуется в следующем этапе (см. Stage 2 report) — сейчас
            # только временный ответ, без выбора конкретного авто и без
            # callback вообще (не вводим небезопасный payload заранее).
            self._states.clear(chat_id)
            return BotReply(text=texts.COMING_SOON_TEXT)

        return None

    # ---- текстовые сообщения (меню + шаги диалога) ----

    def handle_text(
        self,
        text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
    ) -> BotReply:
        stripped = text.strip()

        menu_reply = self._handle_menu_label(
            stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
        )
        if menu_reply is not None:
            return menu_reply

        state = self._states.get(chat_id)
        if state is None or state.telegram_user_id != telegram_user_id:
            # Нет активного диалога у ЭТОГО пользователя в этом chat_id —
            # мягкая подсказка вместо падения/молчания.
            return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

        if state.step == STEP_AWAITING_CAR_NUMBER:
            return self._handle_car_number_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, username=username,
            )

        if state.step == STEP_AWAITING_USERNAME:
            return self._handle_username_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state_payload=state.payload,
            )

        if state.step == STEP_AWAITING_PERIOD:
            # Период выбирается ТОЛЬКО inline-кнопкой — текст на этом шаге
            # просто повторно показывает клавиатуру.
            return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    def _handle_car_number_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int, username: str | None,
    ) -> BotReply:
        try:
            car_number = normalize_car_number(raw_text)
        except FineValidationError as exc:
            # Остаёмся на том же шаге — пользователь может ввести номер
            # заново, без необходимости начинать весь flow сначала.
            return BotReply(text=f"❌ {exc.message}\n\n{texts.CAR_NUMBER_PROMPT}")

        if username:
            # Telegram уже отдал username — шаг "Введите Telegram-логин"
            # пропускается полностью (см. design).
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD,
                payload={"car_number": car_number, "username": username},
            )
            return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_USERNAME,
            payload={"car_number": car_number},
        )
        return BotReply(text=texts.USERNAME_PROMPT)

    def _handle_username_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int, state_payload: dict | None,
    ) -> BotReply:
        try:
            username = normalize_telegram_username(raw_text)
        except UsernameValidationError as exc:
            return BotReply(text=f"❌ {exc.message}\n\n{texts.USERNAME_PROMPT}")

        payload = dict(state_payload or {})
        payload["username"] = username
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD, payload=payload,
        )
        return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

    # ---- выбор периода (inline-кнопки) ----

    async def handle_period_choice(
        self,
        days: int,
        *,
        chat_id: int,
        telegram_user_id: int,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply | None:
        """None означает "эту кнопку нельзя обработать сейчас" — Telethon-
        адаптер (handlers.py) в этом случае отвечает на callback коротким
        предупреждением и НИЧЕГО не создаёт/не меняет.

        Ownership-проверка — ЕДИНСТВЕННЫЙ источник истины здесь: сам days
        (пришедший из callback_data) публичен и безвреден сам по себе —
        какую car_number/username/подписку он затронет, решает ТОЛЬКО
        conversation_state, полученное по chat_id, и сверенное с
        telegram_user_id (реальным отправителем ЭТОГО события, а не тем,
        что мог бы нести сам callback_data, если бы там был чей-то id —
        его там нет и не должно быть, см. reader/public_bot/keyboards.py)."""
        if days not in PERIOD_CHOICES:
            return None

        state = self._states.get(chat_id)
        if (
            state is None
            or state.step != STEP_AWAITING_PERIOD
            or state.telegram_user_id != telegram_user_id
        ):
            return None

        payload = state.payload or {}
        car_number = payload.get("car_number")
        username = payload.get("username")
        if not car_number or not username:
            # Не должно происходить штатно (payload всегда заполняется
            # обоими полями к моменту STEP_AWAITING_PERIOD) — но не падаем
            # молча, если состояние всё же оказалось повреждено/устарело.
            self._states.clear(chat_id)
            return BotReply(text=texts.STALE_DIALOG_TEXT, show_main_menu=True)

        today = self._today()
        outcome = await self._subscriptions.add_car(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            car_number=car_number,
            period_days=days,
            today=today,
        )
        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_add_car_summary(
                car_number=car_number,
                username=username,
                start_date=outcome.subscription.start_date,
                end_date=outcome.subscription.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
            )
        )
