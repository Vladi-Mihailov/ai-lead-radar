"""ConversationController Turkey test-clone бота — UNIFIED UX (см. design
report "Перестроить UX Turkey test bot"): USER -> CAR -> UNIFIED CHECK ->
MONITORING. GİB/Avrasya/KGM — ВНУТРЕННИЕ providers (см.
reader/turkey_bot/unified/check_service.py::UnifiedTurkeyCheckService)
— пользователь их никогда не выбирает явно, ни для ручной проверки, ни
для мониторинга.

CAPTCHA — целиком concern UnifiedTurkeyCheckService/CaptchaResolver (см.
reader/turkey_bot/unified/captcha_resolver.py) — этот файл про неё
вообще ничего не знает (ни OCR, ни показа картинки пользователю, см.
design report решение п.7: "captcha_code — входной технический параметр").

НИЧЕГО не знает про Telethon (та же граница, что и
reader/public_bot/conversation.py) — reader/turkey_bot/handlers.py
конвертирует BotReply в реальные Telegram-вызовы.

Разделение (см. design report п.15):
  UI/conversation (этот файл)
        -> UnifiedTurkeyCheckService (ручная проверка)
        -> TurkeyMonitoringService (плановая проверка, отдельный вызывающий
           код — reader/turkey_bot/monitoring/scheduler_job.py, НЕ
           этот файл)
  Оба используют ОДИН UnifiedTurkeyCheckService (см. design report п.12)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from reader.turkey_bot import texts
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.debt_refresh_service import (
    TurkeyDebtRefreshService,
    TurkeyRefreshAlreadyInProgressError,
)
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.monitoring.scheduler_job import next_monitoring_slot
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import OverallStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

_DEFAULT_TZ = ZoneInfo("UTC")

# Единственный оставшийся шаг диалога (см. design report — GİB/Avrasya/KGM
# провайдер-специфичные шаги/CAPTCHA-шаг полностью убраны из UI).
_STEP_AWAITING_NEW_CAR_PLATE = "awaiting_new_car_plate"
# Manager/trusted Search (см. задачу "manager/trusted Search") — ввод
# @username/username/номера, см. _handle_search_query_input.
_STEP_AWAITING_SEARCH_QUERY = "awaiting_search_query"

_INITIATOR_MANUAL = "manual"

_CAR_ACTIONS_REQUIRING_CHECK = {"check"}

# См. задачу "Retry orchestration Unified Turkey checks" п.1 — РУЧНАЯ
# проверка допускает до 35 попыток НА ПРОВАЙДЕРА (получить новую captcha
# через provider.refresh_captcha() + новую попытку резолвера) при
# captcha_unavailable/captcha_rejected, прежде чем зафиксировать ERROR
# (см. reader/turkey_bot/unified/check_service.py::UnifiedTurkeyCheckService.
# check — вся orchestration retry живёт там, здесь только выбор
# max_attempts для режима "manual").
_MANUAL_MAX_ATTEMPTS = 35

# manager/trusted-operator "🚗 Мои автомобили" (см. задачу "Реализуем
# manager/trusted 'Мои автомобили' для Turkey bot", референс — Georgian
# bot _TRUSTED_TASKS_PAGE_SIZE) — та же величина страницы.
_MANAGER_CARS_PAGE_SIZE = 10

# Manager/trusted Search (см. задачу "manager/trusted Search" п.11) — та
# же величина страницы, что и _MANAGER_CARS_PAGE_SIZE выше.
_SEARCH_PAGE_SIZE = 10


@dataclass(frozen=True)
class _SearchHit:
    """Одна строка результата manager/trusted Search — ОДНА строка
    turkey_bot_user_cars (см. ConversationController._format_search_results_reply)
    — в отличие от Georgian bot, owner_telegram_user_id здесь ВСЕГДА
    задан (turkey_bot_user_cars.telegram_user_id NOT NULL, нет аналога
    "задача без клиента")."""

    car_id: int
    owner_telegram_user_id: int
    car_number: str


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю (см. design report п.15 — этот файл
    ничего не знает про Telethon-кнопки, только флаги/данные, из которых
    reader/turkey_bot/handlers.py строит реальную клавиатуру).

    my_cars — список автомобилей для inline-клавиатуры "📋 Мои авто"/
    "🔎 Проверить сейчас" (см. keyboards.py::my_cars_list_keyboard/
    check_now_picker_keyboard) — is_check_picker различает, какую из двух
    клавиатур строить (одинаковый список car'ов, разный callback).
    my_cars_monitoring_active — {car.id: active} ТОЛЬКО для my_cars_list_
    keyboard (см. _monitoring_map — ВСЕГДА читается заново из
    TurkeyMonitoringSubscriptionRepository, никогда не кэшируется как
    отдельный UI-state, см. задачу п.1).

    car_card — ОДИН автомобиль, для карточки (см. keyboards.py::
    car_card_keyboard) — car_card_monitoring_active управляет тем, какая
    из двух кнопок (Включить/Отключить мониторинг) показывается.

    car_delete_confirm_car_id — не None ТОЛЬКО на экране подтверждения
    удаления (см. keyboards.py::car_delete_confirm_keyboard) — вместо
    car_card_keyboard на этом шаге показывается Да/Отмена.

    back_to_car_id — не None, когда после действия (проверка/история)
    нужна кнопка возврата к карточке конкретного автомобиля (см. задачу
    п.4: "После результата пользователь должен иметь возможность
    вернуться к карточке автомобиля").

    check_now_confirmation_car_id — не None ТОЛЬКО сразу после успешного
    добавления нового автомобиля (см. keyboards.py::
    add_car_confirmation_keyboard)."""

    text: str
    show_cancel_button: bool = False
    extra_texts: tuple[str, ...] = ()
    show_main_menu: bool = False
    my_cars: tuple[TurkeyUserCar, ...] | None = None
    my_cars_monitoring_active: dict[int, bool] | None = None
    is_check_picker: bool = False
    car_card: TurkeyUserCar | None = None
    car_card_monitoring_active: bool = False
    car_delete_confirm_car_id: int | None = None
    back_to_car_id: int | None = None
    check_now_confirmation_car_id: int | None = None
    cta_buttons: tuple[tuple[str, str], ...] | None = None
    help_keyboard: str | None = None
    show_georgian_bot_link: bool = False

    # manager/trusted-operator "🚗 Мои автомобили" (см. задачу "Реализуем
    # manager/trusted 'Мои автомобили' для Turkey bot" + "унифицировать оба
    # интерфейса", референс — Georgian bot trusted_tasks_page_options/
    # _format_trusted_tasks_page_reply) — ОТДЕЛЬНЫЕ поля от my_cars/
    # my_cars_monitoring_active выше (self-service НЕ трогается).
    # manager_cars_page_options — (car_id, car_number, owner_display,
    # monitoring_active) — РОВНО ТРИ кнопки на строку
    # (keyboards.py::manager_cars_page_keyboard): car_number, готовая
    # "@username"/"—" (см. _format_manager_cars_page_reply/
    # texts.format_owner_username_button — НИКОГДА @None/None/пустая
    # кнопка), 🟢/⚪.
    manager_cars_page_options: list[tuple[int, str, str, bool]] | None = None
    manager_cars_page: int | None = None
    manager_cars_total_pages: int | None = None
    # Read-only детали ОДНОЙ строки manager-списка (см.
    # handle_manager_car_open/texts.format_manager_car_detail) —
    # manager_car_detail_page — куда вернёт "⬅️ Назад" (та же страница
    # списка, см. keyboards.py::manager_car_detail_keyboard).
    manager_car_detail_page: int | None = None

    # Manager/trusted Search (см. задачу "manager/trusted Search") — ТОЛЬКО
    # trusted_operator_user_ids, тот же смысл полей, что и у Georgian bot
    # (reader/public_bot/conversation.py::BotReply) — search_prompt/
    # search_result_shown/search_query_type/search_query/search_page/
    # search_total_pages.
    search_prompt: bool = False
    search_result_shown: bool = False
    search_query_type: str | None = None
    search_query: str | None = None
    search_page: int | None = None
    search_total_pages: int | None = None

    # Trusted-manager "🔄 Проверить авто с задолженностью" (см. задачу
    # "manual Turkey debt refresh") — debt_refresh_available прикрепляет
    # inline-кнопку (см. reader/turkey_bot/handlers.py::
    # _first_message_buttons/_send_reply — та же логика "последнему из
    # extra_texts, если они есть, иначе первому сообщению", что и у
    # show_main_menu); debt_refresh_confirm_car_count — не None ТОЛЬКО на
    # экране подтверждения "Будет проверено автомобилей: N" с [✅
    # Проверить N авто][↩️ Отмена] — ПЕРВОЕ нажатие ничего не запускает, ни
    # одного provider request. is_trusted() перепроверяется заново на
    # каждом шаге (см. handle_debt_refresh_pick/confirm/cancel).
    debt_refresh_available: bool = False
    debt_refresh_confirm_car_count: int | None = None


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: TurkeyConversationStateRepository,
        garage_repository: TurkeyUserCarsRepository,
        run_repository: TurkeyCheckRunRepository,
        subscription_repository: TurkeyMonitoringSubscriptionRepository,
        statistics_service: TurkeyStatisticsService,
        check_service: UnifiedTurkeyCheckService,
        *,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
        tz: ZoneInfo = _DEFAULT_TZ,
        payment_help_contact_username: str = "tplgee",
        debt_refresh_service: TurkeyDebtRefreshService | None = None,
    ):
        self._states = conversation_state_repository
        self._garage = garage_repository
        self._runs = run_repository
        self._subscriptions = subscription_repository
        self._statistics = statistics_service
        self._check_service = check_service
        self._trusted_operator_user_ids = frozenset(trusted_operator_user_ids)
        self._tz = tz
        self._payment_help_contact_username = payment_help_contact_username
        # None — как и everywhere в проекте (см. Georgian bot
        # SubscriptionService/OwnerUsernameResolverLike) — означает "фичи
        # 🔄 Проверить авто с задолженностью вообще нет в этой сборке"
        # (кнопка не показывается, см. handle_statistics). Существующие
        # вызовы ConversationController(...) без этого параметра (тесты)
        # продолжают работать бит в бит как раньше.
        self._debt_refresh = debt_refresh_service

    def _debt_cta_buttons(self) -> tuple[tuple[str, str], ...]:
        """Та же destination (payment_help_contact_username), что и
        раньше (см. design report п.14 — конфигурация контакта не
        менялась)."""
        url = f"https://t.me/{self._payment_help_contact_username}"
        return (("💳 Оплатить в рублях", url), ("🚗 ОСАГО Турции", url))

    def _is_trusted(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self._trusted_operator_user_ids

    def is_trusted(self, telegram_user_id: int) -> bool:
        """Публичная обёртка — нужна reader/turkey_bot/handlers.py,
        чтобы решить, показывать ли STATISTICS_LABEL/STOP_MONITORING_LABEL
        в главном меню (см. reader/turkey_bot/keyboards.py::
        main_menu_keyboard)."""
        return self._is_trusted(telegram_user_id)

    # ---- Верхнеуровневый роутинг ----

    async def handle_text(self, text: str, *, chat_id: int, telegram_user_id: int) -> BotReply:
        stripped = text.strip()
        lowered = stripped.lower()

        if lowered == "/start":
            self._states.clear(chat_id)
            return BotReply(text=texts.WELCOME_TEXT, show_main_menu=True)

        if lowered == "/cancel":
            return await self.handle_cancel(chat_id=chat_id)

        if stripped == texts.ADD_CAR_LABEL:
            return self.handle_add_car_start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.MY_CARS_LABEL:
            # Manager/trusted-operator видит ВСЕ автомобили ВСЕХ
            # пользователей (см. задачу "Реализуем manager/trusted 'Мои
            # автомобили' для Turkey bot", референс — Georgian bot
            # _handle_menu_label) — обычный пользователь получает
            # handle_my_cars() БЕЗ ИЗМЕНЕНИЙ, как и раньше.
            if self._is_trusted(telegram_user_id):
                return self._format_manager_cars_page_reply(0)
            return self.handle_my_cars(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.CHECK_NOW_LABEL:
            return self.handle_check_now_start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.STATISTICS_LABEL and self._is_trusted(telegram_user_id):
            return self.handle_statistics(chat_id=chat_id)

        if stripped == texts.STOP_MONITORING_LABEL and self._is_trusted(telegram_user_id):
            return self.handle_stop_monitoring(chat_id=chat_id)

        if stripped == texts.SEARCH_LABEL and self._is_trusted(telegram_user_id):
            return self.handle_search_start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.HELP_LABEL:
            return self.handle_help(chat_id=chat_id)

        if stripped == texts.GEORGIAN_BOT_LINK_LABEL:
            return self.handle_georgian_bot_link(chat_id=chat_id)

        state = self._states.get(chat_id)
        if state is not None and state.step == _STEP_AWAITING_NEW_CAR_PLATE:
            return await self._handle_new_car_plate(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        if state is not None and state.step == _STEP_AWAITING_SEARCH_QUERY:
            return self._handle_search_query_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        # Голый ввод номера БЕЗ предварительного нажатия "➕ Добавить авто"
        # (см. design report: тот же принцип "bare plate still works",
        # что и в старом UX) — трактуется как то же "добавить автомобиль".
        return await self._handle_new_car_plate(
            stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
        )

    async def handle_cancel(self, *, chat_id: int) -> BotReply:
        had_state = self._states.get(chat_id) is not None
        self._states.clear(chat_id)
        if not had_state:
            return BotReply(text=texts.NOTHING_TO_CANCEL_TEXT, show_main_menu=True)
        return BotReply(text=texts.CANCEL_CONFIRM_TEXT, show_main_menu=True)

    # ---- ➕ Добавить авто (см. design report п.2) ----

    def handle_add_car_start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_NEW_CAR_PLATE,
        )
        return BotReply(text=texts.ASK_PLATE_FOR_NEW_CAR_TEXT, show_cancel_button=True)

    async def _handle_new_car_plate(
        self, raw_plate: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        plate = normalize_plate(raw_plate)
        if plate is None:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_NEW_CAR_PLATE,
            )
            return BotReply(text=texts.INVALID_PLATE_TEXT, show_cancel_button=True)

        self._states.clear(chat_id)
        already_existed = self._garage.get_owned_car_by_number(telegram_user_id, plate) is not None
        car = self._garage.add_car(telegram_user_id=telegram_user_id, car_number=plate)

        template = texts.CAR_ALREADY_EXISTS_TEMPLATE if already_existed else texts.CAR_ADDED_TEMPLATE
        return BotReply(
            text=template.format(plate=plate),
            check_now_confirmation_car_id=car.id,
        )

    # ---- 🚗 Мои автомобили / 🔎 Проверить сейчас (адаптация car-centric
    # UX Georgian bot, см. задачу "Адаптация car-centric UX Georgian bot"
    # п.1-п.9) ----

    def _monitoring_map(self, cars: tuple[TurkeyUserCar, ...], *, telegram_user_id: int) -> dict[int, bool]:
        """ВСЕГДА читает РЕАЛЬНОЕ состояние turkey_monitoring_subscriptions
        (см. задачу п.1: "Не хранить ON/OFF отдельно как UI-state") — ни
        ConversationController, ни BotReply не кэшируют это между
        вызовами."""
        result: dict[int, bool] = {}
        for car in cars:
            subscription = self._subscriptions.get(telegram_user_id=telegram_user_id, plate=car.car_number)
            result[car.id] = subscription is not None and subscription.active
        return result

    def handle_my_cars(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        self._states.clear(chat_id)
        cars = tuple(self._garage.list_cars(telegram_user_id))
        if not cars:
            return BotReply(text=texts.EMPTY_MY_CARS_TEXT, show_main_menu=True)

        return BotReply(
            text=texts.MY_CARS_HEADER, my_cars=cars, is_check_picker=False,
            my_cars_monitoring_active=self._monitoring_map(cars, telegram_user_id=telegram_user_id),
        )

    def handle_check_now_start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        self._states.clear(chat_id)
        cars = tuple(self._garage.list_cars(telegram_user_id))
        if not cars:
            return BotReply(text=texts.EMPTY_MY_CARS_TEXT, show_main_menu=True)
        return BotReply(text=texts.MY_CARS_LABEL, my_cars=cars, is_check_picker=True)

    # ---- manager/trusted-operator "🚗 Мои автомобили" (см. задачу
    # "Реализуем manager/trusted 'Мои автомобили' для Turkey bot",
    # референс — Georgian bot _format_trusted_tasks_page_reply/
    # handle_trusted_tasks_page/handle_trusted_task_open/
    # handle_trusted_task_toggle) — is_trusted() перепроверяется ЗАНОВО на
    # КАЖДОМ из трёх методов ниже (вход через меню клампит это один раз,
    # но pagination/open/toggle callback'и приходят НАПРЯМУЮ из Telegram —
    # см. задачу п.8.H: "проверка trusted должна быть не только при входе
    # через меню, но и на manager pagination/action callbacks", тот же
    # authorization-инвариант, что и везде в проекте: callback_data
    # публичен и НЕ является доказательством авторизации сам по себе). ----

    def _owner_username_display(self, telegram_user_id: int) -> str | None:
        """Auto-captured username владельца ОДНОЙ строки manager-списка —
        см. задачу п.4/п.5: "Username получать из turkey_bot_known_users
        по telegram_user_id ВЛАДЕЛЬЦА машины" (car.telegram_user_id, а НЕ
        telegram_user_id менеджера, см. _format_manager_cars_page_reply
        ниже, где именно car.telegram_user_id сюда передаётся) — ТА ЖЕ
        актуальная запись, что обновляется на каждое сообщение владельца
        боту (см. reader/turkey_bot/handlers.py::_record_known_user), а не
        историческое значение откуда-либо ещё."""
        return self._statistics.get_known_username(telegram_user_id)

    def _format_manager_cars_page_reply(self, page: int) -> BotReply:
        """Manager-facing "🚗 Мои автомобили" — ОДНА страница ВСЕХ строк
        turkey_bot_user_cars, ЛЮБОГО владельца, БЕЗ dedup по car_number
        (см. задачу п.2: "если один и тот же автомобиль добавлен разными
        пользователями, manager должен видеть отдельные строки"). page —
        ЛЮБОЕ int (форматированный/устаревший callback — см. задачу
        п.8.G) — клампится здесь же, тот же приём, что и у Georgian bot
        _format_trusted_tasks_page_reply."""
        total = self._garage.count_all()
        if total == 0:
            return BotReply(text=texts.MANAGER_CARS_EMPTY_TEXT)

        total_pages = -(-total // _MANAGER_CARS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        cars = self._garage.list_all_page(
            offset=page * _MANAGER_CARS_PAGE_SIZE, limit=_MANAGER_CARS_PAGE_SIZE,
        )

        options = []
        for car in cars:
            username = self._owner_username_display(car.telegram_user_id)
            owner_display = texts.format_owner_username_button(username)
            subscription = self._subscriptions.get(
                telegram_user_id=car.telegram_user_id, plate=car.car_number,
            )
            monitoring_active = subscription is not None and subscription.active
            options.append((car.id, car.car_number, owner_display, monitoring_active))

        return BotReply(
            text=texts.format_manager_cars_page(page=page, total_pages=total_pages),
            manager_cars_page_options=options,
            manager_cars_page=page,
            manager_cars_total_pages=total_pages,
        )

    def handle_manager_cars_page(self, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Навигация ◀️/индикатор/▶️ — None, только если telegram_user_id
        НЕ trusted (см. задачу п.8.H) — сам page не может быть
        "невалидным", клампится в _format_manager_cars_page_reply."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self._format_manager_cars_page_reply(page)

    def handle_manager_car_open(self, car_id: int, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Левая кнопка строки — read-only детали (см.
        texts.format_manager_car_detail) — None, если НЕ trusted ИЛИ
        car_id не существует (тот же принцип, что и handle_car_open, но
        БЕЗ ownership-фильтра — get_any_car(), см. задачу: менеджер должен
        видеть чужие машины)."""
        if not self._is_trusted(telegram_user_id):
            return None
        car = self._garage.get_any_car(car_id)
        if car is None:
            return None

        username = self._owner_username_display(car.telegram_user_id)
        subscription = self._subscriptions.get(
            telegram_user_id=car.telegram_user_id, plate=car.car_number,
        )
        monitoring_active = subscription is not None and subscription.active
        return BotReply(
            text=texts.format_manager_car_detail(
                car, owner_username=username, monitoring_active=monitoring_active,
            ),
            manager_car_detail_page=page,
        )

    def handle_manager_car_toggle(self, car_id: int, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Правая кнопка строки — переключает мониторинг НАПРЯМУЮ (см.
        задачу п.6: "сохрани существующую Turkey семантику monitoring/
        status" — тот же прямой toggle без промежуточного экрана, что и
        self-service _enable_monitoring/_disable_monitoring, никакого
        нового "выбора периода" — Turkey monitoring бессрочен). Действует
        от имени ВЛАДЕЛЬЦА (car.telegram_user_id/car.telegram_user_id как
        chat_id — приватный чат с ботом, chat_id == user_id, тот же
        инвариант, что и во всей остальной Turkey-схеме), НЕ от имени
        менеджера — см. задачу п.5: "не подменять owner вызывающим manager
        user_id". Возвращает ТУ ЖЕ страницу списка с уже обновлённым
        статусом. None — НЕ trusted ИЛИ car_id не существует."""
        if not self._is_trusted(telegram_user_id):
            return None
        car = self._garage.get_any_car(car_id)
        if car is None:
            return None

        subscription = self._subscriptions.get(
            telegram_user_id=car.telegram_user_id, plate=car.car_number,
        )
        if subscription is not None and subscription.active:
            self._subscriptions.disable(telegram_user_id=car.telegram_user_id, plate=car.car_number)
        else:
            next_check_at = next_monitoring_slot(datetime.now(timezone.utc))
            self._subscriptions.enable(
                telegram_user_id=car.telegram_user_id, telegram_chat_id=car.telegram_user_id,
                plate=car.car_number, next_check_at=next_check_at,
            )
        return self._format_manager_cars_page_reply(page)

    def handle_car_open(self, car_id: int, *, telegram_user_id: int) -> BotReply | None:
        """None — car_id не существует ИЛИ принадлежит другому
        пользователю (см. TurkeyUserCarsRepository.get_owned_car) —
        reader/turkey_bot/handlers.py должен показать общий
        "неизвестная кнопка" alert."""
        car = self._garage.get_owned_car(car_id, telegram_user_id=telegram_user_id)
        if car is None:
            return None
        return self._render_car_card(car, telegram_user_id=telegram_user_id)

    def handle_car_open_by_plate(self, plate: str, *, telegram_user_id: int) -> BotReply | None:
        """Используется кнопкой "🔎 Подробнее" в уведомлениях мониторинга
        (см. reader/turkey_bot/keyboards.py::
        decode_car_open_by_plate_callback) — та же ownership-проверка, что
        и handle_car_open, просто по (telegram_user_id, plate) вместо
        car_id."""
        car = self._garage.get_owned_car_by_number(telegram_user_id, plate)
        if car is None:
            return None
        return self._render_car_card(car, telegram_user_id=telegram_user_id)

    def _render_car_card(self, car: TurkeyUserCar, *, telegram_user_id: int) -> BotReply:
        subscription = self._subscriptions.get(telegram_user_id=telegram_user_id, plate=car.car_number)
        monitoring_active = subscription is not None and subscription.active
        return BotReply(
            text=texts.format_car_card_text(car, monitoring_active=monitoring_active),
            car_card=car, car_card_monitoring_active=monitoring_active,
        )

    async def handle_car_action(
        self, action: str, car_id: int, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """None — car_id не существует/чужой (тот же принцип, что и
        handle_car_open) — reader/turkey_bot/handlers.py показывает
        общий "неизвестная кнопка" alert. Ownership перепроверяется здесь
        ЗАНОВО на каждый вызов (см. задачу п.9) — в том числе для
        delete_confirm/delete_cancel, даже если пользователь уже прошёл
        экран подтверждения: car_id из callback_data сам по себе ничего не
        доказывает."""
        car = self._garage.get_owned_car(car_id, telegram_user_id=telegram_user_id)
        if car is None:
            return None

        if action == "check":
            return await self._run_manual_check(car, chat_id=chat_id, telegram_user_id=telegram_user_id)
        if action == "monitor_on":
            return self._enable_monitoring(car, chat_id=chat_id, telegram_user_id=telegram_user_id)
        if action == "monitor_off":
            return self._disable_monitoring(car, telegram_user_id=telegram_user_id)
        if action == "history":
            return self._show_history(car)
        if action == "delete_prompt":
            return self._prompt_delete_car(car)
        if action == "delete_confirm":
            return self._delete_car(car, chat_id=chat_id, telegram_user_id=telegram_user_id)
        if action == "delete_cancel":
            return self._render_car_card(car, telegram_user_id=telegram_user_id)

        # Не должно достигаться — decode_car_action_callback уже
        # ограничивает action допустимым набором (см. keyboards.py).
        logger.warning("Turkey unified UX: неизвестное car action %r (car_id=%s)", action, car_id)
        return None

    async def _run_manual_check(
        self, car: TurkeyUserCar, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """🔎 Проверить сейчас — ОДИН и тот же UnifiedTurkeyCheckService,
        что и плановый мониторинг (см. design report п.12), но БЕЗ
        change-detection/уведомлений (ручная проверка всегда просто
        показывает live-результат целиком, см.
        reader/turkey_bot/monitoring/monitoring_service.py про то,
        где именно живёт change-detection — не здесь). До 35 попыток на
        провайдера при captcha_unavailable/captcha_rejected (см.
        _MANUAL_MAX_ATTEMPTS) — вся retry-логика в check_service, здесь
        только выбор режима/лимита."""
        result = await self._check_service.check(
            car.car_number, max_attempts=_MANUAL_MAX_ATTEMPTS, mode="manual",
        )

        self._runs.save(
            result, telegram_user_id=telegram_user_id, telegram_chat_id=chat_id,
            initiator=_INITIATOR_MANUAL,
        )
        self._garage.update_last_result(
            telegram_user_id=telegram_user_id, car_number=car.car_number,
            overall_status=result.overall_status.value, total_amount=result.total_amount,
        )

        cta = self._debt_cta_buttons() if result.overall_status == OverallStatus.HAS_DEBT else None
        return BotReply(
            text=texts.format_unified_check_result(result), cta_buttons=cta,
            show_main_menu=True, back_to_car_id=car.id,
        )

    def _enable_monitoring(
        self, car: TurkeyUserCar, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """▶️ Включить мониторинг (см. задачу п.5) — active=True,
        next_check_at по строгому расписанию 13:00/21:00 Europe/Istanbul
        (см. next_monitoring_slot, НЕ изменено). Карточка перерисовывается
        сразу с обновлённым состоянием (см. _render_car_card) — ОДНО
        сообщение, без отдельного текста-квитанции (см. задачу: "Не
        создавать новое Telegram-сообщение без необходимости... повторить
        pattern [Georgian bot edit-in-place]")."""
        next_check_at = next_monitoring_slot(datetime.now(timezone.utc))
        self._subscriptions.enable(
            telegram_user_id=telegram_user_id, telegram_chat_id=chat_id,
            plate=car.car_number, next_check_at=next_check_at,
        )
        return self._render_car_card(car, telegram_user_id=telegram_user_id)

    def _disable_monitoring(self, car: TurkeyUserCar, *, telegram_user_id: int) -> BotReply:
        """⏹ Отключить мониторинг (см. задачу п.6) — только подписка
        ИМЕННО этого автомобиля (см. TurkeyMonitoringSubscriptionRepository.
        disable — UNIQUE(telegram_user_id, plate), не может затронуть чужие
        подписки)."""
        self._subscriptions.disable(telegram_user_id=telegram_user_id, plate=car.car_number)
        return self._render_car_card(car, telegram_user_id=telegram_user_id)

    def _show_history(self, car: TurkeyUserCar) -> BotReply:
        runs = self._runs.list_by_plate(car.car_number, limit=10)
        messages = texts.format_history_messages(car.car_number, runs)
        return BotReply(
            text=messages[0], extra_texts=tuple(messages[1:]),
            show_main_menu=True, back_to_car_id=car.id,
        )

    def _prompt_delete_car(self, car: TurkeyUserCar) -> BotReply:
        """Первый шаг удаления — только показывает подтверждение, ЕЩЁ
        НИЧЕГО не удаляет (см. задачу п.7, тот же Georgian pattern —
        reader/public_bot/conversation.py::handle_my_car_delete_prompt)."""
        return BotReply(
            text=texts.format_delete_confirm_prompt(car.car_number),
            car_delete_confirm_car_id=car.id,
        )

    def _delete_car(self, car: TurkeyUserCar, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """Финальный шаг — удаляет ИМЕННО этот автомобиль и его monitoring
        subscription (см. задачу п.7 — не затрагивает других
        пользователей: оба repository-метода фильтруют по
        telegram_user_id), возвращает обновлённый список "🚗 Мои
        автомобили" (тот же Georgian pattern, см.
        reader/public_bot/conversation.py::handle_my_car_delete_confirm —
        "не отдельное сообщение-квитанция, а сразу актуальный список")."""
        self._subscriptions.disable(telegram_user_id=telegram_user_id, plate=car.car_number)
        self._garage.delete_car(car.id, telegram_user_id=telegram_user_id)
        return self.handle_my_cars(chat_id=chat_id, telegram_user_id=telegram_user_id)

    # ---- 📊 Статистика / ⛔ Остановить мониторинг (manager, см. design
    # report п.13) ----

    def handle_statistics(self, *, chat_id: int) -> BotReply:
        """См. задачу "доработать 📊 Статистика Turkey bot" — ТОЛЬКО SQL
        reads поверх уже сохранённых repositories (get_debt_rows/
        get_known_profile), НИКАКИХ provider/network requests, НИКАКОГО
        unified check (см. задачу "КРИТИЧЕСКИ ВАЖНО: НИКАКИХ LIVE CHECK")."""
        self._states.clear(chat_id)
        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        debt_block, debt_messages = self._build_debt_section()
        stats_text = (
            f"{texts.format_statistics_header(stats)}\n\n{debt_block}\n\n{texts.format_statistics_footer(stats)}"
        )

        return BotReply(
            text=stats_text, extra_texts=tuple(debt_messages), show_main_menu=True,
            debt_refresh_available=self._debt_refresh is not None,
        )

    def _build_debt_section(self) -> tuple[str, list[str]]:
        """ВСЕГДА заново перечитывает persisted state из БД (см. задачу
        "manager Statistics / refresh для обоих ботов" п.8: "Statistics
        после refresh: заново query из DB, НЕ показывать старый cached
        UI") — переиспользуется И обычным открытием 📊 Статистика
        (handle_statistics), И результатом "🔄 Проверить авто с
        задолженностью" (handle_debt_refresh_confirm), поэтому обе точки
        ВСЕГДА показывают один и тот же, только что построенный список."""
        total_cars = self._garage.count_all()
        cars = self._garage.list_all_page(offset=0, limit=total_cars) if total_cars else []
        debt_rows = self._statistics.get_debt_rows(cars)

        debt_total_amount = sum((row.total_amount for row in debt_rows), Decimal(0))
        debt_block = texts.format_debt_summary_block(
            debt_car_count=len(debt_rows), debt_total_amount=debt_total_amount,
        )

        debt_lines = []
        for row in debt_rows:
            profile = self._statistics.get_known_profile(row.owner_telegram_user_id)
            username, first_name, last_name = profile if profile is not None else (None, None, None)
            owner_display = texts.format_search_owner_display(
                first_name=first_name, last_name=last_name, username=username,
            )
            debt_lines.append(
                texts.format_debt_row(
                    car_number=row.car_number, owner_display=owner_display, total_amount=row.total_amount,
                )
            )
        debt_messages = texts.format_debt_list_messages(debt_lines)
        return debt_block, debt_messages

    # ---- "🔄 Проверить авто с задолженностью" (см. задачу "manual Turkey
    # debt refresh") — trusted-manager-only, is_trusted() перепроверяется
    # ЗАНОВО на каждом шаге (см. остальные trusted-* методы этого класса) —
    # доступность самой кнопки (debt_refresh_available) НЕ является
    # доказательством авторизации сама по себе. ----

    def handle_debt_refresh_pick(self, *, telegram_user_id: int) -> BotReply:
        """Первый шаг — ТОЛЬКО чтение (list_candidates(), см.
        TurkeyDebtRefreshService), ни одного provider-запроса (см. задачу
        "FLOW": "до нажатия подтверждения НИКАКИХ provider requests").
        Пустой список — сразу финальный ответ, минуя экран подтверждения
        вовсе (см. задачу TESTS п.16)."""
        if not self._is_trusted(telegram_user_id) or self._debt_refresh is None:
            return BotReply(text=texts.SEARCH_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        car_count = len(self._debt_refresh.list_candidates())
        if car_count == 0:
            return BotReply(text=texts.DEBT_REFRESH_NONE_TEXT, show_main_menu=True)

        return BotReply(text=texts.format_debt_refresh_prompt(car_count), debt_refresh_confirm_car_count=car_count)

    def handle_debt_refresh_cancel(self, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=texts.SEARCH_NOT_AUTHORIZED_TEXT, show_main_menu=True)
        return BotReply(text=texts.DEBT_REFRESH_CANCELLED_TEXT, show_main_menu=True)

    async def handle_debt_refresh_confirm(self, *, telegram_user_id: int) -> BotReply:
        """Финальный шаг — is_trusted() и наличие TurkeyDebtRefreshService
        перепроверяются ЗАНОВО (см. handle_debt_refresh_pick). Защита от
        повторного/параллельного нажатия (см. задачу "CONCURRENCY") —
        TurkeyDebtRefreshService.refresh() сам бросает
        TurkeyRefreshAlreadyInProgressError, если предыдущий прогон ещё не
        завершился — единственный источник истины про "идёт ли уже
        refresh", а не отдельная проверка здесь."""
        if not self._is_trusted(telegram_user_id) or self._debt_refresh is None:
            return BotReply(text=texts.SEARCH_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        try:
            outcome = await self._debt_refresh.refresh()
        except TurkeyRefreshAlreadyInProgressError:
            return BotReply(text=texts.DEBT_REFRESH_IN_PROGRESS_TEXT, show_main_menu=True)

        # См. задачу п.8: "Statistics после refresh: заново query из DB,
        # НЕ показывать старый cached UI" — _build_debt_section() читает
        # persisted state ЗАНОВО (provider checks уже завершились и
        # персистентны к этому моменту, см. TurkeyDebtRefreshService.
        # refresh()), поэтому здесь виден РОВНО тот же список, что покажет
        # следующее открытие 📊 Статистика.
        debt_block, debt_messages = self._build_debt_section()
        text = f"{texts.format_debt_refresh_summary(outcome)}\n\n{debt_block}"
        return BotReply(
            text=text, extra_texts=tuple(debt_messages), show_main_menu=True, debt_refresh_available=True,
        )

    def handle_stop_monitoring(self, *, chat_id: int) -> BotReply:
        """⛔ Остановить мониторинг — ТОЛЬКО Turkey test monitoring (своя
        таблица/БД, см. TurkeyMonitoringSubscriptionRepository.
        deactivate_all — физически не может задеть Georgian/production)."""
        self._states.clear(chat_id)
        count = self._subscriptions.deactivate_all()
        return BotReply(text=texts.MONITORING_STOPPED_ALL_TEMPLATE.format(count=count), show_main_menu=True)

    # ---- ℹ️ Справка / 🇬🇪 Штрафы Грузии (не изменились по сути) ----

    def handle_help(self, *, chat_id: int) -> BotReply:
        self._states.clear(chat_id)
        return BotReply(text=texts.HELP_MENU_TEXT, help_keyboard="menu")

    def handle_georgian_bot_link(self, *, chat_id: int) -> BotReply:
        self._states.clear(chat_id)
        return BotReply(text=texts.GEORGIAN_BOT_LINK_TEXT, show_georgian_bot_link=True)

    def handle_help_callback(self, section: str) -> BotReply:
        if section == "terms":
            return BotReply(text=texts.HELP_TERMS_TEXT, help_keyboard="section")
        if section == "gib":
            return BotReply(text=texts.HELP_GIB_TEXT, help_keyboard="section")
        if section == "avrasya":
            return BotReply(text=texts.HELP_AVRASYA_TEXT, help_keyboard="section")
        if section == "payment":
            return BotReply(text=texts.HELP_PAYMENT_TEXT, help_keyboard="section")
        return BotReply(text=texts.HELP_MENU_TEXT, help_keyboard="menu")

    def handle_help_back_to_main(self) -> BotReply:
        return BotReply(text=texts.WELCOME_TEXT, show_main_menu=True)

    # ---- manager/trusted Search (см. задачу "manager/trusted Search") —
    # ТОЛЬКО trusted_operator_user_ids: is_trusted() перепроверяется
    # ЗАНОВО в КАЖДОМ из методов ниже (вход через SEARCH_LABEL клампит это
    # один раз в handle_text, но query-ввод/pagination/New Search/Back
    # callback'и приходят НАПРЯМУЮ из Telegram, см. задачу п.14).
    #
    # В отличие от Georgian bot — БЕЗ live-проверки: Turkey уже хранит
    # ПОЛНЫЙ unified-результат (см. TurkeyCheckRunRepository/
    # turkey_check_runs) на каждую ручную/плановую проверку, включая уже
    # посчитанный authoritative total_amount (см. total_amount_for()) —
    # Search читает ПОСЛЕДНИЙ сохранённый результат конкретного владельца
    # (get_latest_for_owner), никакой новой проверки не запускает и
    # ничего не пересчитывает (см. задачу п.9: "переиспользовать
    # существующую production unified-total business logic"). ----

    def _parse_search_query(self, raw_text: str) -> tuple[str, str] | None:
        """None — пустой запрос (см. задачу "add name search to trusted
        bot search" п.3/п.5). Ведущий "@" — ВСЕГДА username, однозначно.
        БЕЗ "@" — сначала пробуем normalize_plate (см. задачу:
        "переиспользовать СУЩЕСТВУЮЩУЮ normalization каждой страны, не
        создавать вторую реализацию") — похоже на валидный номер —
        car-search; иначе — "person": НЕ различаем здесь username-без-@ и
        имя (см. задачу п.5: "если надёжно автоматически отличить
        невозможно — искать одновременно по username/name и объединять
        результаты с dedup") — _format_search_results_reply сам пробует
        ОБА lookup'а и объединяет по telegram_user_id."""
        stripped = raw_text.strip()
        if not stripped:
            return None

        if stripped.startswith("@"):
            username = stripped[1:].strip()
            return ("username", username) if username else None

        plate = normalize_plate(stripped)
        if plate is not None:
            return "car", plate

        return ("person", stripped) if stripped else None

    def _format_search_block(self, hit: _SearchHit) -> str:
        """Блок ВСЕГДА показывает и владельца (имя+username, см. задачу
        "add name search to trusted bot search" п.7), и машину — единый
        формат независимо от того, что искали (см. texts.format_search_results
        докстрок: имя может соответствовать НЕСКОЛЬКИМ разным
        telegram_user_id, единого "внешнего" owner для заголовка нет)."""
        profile = self._statistics.get_known_profile(hit.owner_telegram_user_id)
        username, first_name, last_name = profile if profile is not None else (None, None, None)
        owner_display = texts.format_search_owner_display(
            first_name=first_name, last_name=last_name, username=username,
        )
        subscription = self._subscriptions.get(
            telegram_user_id=hit.owner_telegram_user_id, plate=hit.car_number,
        )
        monitoring_active = subscription is not None and subscription.active
        unified_result = self._runs.get_latest_for_owner(
            plate=hit.car_number, telegram_user_id=hit.owner_telegram_user_id,
        )
        return texts.format_search_block(
            owner_display=owner_display, car_number=hit.car_number,
            monitoring_active=monitoring_active, unified_result=unified_result,
        )

    def _format_search_results_reply(self, query_type: str, query: str, page: int) -> BotReply:
        """page — ЛЮБОЕ int (forged/устаревший callback, см. задачу п.14) —
        клампится здесь же. Результаты НЕ кэшируются между вызовами —
        DB-запрос выполняется заново на КАЖДЫЙ page (дёшево — Turkey
        Search читает уже сохранённые unified-результаты, а не запускает
        новую проверку, см. класс docstring)."""
        if query_type == "username":
            found = self._statistics.find_username(query)
            if found is None:
                return BotReply(
                    text=texts.format_search_not_found(f"@{query}"),
                    search_result_shown=True, search_query_type=query_type, search_query=query,
                    search_page=0, search_total_pages=1,
                )
            owner_id, _display_username = found
            all_hits = [
                _SearchHit(car_id=car.id, owner_telegram_user_id=owner_id, car_number=car.car_number)
                for car in self._garage.list_cars(owner_id)
            ]
        elif query_type == "person":
            # Bare текст — одновременно username-без-@ И имя (см. задачу
            # п.5) — объединяем по telegram_user_id (см. задачу п.6:
            # "имена не уникальны, показать ВСЕХ найденных пользователей";
            # тот же union покрывает "один и тот же человек найден и по
            # username, и по имени" — не задваивает его машины).
            user_ids: set[int] = set()
            found_by_username = self._statistics.find_username(query)
            if found_by_username is not None:
                user_ids.add(found_by_username[0])
            for user_id, _username, _first_name, _last_name in self._statistics.find_name(query):
                user_ids.add(user_id)

            all_hits = []
            for user_id in sorted(user_ids):
                all_hits.extend(
                    _SearchHit(car_id=car.id, owner_telegram_user_id=user_id, car_number=car.car_number)
                    for car in self._garage.list_cars(user_id)
                )
        else:
            all_hits = [
                _SearchHit(car_id=car.id, owner_telegram_user_id=car.telegram_user_id, car_number=car.car_number)
                for car in self._garage.list_by_car_number(query)
            ]

        if not all_hits:
            query_display = f"@{query}" if query_type == "username" else query
            return BotReply(
                text=texts.format_search_not_found(query_display),
                search_result_shown=True, search_query_type=query_type, search_query=query,
                search_page=0, search_total_pages=1,
            )

        total_pages = -(-len(all_hits) // _SEARCH_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        page_hits = all_hits[page * _SEARCH_PAGE_SIZE:page * _SEARCH_PAGE_SIZE + _SEARCH_PAGE_SIZE]

        blocks = [self._format_search_block(hit) for hit in page_hits]
        text = texts.format_search_results(blocks=blocks)
        if total_pages > 1:
            text += "\n\n" + texts.format_search_pagination_footer(page=page, total_pages=total_pages)

        return BotReply(
            text=text, search_result_shown=True, search_query_type=query_type, search_query=query,
            search_page=page, search_total_pages=total_pages,
        )

    def handle_search_start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        self._states.set(chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_SEARCH_QUERY)
        return BotReply(text=texts.SEARCH_ENTRY_TEXT, search_prompt=True)

    def _handle_search_query_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            self._states.clear(chat_id)
            return BotReply(text=texts.SEARCH_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        parsed = self._parse_search_query(raw_text)
        if parsed is None:
            # Остаёмся на том же шаге — тот же UX, что и у
            # _handle_new_car_plate: можно ввести запрос заново без
            # повторного нажатия "🔎 Поиск".
            return BotReply(
                text=f"❌ Не удалось распознать запрос.\n\n{texts.SEARCH_ENTRY_TEXT}", search_prompt=True,
            )

        query_type, query = parsed
        self._states.clear(chat_id)
        return self._format_search_results_reply(query_type, query, 0)

    def handle_search_page(
        self, query_type: str, query: str, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — telegram_user_id НЕ trusted (см. задачу п.14) —
        query_type/query из callback_data публичны и НЕ являются
        доказательством авторизации сами по себе."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self._format_search_results_reply(query_type, query, page)

    def handle_search_new(self, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """"🔎 Новый поиск" с экрана результата — возвращает на экран ввода
        запроса (см. handle_search_start)."""
        if not self._is_trusted(telegram_user_id):
            return None
        self._states.set(chat_id, telegram_user_id=telegram_user_id, step=_STEP_AWAITING_SEARCH_QUERY)
        return BotReply(text=texts.SEARCH_ENTRY_TEXT, search_prompt=True)

    def handle_search_back(self, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """"↩️ Назад"/"↩️ В меню" — возвращает в главное меню, очищая
        любое незавершённое состояние Search."""
        if not self._is_trusted(telegram_user_id):
            return None
        self._states.clear(chat_id)
        return BotReply(text=texts.WELCOME_TEXT, show_main_menu=True)
