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
сверка владения — telegram_user_id), а не сам callback. То же самое для
deep-link claim (см. _handle_claim_start) — claim_token опаден, но
identity, которой он в итоге биндится, берётся ИСКЛЮЧИТЕЛЬНО из
event.sender_id этого конкретного /start, а не из чего-либо в payload.

Trusted-operator delegated flow (см. design report): пользователь из
trusted_operator_user_ids — единственная авторизация ТОЛЬКО по numeric
telegram_user_id из конфига (reader/settings.py::PublicBotSettings,
никогда по username) — после ввода номера авто ВСЕГДА видит
ADD_CLIENT_DECISION_PROMPT ("👤 Добавить Telegram клиента?"): "OK" ведёт к
OWNER_USERNAME_PROMPT (как раньше), "Отмена" ставит машину на мониторинг
БЕЗ клиента и БЕЗ subscription вовсе (см.
SubscriptionService.add_delegated_car_without_client) — username клиента
НЕ обязателен, у клиента он может отсутствовать. Никакого отдельного
экрана "Для себя/Для другого" по-прежнему нет (упрощение — см. design
report).

Trusted-operator TASK-LEVEL admin (см. design report: пересмотр
архитектуры — fine_monitoring_tasks остаётся ЕДИНСТВЕННЫМ source of truth
автомобилей/monitoring jobs, fine_monitoring_subscriptions — ТОЛЬКО
access/delivery слой для реальных клиентов): для telegram_user_id из
trusted_operator_user_ids —

"📋 Мои авто" — ПАГИНИРОВАННЫЙ список ВСЕХ активных fine_monitoring_tasks
(10 на страницу, см. _format_trusted_tasks_page_reply/
handle_trusted_tasks_page) — hard cap "первые 50" убран, доступны все
задачи, включая исторические операторские без единой подписки.

"🔎 Проверить сейчас" — ВВОД НОМЕРА (см. design report: "искать
автомобиль в списке неудобно"), а не список: STEP_AWAITING_TRUSTED_
CHECK_NOW_CAR_NUMBER → _handle_trusted_check_now_car_number_input(),
ищет active task по car_number напрямую, subscription не требуется.

"⛔ Остановить мониторинг" — БЕЗ ИЗМЕНЕНИЙ, всё ещё picker (см.
_build_trusted_stop_picker_reply/handle_trusted_stop_pick/
handle_trusted_stop_confirm) — не переделывался, в этой задаче
технической необходимости не было.

Для обычных пользователей поведение всех трёх пунктов меню НЕ меняется
(см. _build_picker_reply/_format_my_cars_reply — subscription-based, как
и раньше). is_trusted() перепроверяется на КАЖДОМ шаге этого flow заново
по РЕАЛЬНОМУ event.sender_id — callback task_id/page публичны и НЕ
являются доказательством авторизации сами по себе (тот же принцип, что и
у subscription_id выше)."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from reader.fines.validation import FineValidationError, normalize_car_number
from reader.public_bot import texts
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.owner_resolution import OwnerResolutionError
from reader.public_bot.subscription_service import SubscriptionService
from reader.public_bot.validation import UsernameValidationError, normalize_telegram_username

STEP_AWAITING_CAR_NUMBER = "awaiting_car_number"
STEP_AWAITING_USERNAME = "awaiting_username"
STEP_AWAITING_CLIENT_DECISION = "awaiting_client_decision"
STEP_AWAITING_OWNER_USERNAME = "awaiting_owner_username"
STEP_AWAITING_PERIOD = "awaiting_period"
# Trusted-operator task-level 🔎 Проверить сейчас — ввод номера (см.
# design report: "искать автомобиль в списке неудобно"), а не список.
STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER = "awaiting_trusted_check_now_car_number"

PERIOD_CHOICES = (30, 90, 180, 365)

_START_PREFIX = "/start"
_CLAIM_PAYLOAD_PREFIX = "claim_"

# ⛔ Остановить мониторинг для trusted-оператора остаётся НЕ пагинированным
# picker'ом (см. design report: "пока существующую реализацию не
# переделывай, если это не требуется технически") — этот лимит защищает
# ТОЛЬКО его: без него inline-клавиатура на 250+ задач стала бы физически
# непригодной для использования. "📋 Мои авто" этот лимит больше НЕ
# использует — она пагинирована (см. _TRUSTED_TASKS_PAGE_SIZE ниже).
_TRUSTED_STOP_PICKER_LIMIT = 50

# "📋 Мои авто" для trusted-оператора — размер страницы (см. design
# report: "по 10 машин на страницу").
_TRUSTED_TASKS_PAGE_SIZE = 10


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю — Telethon-адаптер (handlers.py) решает,
    какую клавиатуру приложить к тексту, исходя из этих полей.

    check_now_options/stop_options — (subscription_id, car_number) для
    построения списка выбора (см. reader/public_bot/keyboards.py::
    options_keyboard) — subscription_id в callback_data ПУБЛИЧЕН и НЕ
    является доказательством авторизации сам по себе: владение всегда
    перепроверяется server-side при нажатии (см.
    SubscriptionService.get_actionable_subscription), а не на этапе
    показа списка (см. design report Stage 4: "никаких действий с чужими
    subscriptions по callback payload").

    trusted_stop_options/trusted_stop_confirm_task_id — task-level аналоги
    ⛔ для trusted-оператора (см. design report про пересмотр архитектуры) —
    (task_id, car_number) вместо (subscription_id, car_number); тот же
    принцип: task_id публичен, авторизация — ИСКЛЮЧИТЕЛЬНО server-side
    повторная проверка (is_trusted + задача существует и активна, см.
    SubscriptionService.get_active_task_for_trusted_admin).

    trusted_tasks_page/trusted_tasks_total_pages — 📋 Мои авто пагинация
    для trusted-оператора (см. design report) — page (0-indexed) публичен
    и НЕ является доказательством авторизации: is_trusted() перепроверяется
    на каждом callback заново, а page вне диапазона клампится сервером."""

    text: str
    show_main_menu: bool = False
    show_period_buttons: bool = False
    show_add_client_decision_buttons: bool = False
    check_now_options: list[tuple[int, str]] | None = None
    stop_options: list[tuple[int, str]] | None = None
    stop_confirm_subscription_id: int | None = None
    trusted_stop_options: list[tuple[int, str]] | None = None
    trusted_stop_confirm_task_id: int | None = None
    trusted_stop_confirm_button_label: str | None = None
    trusted_tasks_page: int | None = None
    trusted_tasks_total_pages: int | None = None


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: BotConversationStateRepository,
        subscription_service: SubscriptionService,
        *,
        tz: ZoneInfo,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
    ):
        self._states = conversation_state_repository
        self._subscriptions = subscription_service
        self._tz = tz
        # frozenset(...) на входе — на случай, если вызывающий код (см.
        # reader/public_bot/main.py) передал обычный list из config.yaml.
        self._trusted_operator_user_ids = frozenset(trusted_operator_user_ids)

    def _today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self._tz).date()

    def _is_trusted(self, telegram_user_id: int) -> bool:
        """Единственная проверка авторизации trusted-режима — ТОЛЬКО по
        numeric telegram_user_id (см. design report: "authorization
        trusted mode только по numeric Telegram user_id из config"),
        никогда по username."""
        return telegram_user_id in self._trusted_operator_user_ids

    # ---- /start, главное меню, claim deep-link ----

    def start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """/start — сбрасывает ЛЮБОЙ незавершённый диалог этого chat_id и
        показывает главное меню (см. design: "/start и повторное нажатие
        Добавить авто очищают старое незавершённое состояние")."""
        self._states.clear(chat_id)
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    def _handle_start_command(
        self,
        stripped_text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply:
        """"/start" сам по себе либо "/start claim_<token>" (deep-link,
        см. reader/public_bot/subscription_service.py::_build_claim_link) —
        второе Telegram доставляет как обычное текстовое сообщение вида
        "/start <payload>", когда пользователь переходит по
        https://t.me/<bot>?start=<payload>."""
        payload = stripped_text[len(_START_PREFIX):].strip()
        if not payload:
            return self.start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if payload.startswith(_CLAIM_PAYLOAD_PREFIX):
            token = payload[len(_CLAIM_PAYLOAD_PREFIX):]
            self._states.clear(chat_id)
            outcome = self._subscriptions.claim(
                token,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_username=username,
                first_name=first_name,
                last_name=last_name,
            )
            if outcome is None:
                return BotReply(text=texts.CLAIM_INVALID_TEXT, show_main_menu=True)

            return BotReply(
                text=texts.CLAIM_SUCCESS_TEXT.format(
                    # Bot identity switch (см. audit report): @ProtocolGEbot —
                    # НЕ через _BOT_USERNAME/SubscriptionService (та же
                    # значение, но отдельный hardcode здесь, т.к. этот текст
                    # формируется в conversation.py, а не в
                    # subscription_service.py::_build_claim_link).
                    car_number=outcome.subscription.car_number, bot_username="ProtocolGEbot",
                ),
                show_main_menu=True,
            )

        # Неизвестный payload — ведём себя как обычный /start, не падаем.
        return self.start(chat_id=chat_id, telegram_user_id=telegram_user_id)

    def _handle_menu_label(
        self,
        stripped_text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply | None:
        """None, если stripped_text не совпадает ни с одним пунктом меню —
        вызывающий код (handle_text) тогда трактует текст как ввод текущего
        шага диалога, а не команду меню."""
        if stripped_text == _START_PREFIX or stripped_text.startswith(_START_PREFIX + " "):
            return self._handle_start_command(
                stripped_text,
                chat_id=chat_id, telegram_user_id=telegram_user_id, telegram_chat_id=chat_id,
                username=username, first_name=first_name, last_name=last_name,
            )

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
            if self._is_trusted(telegram_user_id):
                return self._format_trusted_tasks_page_reply(0)
            return self._format_my_cars_reply(telegram_user_id)

        if stripped_text == texts.CHECK_NOW_LABEL:
            self._states.clear(chat_id)
            if self._is_trusted(telegram_user_id):
                # Ввод номера, НЕ список (см. design report: "искать
                # автомобиль в списке неудобно") — ответ на следующее
                # сообщение обрабатывается в handle_text() ниже.
                self._states.set(
                    chat_id, telegram_user_id=telegram_user_id,
                    step=STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER,
                )
                return BotReply(text=texts.CAR_NUMBER_PROMPT)
            return self._build_picker_reply(telegram_user_id, kind="checknow")

        if stripped_text == texts.STOP_LABEL:
            self._states.clear(chat_id)
            if self._is_trusted(telegram_user_id):
                return self._build_trusted_stop_picker_reply()
            return self._build_picker_reply(telegram_user_id, kind="stop")

        return None

    def _format_trusted_tasks_page_reply(self, page: int) -> BotReply:
        """"📋 Мои авто" для trusted-оператора — ОДНА страница ВСЕХ
        активных fine_monitoring_tasks (см. design report: hard cap
        "первые 50" убран — пагинация по _TRUSTED_TASKS_PAGE_SIZE вместо
        него), subscription для отображения не требуется вовсе.

        page — ЛЮБОЕ int (в т.ч. отрицательное/за пределами общего числа
        страниц, см. design report: "page из callback нельзя считать
        authorization") — клампится здесь, а не у вызывающего кода,
        поэтому единственная точка, где может быть баг с границами."""
        total = self._subscriptions.count_active_tasks()
        if total == 0:
            return BotReply(text=texts.NO_ACTIVE_TASKS_TEXT)

        total_pages = -(-total // _TRUSTED_TASKS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        tasks = self._subscriptions.list_active_tasks_page(page=page, page_size=_TRUSTED_TASKS_PAGE_SIZE)

        return BotReply(
            text=texts.format_trusted_tasks_page(tasks, page=page, total_pages=total_pages),
            trusted_tasks_page=page,
            trusted_tasks_total_pages=total_pages,
        )

    def handle_trusted_tasks_page(self, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Навигация 📋 Мои авто (◀️/индикатор/▶️) — None, только если
        telegram_user_id НЕ trusted (см. design report: "на каждом
        callback повторно проверять trusted_operator_user_ids"); сам page
        не может быть "невалидным" — _format_trusted_tasks_page_reply()
        клампит его в допустимый диапазон, forged/out-of-range page просто
        показывает ближайшую валидную страницу, а не ошибку."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self._format_trusted_tasks_page_reply(page)

    def _build_trusted_stop_picker_reply(self) -> BotReply:
        """Список активных задач для ⛔ trusted-оператора — БЕЗ ИЗМЕНЕНИЙ
        (см. design report: "пока существующую реализацию не
        переделывай") — task_id в кнопках (не subscription_id, не
        car_number): владение/актуальность перепроверяются server-side при
        нажатии (см. handle_trusted_stop_pick), список сам по себе — не
        источник авторизации."""
        tasks = self._subscriptions.list_all_active_tasks()
        if not tasks:
            return BotReply(text=texts.NO_ACTIVE_TASKS_TEXT)

        options = [(t.id, t.car_number) for t in tasks[:_TRUSTED_STOP_PICKER_LIMIT]]
        return BotReply(text=texts.TRUSTED_STOP_PICK_PROMPT, trusted_stop_options=options)

    def _build_picker_reply(self, telegram_user_id: int, *, kind: str) -> BotReply:
        """Список авто, с которыми telegram_user_id может действовать
        через 🔎/⛔ (свои + delegated, которые он создал, см.
        SubscriptionService.list_actionable_subscriptions) — subscription_id
        в кнопках, не car_number, чтобы не полагаться на уникальность
        номера при последующей server-side проверке владения."""
        subscriptions = self._subscriptions.list_actionable_subscriptions(
            telegram_user_id, today=self._today(),
        )
        if not subscriptions:
            return BotReply(text=texts.NO_ACTIONABLE_CARS_TEXT)

        options = [(s.id, s.car_number) for s in subscriptions]
        if kind == "checknow":
            return BotReply(text=texts.CHECK_NOW_PICK_PROMPT, check_now_options=options)
        return BotReply(text=texts.STOP_PICK_PROMPT, stop_options=options)

    def _format_my_cars_reply(self, telegram_user_id: int) -> BotReply:
        today = self._today()
        subscriptions = self._subscriptions.list_my_cars(telegram_user_id)
        text = texts.format_my_cars(subscriptions, today)

        if self._is_trusted(telegram_user_id):
            managed = self._subscriptions.list_managed_cars(telegram_user_id)
            text = "\n\n".join([
                text,
                "",
                texts.MANAGED_CARS_HEADER,
                texts.format_managed_cars(managed, today),
            ])

        return BotReply(text=text)

    # ---- текстовые сообщения (меню + шаги диалога) ----

    async def handle_text(
        self,
        text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> BotReply:
        """async — единственная причина: STEP_AWAITING_TRUSTED_CHECK_NOW_
        CAR_NUMBER ниже вызывает await check_now_task_by_car_number()
        (реальная сетевая проверка). Все остальные ветки синхронны и
        просто return'ят как раньше — awaiting здесь им не вредит."""
        stripped = text.strip()

        menu_reply = self._handle_menu_label(
            stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            username=username, first_name=first_name, last_name=last_name,
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

        if state.step == STEP_AWAITING_CLIENT_DECISION:
            # Тот же приём, что и у STEP_AWAITING_PERIOD ниже — выбор
            # только inline-кнопкой, текст на этом шаге просто повторно
            # показывает вопрос+кнопки.
            return BotReply(
                text=texts.ADD_CLIENT_DECISION_PROMPT, show_add_client_decision_buttons=True,
            )

        if state.step == STEP_AWAITING_OWNER_USERNAME:
            return self._handle_owner_username_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state_payload=state.payload,
            )

        if state.step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER:
            return await self._handle_trusted_check_now_car_number_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
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

        if self._is_trusted(telegram_user_id):
            # Trusted-оператор — сначала явно спрашиваем, есть ли клиент
            # вообще (см. design report: username клиента не обязателен —
            # у клиента он может отсутствовать) — никакого авто-детекта
            # отправителя здесь нет, в отличие от self-service ниже.
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CLIENT_DECISION,
                payload={"car_number": car_number},
            )
            return BotReply(
                text=texts.ADD_CLIENT_DECISION_PROMPT, show_add_client_decision_buttons=True,
            )

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

    def _handle_owner_username_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int, state_payload: dict | None,
    ) -> BotReply:
        try:
            owner_username = normalize_telegram_username(raw_text)
        except UsernameValidationError as exc:
            return BotReply(text=f"❌ {exc.message}\n\n{texts.OWNER_USERNAME_PROMPT}")

        payload = dict(state_payload or {})
        payload["owner_username"] = owner_username
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD, payload=payload,
        )
        return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

    async def _handle_trusted_check_now_car_number_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """Trusted-operator task-level 🔎 Проверить сейчас — ввод номера
        (см. design report). is_trusted() уже был проверен, чтобы попасть
        на этот шаг (см. _handle_menu_label), но перепроверяется ЗАНОВО и
        здесь — тот же принцип "на каждом действии повторно проверять",
        что и у task-level ⛔. Невалидный формат номера — остаёмся на этом
        же шаге (тот же UX, что и у self-service _handle_car_number_input),
        чтобы можно было ввести номер заново без повторного нажатия
        "🔎 Проверить сейчас"."""
        if not self._is_trusted(telegram_user_id):
            self._states.clear(chat_id)
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        try:
            car_number = normalize_car_number(raw_text)
        except FineValidationError as exc:
            return BotReply(text=f"❌ {exc.message}\n\n{texts.CAR_NUMBER_PROMPT}")

        self._states.clear(chat_id)

        outcome = await self._subscriptions.check_now_task_by_car_number(car_number)
        if outcome is None:
            return BotReply(
                text=texts.format_trusted_check_now_not_found(car_number), show_main_menu=True,
            )
        return BotReply(text=texts.format_check_now_result(outcome), show_main_menu=True)

    # ---- "👤 Добавить Telegram клиента?" (inline-кнопки, trusted-flow) ----

    def handle_add_client_decision(
        self, wants_client: bool, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """None — та же server-side проверка, что и у handle_period_choice:
        нет активного диалога ИМЕННО на этом шаге у ИМЕННО этого
        telegram_user_id в этом chat_id (chat_id+conversation_state — а не
        что-либо из самого callback_data). wants_client=True (OK) → тот же
        OWNER_USERNAME_PROMPT, что и раньше; False (Отмена) → сразу период,
        БЕЗ username вовсе (см. handle_period_choice про payload["no_client"])."""
        state = self._states.get(chat_id)
        if (
            state is None
            or state.step != STEP_AWAITING_CLIENT_DECISION
            or state.telegram_user_id != telegram_user_id
        ):
            return None

        payload = state.payload or {}
        car_number = payload.get("car_number")
        if not car_number:
            self._states.clear(chat_id)
            return BotReply(text=texts.STALE_DIALOG_TEXT, show_main_menu=True)

        if wants_client:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_OWNER_USERNAME,
                payload={"car_number": car_number},
            )
            return BotReply(text=texts.OWNER_USERNAME_PROMPT)

        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD,
            payload={"car_number": car_number, "no_client": True},
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
        его там нет и не должно быть, см. reader/public_bot/keyboards.py).

        Ветвится на self-service/delegated-с-клиентом/delegated-без-клиента
        по содержимому payload (owner_username / no_client — см.
        _handle_owner_username_input и handle_add_client_decision) —
        никогда повторно не проверяет trusted-статус здесь: раз payload
        уже сформирован верным путём (единственный способ попасть в
        STEP_AWAITING_OWNER_USERNAME или payload["no_client"] — быть
        trusted, см. _handle_car_number_input/handle_add_client_decision),
        этого достаточно."""
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
        owner_username = payload.get("owner_username")
        username = payload.get("username")
        no_client = bool(payload.get("no_client"))

        if not car_number or not (owner_username or username or no_client):
            # Не должно происходить штатно (payload всегда заполняется к
            # моменту STEP_AWAITING_PERIOD) — но не падаем молча, если
            # состояние всё же оказалось повреждено/устарело.
            self._states.clear(chat_id)
            return BotReply(text=texts.STALE_DIALOG_TEXT, show_main_menu=True)

        today = self._today()

        if no_client:
            return await self._complete_delegated_add_car_without_client(
                car_number=car_number, days=days, today=today,
                chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        if owner_username:
            return await self._complete_delegated_add_car(
                car_number=car_number, owner_username=owner_username, days=days, today=today,
                chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        return await self._complete_self_add_car(
            car_number=car_number, username=username, days=days, today=today,
            chat_id=chat_id, telegram_user_id=telegram_user_id,
            first_name=first_name, last_name=last_name,
        )

    async def _complete_self_add_car(
        self, *, car_number, username, days, today, chat_id, telegram_user_id, first_name, last_name,
    ) -> BotReply:
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

    async def _complete_delegated_add_car(
        self, *, car_number, owner_username, days, today, chat_id, telegram_user_id,
    ) -> BotReply:
        try:
            outcome = await self._subscriptions.add_delegated_car(
                created_by_telegram_user_id=telegram_user_id,
                created_by_telegram_chat_id=chat_id,
                owner_username=owner_username,
                car_number=car_number,
                period_days=days,
                today=today,
            )
        except OwnerResolutionError:
            self._states.clear(chat_id)
            return BotReply(text=texts.OWNER_RESOLUTION_ERROR_TEXT, show_main_menu=True)

        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_delegated_add_car_summary(
                car_number=car_number,
                owner_username=owner_username,
                start_date=outcome.subscription.start_date,
                end_date=outcome.subscription.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
                pending_claim=outcome.pending_claim,
                claim_link=outcome.claim_link,
            )
        )

    async def _complete_delegated_add_car_without_client(
        self, *, car_number, days, today, chat_id, telegram_user_id,
    ) -> BotReply:
        outcome = await self._subscriptions.add_delegated_car_without_client(
            created_by_telegram_user_id=telegram_user_id,
            created_by_telegram_chat_id=chat_id,
            car_number=car_number,
            period_days=days,
            today=today,
        )
        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_delegated_add_car_without_client_summary(
                car_number=car_number,
                start_date=outcome.task.start_date,
                end_date=outcome.task.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
            )
        )

    # ---- 🔎 Проверить сейчас (inline-кнопки, без conversation_state) ----

    async def handle_check_now_choice(
        self, subscription_id: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — подписка не найдена/не принадлежит/не создана этим
        telegram_user_id (см. SubscriptionService.get_actionable_subscription,
        вызывается внутри check_now()) — Telethon-адаптер (handlers.py)
        в этом случае отвечает алертом и ничего не проверяет. subscription_id
        пришёл из callback_data (см. reader/public_bot/keyboards.py) —
        публичный, не секрет; авторизация — ИСКЛЮЧИТЕЛЬНО через запрос к
        БД по РЕАЛЬНОМУ event.sender_id, а не через факт валидности id."""
        outcome = await self._subscriptions.check_now(subscription_id, telegram_user_id=telegram_user_id)
        if outcome is None:
            return None
        return BotReply(text=texts.format_check_now_result(outcome))

    # ---- ⛔ Остановить мониторинг (pick -> confirm, без conversation_state) ----

    def handle_stop_pick(self, subscription_id: int, *, telegram_user_id: int) -> BotReply | None:
        """Первый шаг — показывает подтверждение, ЕЩЁ НИЧЕГО не
        останавливает. None — та же server-side проверка владения, что и
        у check-now (см. SubscriptionService.get_actionable_subscription)."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None

        return BotReply(
            text=texts.STOP_CONFIRM_PROMPT.format(car_number=subscription.car_number),
            stop_confirm_subscription_id=subscription.id,
        )

    def handle_stop_confirm(self, subscription_id: int, *, telegram_user_id: int) -> BotReply:
        """Финальный шаг — владение перепроверяется ЗАНОВО здесь (а не
        только доверяется тому, что пользователь дошёл до этого экрана,
        см. design report: "нельзя подменить owner/user_id через callback
        payload") — SubscriptionService.stop_subscription() сам делает эту
        проверку через FineSubscriptionRepository.stop_by_owner_or_creator."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return BotReply(text=texts.STOP_FAILED_TEXT, show_main_menu=True)

        stopped = self._subscriptions.stop_subscription(subscription_id, telegram_user_id=telegram_user_id)
        if not stopped:
            return BotReply(text=texts.STOP_FAILED_TEXT, show_main_menu=True)

        return BotReply(text=texts.format_stop_success(subscription.car_number), show_main_menu=True)

    def handle_stop_cancel(self) -> BotReply:
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    # ---- trusted-operator task-level admin (см. design report: пересмотр
    # архитектуры) — 🔎/⛔ работают НАПРЯМУЮ с fine_monitoring_tasks, без
    # subscription. is_trusted() перепроверяется ЗАНОВО на каждом шаге по
    # РЕАЛЬНОМУ telegram_user_id (никогда не из предыдущего шага/payload) —
    # callback task_id публичен и НЕ является доказательством авторизации
    # сам по себе. ----

    def handle_trusted_stop_pick(self, task_id: int, *, telegram_user_id: int) -> BotReply | None:
        """Первый шаг — показывает подтверждение, ЕЩЁ НИЧЕГО не
        останавливает. Текст/кнопка подтверждения зависят от того, есть ли
        у задачи ещё actionable (active/pending_claim) client-подписки
        (см. design report): 0 — обычное подтверждение; 1+ — явное
        предупреждение "также отслеживается клиентом(-ами)", кнопка
        "Остановить для всех"."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return None

        subscriber_count = self._subscriptions.count_active_or_pending_subscribers_for_task(task_id)
        return BotReply(
            text=texts.format_trusted_stop_confirm_prompt(task.car_number, subscriber_count),
            trusted_stop_confirm_task_id=task.id,
            trusted_stop_confirm_button_label=texts.trusted_stop_confirm_button_label(subscriber_count),
        )

    def handle_trusted_stop_confirm(self, task_id: int, *, telegram_user_id: int) -> BotReply:
        """Финальный шаг — is_trusted()+задача существует/активна
        перепроверяются ЗАНОВО здесь (а не только доверяются тому, что
        пользователь дошёл до этого экрана, см. design report: "на КАЖДОМ
        действии и особенно финальном Stop повторно проверять") —
        SubscriptionService.stop_task_for_trusted_admin() сам делает
        итоговую проверку актуальности задачи ещё раз перед записью."""
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        task = self._subscriptions.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return BotReply(text=texts.TRUSTED_STOP_FAILED_TEXT, show_main_menu=True)

        stopped = self._subscriptions.stop_task_for_trusted_admin(task_id)
        if not stopped:
            return BotReply(text=texts.TRUSTED_STOP_FAILED_TEXT, show_main_menu=True)

        return BotReply(text=texts.format_stop_success(task.car_number), show_main_menu=True)
