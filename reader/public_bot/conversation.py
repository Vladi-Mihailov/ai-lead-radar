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
handle_trusted_stop_confirm) — единственный способ остановить ЛЮБУЮ
задачу мониторинга (включая операторские, без единой client-подписки),
которого car-centric ON/OFF (subscription-based, см. ниже) не заменяет.

Для обычных (не-trusted) пользователей "📋 Мои авто" (см. design report
про переработку UX — car-centric ON/OFF) теперь показывает ВСЕ подписки
(включая 'stopped') кнопками — сам автомобиль является кнопкой,
открывающей карточку с управлением (🔎 Проверить сейчас/⏸ Выключить или
▶️ Включить/🗑 Удалить/⬅️ Назад, см. _format_my_cars_page_reply/
handle_my_car_open и далее). Старая отдельная кнопка "⛔ Остановить
мониторинг" для обычных пользователей УБРАНА из главного меню (см.
reader/public_bot/keyboards.py::main_menu_keyboard) — она стала
полностью избыточной (её единственная функция, ON -> OFF, теперь через
карточку конкретного автомобиля), а её subscription-based picker/confirm
(handle_stop_pick/handle_stop_confirm) удалён вместе с ней: usage-аудит
подтвердил, что ни один другой flow на них не полагался (trusted-оператор
всегда использовал ОТДЕЛЬНый task-level picker выше, а не этот).

is_trusted() перепроверяется на КАЖДОМ шаге обоих flow заново по
РЕАЛЬНОМУ event.sender_id — callback task_id/subscription_id/page
публичны и НЕ являются доказательством авторизации сами по себе."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from reader.fines.validation import FineValidationError, normalize_car_number
from reader.public_bot import texts
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.owner_resolution import OwnerResolutionError
from reader.public_bot.statistics_service import BotStatisticsService
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

# Manager-facing "📋 Мои авто" ON/OFF (см. design report про per-car
# monitoring toggle) — "▶️ Продолжить мониторинг" после OFF: ОТДЕЛЬНЫЙ
# набор длительностей от PERIOD_CHOICES выше (15/30/90, не 30/90/180/365) —
# другая клавиатура (3 кнопки + "⬅️ Назад" в один столбец, см. design
# report layout, не 2x2 grid), другой шаг/действие (продлить УЖЕ
# существующую task-level задачу менеджера, а не создать новую подписку
# при "➕ Добавить авто") — поэтому не переиспользует PERIOD_CHOICES/
# STEP_AWAITING_PERIOD напрямую.
TRUSTED_TASK_PERIOD_CHOICES = (15, 30, 90)

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

# "📋 Мои авто" car-centric (обычный пользователь, см. design report про
# переработку UX) — та же страница в 10 (см. задачу: "существующую
# pagination 10/page сохранить").
_MY_CARS_PAGE_SIZE = 10


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю — Telethon-адаптер (handlers.py) решает,
    какую клавиатуру приложить к тексту, исходя из этих полей.

    check_now_options — (subscription_id, car_number) для построения
    списка выбора (см. reader/public_bot/keyboards.py::options_keyboard) —
    subscription_id в callback_data ПУБЛИЧЕН и НЕ является доказательством
    авторизации сам по себе: владение всегда перепроверяется server-side
    при нажатии (см. SubscriptionService.get_actionable_subscription), а
    не на этапе показа списка (см. design report Stage 4: "никаких
    действий с чужими subscriptions по callback payload").

    trusted_stop_options/trusted_stop_confirm_task_id — task-level ⛔ для
    trusted-оператора (см. design report про пересмотр архитектуры) —
    (task_id, car_number) вместо (subscription_id, car_number); тот же
    принцип: task_id публичен, авторизация — ИСКЛЮЧИТЕЛЬНО server-side
    повторная проверка (is_trusted + задача существует и активна, см.
    SubscriptionService.get_active_task_for_trusted_admin).

    trusted_tasks_page/trusted_tasks_total_pages — 📋 Мои авто пагинация
    для trusted-оператора (см. design report) — page (0-indexed) публичен
    и НЕ является доказательством авторизации: is_trusted() перепроверяется
    на каждом callback заново, а page вне диапазона клампится сервером.

    trusted_tasks_page_options — (task_id, car_number, is_on) на строку
    "📋 Мои авто" (см. design report про per-car ON/OFF toggle) — is_on
    решает, какую переключатель-кнопку показать рядом с car_number
    (🟢 ON/⚪ OFF, см. reader/public_bot/keyboards.py::
    trusted_tasks_page_keyboard). task_id публичен и НЕ является
    доказательством авторизации сам по себе — тот же принцип, что и везде
    в этом модуле (is_trusted() + существование задачи перепроверяются
    server-side на каждом действии).

    trusted_task_off_id/trusted_task_off_page — экран "▶️ Продолжить
    мониторинг" (см. design report) — показывается после ⚪ OFF в списке
    ИЛИ сразу после ON -> OFF toggle (см. handle_trusted_task_toggle).
    page — куда вернёт "⬅️ Назад" (на "📋 Мои авто", ту же страницу).

    trusted_task_period_id/trusted_task_period_page — экран выбора срока
    15/30/90 дней (см. design report, TRUSTED_TASK_PERIOD_CHOICES) — "⬅️
    Назад" отсюда возвращает на экран "▶️ Продолжить мониторинг" (тот же
    task_id/page, см. handle_trusted_task_toggle — переиспользуется как
    no-op показ, задача уже OFF).

    my_cars_page_options/my_cars_page/my_cars_total_pages — car-centric
    "📋 Мои авто" для ОБЫЧНОГО (не-trusted) пользователя (см. design
    report про переработку UX): (subscription_id, label) для списка
    автомобилей-кнопок текущей страницы + сама страница/их общее число —
    тот же принцип пагинации, что и у trusted_tasks_*, но subscription-
    based и полностью отдельными callback'ами (см. keyboards.py).

    car_detail_subscription_id/car_detail_monitoring_state/car_detail_page —
    карточка конкретного автомобиля (см. handle_my_car_open/
    handle_my_car_turn_on/handle_my_car_turn_off) — monitoring_state:
    "ON"/"OFF"/"ИСТЁК" (см. texts.car_monitoring_state) решает, показать
    ли ⏸/▶️-переключатель и какой именно; page — куда вернёт "⬅️ Назад".

    car_delete_confirm_subscription_id/car_delete_confirm_page — "⚠️
    Удалить {car} из списка?" (см. handle_my_car_delete_prompt) — ОТДЕЛЬНОЕ
    промежуточное подтверждение перед soft delete, ничего ещё не удаляет.

    cta_buttons — коммерческие CTA-кнопки под manual "🔎 Проверить сейчас"
    (см. design report про UX manual check) — ТОЛЬКО когда реальный
    владелец увидел хотя бы один штраф своей машины (см.
    handle_check_now_choice), той же формы (label, url), что и
    reader/public_bot/keyboards.py::owner_fine_cta_buttons, но БЕЗ импорта
    Telethon Button здесь — conversation.py намеренно ничего не знает о
    Telegram API, только handlers.py конвертирует эти пары в реальные
    Button.url(...).

    show_turkey_bot_link — True ТОЛЬКО в ответ на нажатие
    "🇹🇷 Штрафы Турции" в главном меню (см. design report "унификация UI":
    переход в Turkey-бот теперь reply-кнопка, а не автоматическое
    companion-сообщение) — reader/public_bot/handlers.py прикрепляет
    turkey_bot_link_keyboard() (inline URL-кнопка) ИМЕННО к этому ответу,
    никогда сам по себе на экране главного меню."""

    text: str
    show_main_menu: bool = False
    show_period_buttons: bool = False
    show_add_client_decision_buttons: bool = False
    check_now_options: list[tuple[int, str]] | None = None
    trusted_stop_options: list[tuple[int, str]] | None = None
    trusted_stop_confirm_task_id: int | None = None
    trusted_stop_confirm_button_label: str | None = None
    trusted_tasks_page: int | None = None
    trusted_tasks_total_pages: int | None = None
    trusted_tasks_page_options: list[tuple[int, str, bool]] | None = None
    trusted_task_off_id: int | None = None
    trusted_task_off_page: int | None = None
    trusted_task_period_id: int | None = None
    trusted_task_period_page: int | None = None
    my_cars_page_options: list[tuple[int, str]] | None = None
    my_cars_page: int | None = None
    my_cars_total_pages: int | None = None
    car_detail_subscription_id: int | None = None
    car_detail_monitoring_state: str | None = None
    car_detail_page: int | None = None
    car_delete_confirm_subscription_id: int | None = None
    car_delete_confirm_page: int | None = None
    cta_buttons: list[list[tuple[str, str]]] | None = None
    show_turkey_bot_link: bool = False


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: BotConversationStateRepository,
        subscription_service: SubscriptionService,
        statistics_service: BotStatisticsService,
        *,
        tz: ZoneInfo,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
        payment_help_contact_username: str = "tplgee",
    ):
        self._states = conversation_state_repository
        self._subscriptions = subscription_service
        self._statistics = statistics_service
        self._tz = tz
        # frozenset(...) на входе — на случай, если вызывающий код (см.
        # reader/public_bot/main.py) передал обычный list из config.yaml.
        self._trusted_operator_user_ids = frozenset(trusted_operator_user_ids)
        # Destination CTA-кнопок под manual "🔎 Проверить сейчас" — тот же
        # config (settings.public_bot.payment_help_contact_username), что и
        # у ClientDeliveryService/owner_fine_cta_buttons, не hardcoded.
        self._payment_help_contact_username = payment_help_contact_username

    def _owner_cta_buttons(self) -> list[list[tuple[str, str]]]:
        url = f"https://t.me/{self._payment_help_contact_username}"
        return [[("💳 Оплатить в рублях", url), ("🚗 ОСАГО Грузии", url)]]

    def _today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self._tz).date()

    def _is_trusted(self, telegram_user_id: int) -> bool:
        """Единственная проверка авторизации trusted-режима — ТОЛЬКО по
        numeric telegram_user_id (см. design report: "authorization
        trusted mode только по numeric Telegram user_id из config"),
        никогда по username."""
        return telegram_user_id in self._trusted_operator_user_ids

    def is_trusted(self, telegram_user_id: int) -> bool:
        """Публичная обёртка над _is_trusted() — нужна reader/public_bot/
        handlers.py (см. "📊 Статистика"), чтобы решить, добавлять ли
        кнопку статистики в главное меню (main_menu_keyboard(
        include_statistics=...)), НЕ дублируя саму проверку trusted-
        статуса вне ConversationController."""
        return self._is_trusted(telegram_user_id)

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
            return self._format_my_cars_page_reply(telegram_user_id, 0)

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
            return self._build_check_now_picker_reply(telegram_user_id)

        if stripped_text == texts.STOP_LABEL:
            self._states.clear(chat_id)
            if not self._is_trusted(telegram_user_id):
                # Для обычного пользователя эта кнопка больше не
                # показывается в главном меню вовсе (см.
                # reader/public_bot/keyboards.py::main_menu_keyboard) — car-
                # centric ON/OFF в "📋 Мои авто" её полностью заменяет.
                # Текст всё же можно отправить вручную (например, из
                # старого чата) — безопасный отказ вместо падения/подсказки
                # про несуществующий flow, та же защита, что и у
                # "📊 Статистика" ниже.
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
            return self._build_trusted_stop_picker_reply()

        if stripped_text == texts.TURKEY_BOT_LINK_LABEL:
            # См. design report "унификация UI" — штатный способ перехода
            # для reply-кнопки (не URL-кнопка сама по себе, Telegram этого
            # не позволяет для reply-клавиатуры): распознаём нажатие как
            # обычный текст и отвечаем ОТДЕЛЬНЫМ сообщением с inline
            # URL-кнопкой (см. BotReply.show_turkey_bot_link докстрок и
            # reader/public_bot/keyboards.py::turkey_bot_link_keyboard).
            # Доступно ВСЕМ пользователям одинаково, не trusted-gated.
            self._states.clear(chat_id)
            return BotReply(text=texts.TURKEY_BOT_LINK_TEXT, show_turkey_bot_link=True)

        if stripped_text == texts.STATISTICS_LABEL:
            self._states.clear(chat_id)
            if not self._is_trusted(telegram_user_id):
                # Кнопка обычному пользователю никогда не показывается
                # (см. main_menu_keyboard(include_statistics=...)), но
                # текст с тем же содержимым можно отправить вручную — та
                # же защита, что и у task-level admin API (см. модуль
                # docstring): безопасный отказ, статистика не раскрывается,
                # ничего не падает и не логируется как ошибка.
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
            return self._format_statistics_reply()

        return None

    def _format_statistics_reply(self) -> BotReply:
        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        return BotReply(text=texts.format_statistics(stats), show_main_menu=True)

    def _format_trusted_tasks_page_reply(self, page: int) -> BotReply:
        """"📋 Мои авто" для trusted-оператора — ОДНА страница ВСЕХ
        fine_monitoring_tasks, ЛЮБОГО статуса (см. design report про
        per-car ON/OFF toggle: менеджер должен видеть и OFF-машины —
        hard cap "первые 50" убран — пагинация по _TRUSTED_TASKS_PAGE_SIZE
        вместо него), subscription для отображения не требуется вовсе.

        page — ЛЮБОЕ int (в т.ч. отрицательное/за пределами общего числа
        страниц, см. design report: "page из callback нельзя считать
        authorization") — клампится здесь, а не у вызывающего кода,
        поэтому единственная точка, где может быть баг с границами."""
        total = self._subscriptions.count_all_tasks()
        if total == 0:
            return BotReply(text=texts.NO_ACTIVE_TASKS_TEXT)

        total_pages = -(-total // _TRUSTED_TASKS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        tasks = self._subscriptions.list_all_tasks_page(page=page, page_size=_TRUSTED_TASKS_PAGE_SIZE)

        options = [
            (task.id, task.car_number, task.status == "active") for task in tasks
        ]
        return BotReply(
            text=texts.format_trusted_tasks_page(tasks, page=page, total_pages=total_pages),
            trusted_tasks_page=page,
            trusted_tasks_total_pages=total_pages,
            trusted_tasks_page_options=options,
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

    def _build_check_now_picker_reply(self, telegram_user_id: int) -> BotReply:
        """Список авто, с которыми telegram_user_id может действовать
        через 🔎 (свои + delegated, которые он создал, см.
        SubscriptionService.list_actionable_subscriptions) — subscription_id
        в кнопках, не car_number, чтобы не полагаться на уникальность
        номера при последующей server-side проверке владения. Единственный
        оставшийся потребитель этого picker'а (⛔ для обычного пользователя
        заменена car-centric ON/OFF, см. design report п.8)."""
        subscriptions = self._subscriptions.list_actionable_subscriptions(
            telegram_user_id, today=self._today(),
        )
        if not subscriptions:
            return BotReply(text=texts.NO_ACTIONABLE_CARS_TEXT)

        options = [(s.id, s.car_number) for s in subscriptions]
        return BotReply(text=texts.CHECK_NOW_PICK_PROMPT, check_now_options=options)

    def _format_my_cars_page_reply(self, telegram_user_id: int, page: int) -> BotReply:
        """Car-centric "📋 Мои авто" (см. design report про переработку
        UX) — ОДНА страница ВСЕХ подписок этого пользователя (любого
        статуса, кроме 'archived', см. SubscriptionService.list_my_cars),
        каждая — отдельная кнопка (см. keyboards.py::my_cars_page_keyboard).
        page клампится здесь же, как и у _format_trusted_tasks_page_reply —
        forged/устаревший page просто показывает ближайшую валидную
        страницу, а не ошибку."""
        today = self._today()
        cars = self._subscriptions.list_my_cars(telegram_user_id)
        if not cars:
            return BotReply(text=texts.NO_CARS_TEXT, show_main_menu=True)

        total_pages = -(-len(cars) // _MY_CARS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        start = page * _MY_CARS_PAGE_SIZE
        page_cars = cars[start:start + _MY_CARS_PAGE_SIZE]

        options = [(car.id, texts.format_car_button_label(car, today)) for car in page_cars]
        return BotReply(
            text=texts.MY_CARS_HEADER,
            my_cars_page_options=options,
            my_cars_page=page,
            my_cars_total_pages=total_pages,
        )

    def _format_car_detail_reply(self, subscription, page: int) -> BotReply:
        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(subscription, today)
        return BotReply(
            text=texts.format_car_details(subscription, today),
            car_detail_subscription_id=subscription.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

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
        # CTA — ТОЛЬКО реальному владельцу и только когда есть хотя бы один
        # штраф (см. design report: "CTA — только owner", не trusted-
        # оператору, управляющему чужой delegated-подпиской).
        cta = self._owner_cta_buttons() if outcome.is_owner and outcome.fines else None
        return BotReply(text=texts.format_check_now_result(outcome), cta_buttons=cta)

    def handle_stop_cancel(self) -> BotReply:
        """"Отмена" на ⛔-подтверждении — ОБЩАЯ и для trusted task-level
        stop (см. STOP_NO/handle_trusted_stop_confirm), единственный
        оставшийся потребитель после удаления subscription-based ⛔ для
        обычного пользователя (см. design report п.8)."""
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    # ---- car-centric "📋 Мои авто": открыть карточку/ON/OFF/Delete (см.
    # design report про переработку UX) — ОБЫЧНЫЙ (не-trusted) пользователь,
    # subscription-based, без conversation_state (все шаги — inline-кнопки,
    # владение перепроверяется на КАЖДОМ шаге заново через
    # SubscriptionService.get_actionable_subscription — тот же принцип, что
    # и у 🔎/⛔ выше: subscription_id/page в callback_data публичны и НЕ
    # являются доказательством владения сами по себе). ----

    def handle_my_cars_page(self, page: int, *, telegram_user_id: int) -> BotReply:
        """Навигация ◀️/индикатор/▶️ — telegram_user_id сам определяет,
        чьи автомобили показывать (list_my_cars уже скопирован по нему на
        уровне SQL, см. SubscriptionService.list_my_cars), поэтому здесь
        нечего "не авторизовать" отдельно — в отличие от открытия
        КОНКРЕТНОГО автомобиля (см. handle_my_car_open), page сам по себе
        не указывает ни на чей ресурс."""
        return self._format_my_cars_page_reply(telegram_user_id, page)

    def handle_my_car_open(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — подписка не найдена/не принадлежит telegram_user_id (см.
        SubscriptionService.get_actionable_subscription) — Telethon-адаптер
        (handlers.py) отвечает алертом, ничего не открывает."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return self._format_car_detail_reply(subscription, page)

    def handle_my_car_turn_off(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """ON -> OFF (см. SubscriptionService.turn_off_car). None —
        подписка не найдена/не принадлежит telegram_user_id — та же
        server-side проверка, что и у открытия карточки. Отдельно от
        None — CAR_ACTION_FAILED_TEXT, если подписка НАЙДЕНА (владение ОК),
        но действие не удалось (например, она уже не 'active' — гонка с
        другим действием/другим устройством того же пользователя)."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None

        updated = self._subscriptions.turn_off_car(subscription_id, telegram_user_id=telegram_user_id)
        if updated is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        # Подтверждение + сразу обновлённая карточка (см. design report:
        # "после ON/OFF также обновить карточку/список так, чтобы
        # пользователь сразу видел новое состояние") — один экран, не два
        # последовательных сообщения.
        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(updated, today)
        text = texts.format_turn_off_success(updated.car_number) + "\n\n" + texts.format_car_details(updated, today)
        return BotReply(
            text=text,
            car_detail_subscription_id=updated.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

    def handle_my_car_turn_on(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """OFF -> ON (см. SubscriptionService.turn_on_car — реактивирует
        существующую подписку, никогда не создаёт вторую)."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None

        updated = self._subscriptions.turn_on_car(
            subscription_id, telegram_user_id=telegram_user_id, today=self._today(),
        )
        if updated is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(updated, today)
        text = texts.format_turn_on_success(updated.car_number) + "\n\n" + texts.format_car_details(updated, today)
        return BotReply(
            text=text,
            car_detail_subscription_id=updated.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

    def handle_my_car_delete_prompt(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """Первый шаг — показывает подтверждение, ЕЩЁ НИЧЕГО не удаляет
        (см. design report: "🗑 Удалить автомобиль — отдельное от OFF
        действие. Сразу ничего не удалять")."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return BotReply(
            text=texts.format_delete_confirm_prompt(subscription.car_number),
            car_delete_confirm_subscription_id=subscription.id,
            car_delete_confirm_page=page,
        )

    def handle_my_car_delete_confirm(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply:
        """Финальный шаг — владение перепроверяется ЗАНОВО здесь (а не
        только доверяется тому, что пользователь дошёл до этого экрана,
        см. design report: "нельзя подменить owner/user_id через callback
        payload") — SubscriptionService.delete_car() сам делает эту
        проверку ещё раз через get_actionable_subscription. Возвращает
        ОБНОВЛЁННЫЙ список "Мои авто" (см. design report: "после удаления
        автомобиль исчезнет из «Мои авто»"), а не отдельное сообщение-
        квитанцию — так пользователь сразу видит актуальный список."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        deleted = self._subscriptions.delete_car(subscription_id, telegram_user_id=telegram_user_id)
        if not deleted:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        return self._format_my_cars_page_reply(telegram_user_id, page)

    def handle_my_car_delete_cancel(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """"Отмена" на удалении — возвращает к карточке того же
        автомобиля, БЕЗ каких-либо изменений (см. design report: "Cancel
        preserves car")."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return self._format_car_detail_reply(subscription, page)

    # ---- manager-facing "📋 Мои авто" ON/OFF + "▶️ Продолжить мониторинг"
    # 15/30/90 дней (см. design report про per-car monitoring toggle) —
    # task-level, ПРЯМО как ⚪/⛔ ниже (fine_monitoring_tasks, subscription
    # НЕ требуется), is_trusted() перепроверяется ЗАНОВО на каждом шаге по
    # РЕАЛЬНОМУ telegram_user_id — task_id/days/page в callback_data
    # публичны и НЕ являются доказательством авторизации сами по себе. ----

    def handle_trusted_task_toggle(
        self, task_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """🟢 ON / ⚪ OFF кнопка в списке (см. design report):
          - задача сейчас ON (status='active') — выключает её (см.
            SubscriptionService.stop_task_for_trusted_admin, тот же метод,
            что и у ⛔, включая остановку client-подписок задачи) и
            показывает экран "▶️ Продолжить мониторинг" с подтверждением
            выключения;
          - задача уже OFF — ничего не меняет, просто показывает тот же
            экран (см. design report п.2: "⚪ OFF открывает следующий
            экран") — ЭТО ЖЕ переиспользуется как "⬅️ Назад" с экрана
            выбора 15/30/90 (см. keyboards.py::
            trusted_task_period_choice_keyboard — там кнопка "⬅️ Назад"
            кодирует ровно этот же callback).

        None — не trusted, ИЛИ задача с таким task_id не существует вовсе
        (см. SubscriptionService.get_task_for_trusted_admin — в отличие от
        get_active_task_for_trusted_admin, здесь допустим ЛЮБОЙ статус,
        задача просто должна существовать)."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        if task is None:
            return None

        if task.status == "active":
            if not self._subscriptions.stop_task_for_trusted_admin(task_id):
                return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)
            task = self._subscriptions.get_task_for_trusted_admin(task_id)
            if task is None:
                return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)
            text = (
                texts.format_trusted_task_turn_off_success(task.car_number)
                + "\n\n" + texts.format_trusted_task_off_detail(task)
            )
        else:
            text = texts.format_trusted_task_off_detail(task)

        return BotReply(text=text, trusted_task_off_id=task.id, trusted_task_off_page=page)

    def handle_trusted_task_continue(
        self, task_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """"▶️ Продолжить мониторинг" на экране OFF-детали (см. design
        report) — показывает выбор срока 15/30/90 дней, ЕЩЁ НИЧЕГО не
        меняет (тот же принцип, что и у handle_my_car_delete_prompt:
        промежуточный экран перед реальным действием). None — не trusted,
        ИЛИ задача не существует."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        if task is None:
            return None

        return BotReply(
            text=texts.TRUSTED_TASK_PERIOD_PROMPT,
            trusted_task_period_id=task.id, trusted_task_period_page=page,
        )

    def handle_trusted_task_period_choice(
        self, task_id: int, days: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """Финальный шаг — 15/30/90 дней (см. design report,
        TRUSTED_TASK_PERIOD_CHOICES): включает мониторинг ИМЕННО этой
        задачи на выбранный срок (см. SubscriptionService.resume_task_for_
        trusted_admin — тот же return_to_active_monitoring(), что и у
        архивных задач, никакой отдельной реализации), затем возвращает
        менеджера в ОБНОВЛЁННОЕ "📋 Мои авто" (та же page, см. design
        report: "После этого вернуть менеджера в 📋 Мои авто") с
        подтверждением ПЕРЕД списком — тот же принцип "подтверждение +
        сразу обновлённый список в одном экране", что и у
        handle_my_car_turn_off/on (подтверждение + карточка), только здесь
        подтверждение + список, а не подтверждение + карточка.

        None — не trusted, ИЛИ days не входит в TRUSTED_TASK_PERIOD_CHOICES
        (defensive — keyboards.py::decode_trusted_task_period_callback уже
        не пропускает произвольные значения, но проверяется и здесь, тот
        же принцип "allowlist, не любое целое число", что и у
        decode_period_callback)."""
        if not self._is_trusted(telegram_user_id):
            return None
        if days not in TRUSTED_TASK_PERIOD_CHOICES:
            return None

        resumed = self._subscriptions.resume_task_for_trusted_admin(
            task_id, days=days, today=self._today(),
        )
        if resumed is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        confirmation = texts.format_trusted_task_resume_success(
            resumed.car_number, days, resumed.end_date,
        )
        list_reply = self._format_trusted_tasks_page_reply(page)
        return BotReply(
            text=confirmation + "\n\n" + list_reply.text,
            trusted_tasks_page=list_reply.trusted_tasks_page,
            trusted_tasks_total_pages=list_reply.trusted_tasks_total_pages,
            trusted_tasks_page_options=list_reply.trusted_tasks_page_options,
        )

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
