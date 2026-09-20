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
from zoneinfo import ZoneInfo

from reader.turkey_bot import texts
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
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

_INITIATOR_MANUAL = "manual"

_CAR_ACTIONS_REQUIRING_CHECK = {"check"}


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
            return self.handle_my_cars(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.CHECK_NOW_LABEL:
            return self.handle_check_now_start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if stripped == texts.STATISTICS_LABEL and self._is_trusted(telegram_user_id):
            return self.handle_statistics(chat_id=chat_id)

        if stripped == texts.STOP_MONITORING_LABEL and self._is_trusted(telegram_user_id):
            return self.handle_stop_monitoring(chat_id=chat_id)

        if stripped == texts.HELP_LABEL:
            return self.handle_help(chat_id=chat_id)

        if stripped == texts.GEORGIAN_BOT_LINK_LABEL:
            return self.handle_georgian_bot_link(chat_id=chat_id)

        state = self._states.get(chat_id)
        if state is not None and state.step == _STEP_AWAITING_NEW_CAR_PLATE:
            return await self._handle_new_car_plate(
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
        где именно живёт change-detection — не здесь)."""
        result = await self._check_service.check(car.car_number)

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
        self._states.clear(chat_id)
        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        user_messages = texts.format_user_list_messages(self._statistics.list_known_users())
        return BotReply(
            text=texts.format_statistics(stats), extra_texts=tuple(user_messages), show_main_menu=True,
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
