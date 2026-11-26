"""Диалог MAX-бота Грузии: события MAX -> MaxGeorgiaService -> ответы.

Клиентские функции — те же, что у Telegram-бота: добавить авто (бессрочный
мониторинг), мои авто, проверить штрафы, сумма к оплате и «🔄 Уточнить
сумму», оплаченные штрафы, включение/выключение уведомлений, удаление.
Менеджеры (отдельный список MAX user_id, не Telegram id) дополнительно видят
статистику MAX и поиск номера — только чтение БД, без живых проверок.

Права: user_id отправителя события; id подписки из кнопки сверяется с ним
на каждом нажатии.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from reader.fines.validation import FineValidationError, looks_like_car_number, normalize_car_number
from reader.max_bot.georgia import keyboards as kb
from reader.max_bot.georgia import texts as max_texts
from reader.max_bot.georgia.service import CHECK_ALREADY_IN_PROGRESS, MaxGeorgiaService
from reader.max_bot.keyboards import callback
from reader.max_bot.linking import MaxTelegramLinkService
from reader.max_bot.runtime import MaxResponder, Reply
from reader.max_bot.storage import MaxBotStorage
from reader.max_bot.updates import MaxEvent
from reader.public_bot import texts as ge_texts
from reader.public_bot.payable_amount_service import (
    STATUS_CACHED,
    STATUS_FAILED,
    STATUS_LIMITED,
    STATUS_NOT_CONFIGURED,
    STATUS_SUSPENDED,
)
from reader.public_bot.payable_amount_texts import format_amount_increase_line

logger = logging.getLogger(__name__)

STEP_AWAITING_PLATE = "awaiting_plate"
STEP_AWAITING_CONFIRM = "awaiting_confirm"
STEP_AWAITING_SEARCH = "awaiting_search"

_START_COMMANDS = {"/start", "/menu", "start", "меню", "старт"}


class MaxGeorgiaBot:
    def __init__(
        self,
        service: MaxGeorgiaService,
        storage: MaxBotStorage,
        *,
        tz: ZoneInfo,
        manager_user_ids: frozenset[int],
        payment_help_url: str | None,
        telegram_bot_username: str,
        link_service: MaxTelegramLinkService | None = None,
        telegram_cars_lookup=None,
        telegram_subscriber_count=None,
    ):
        self._service = service
        self._storage = storage
        self._tz = tz
        self._managers = manager_user_ids
        self._payment_url = payment_help_url
        self._telegram_bot = telegram_bot_username
        self._links = link_service
        # telegram_user_id -> номера его активных Telegram-подписок (только
        # чтение, для переноса после привязки); None — перенос выключен.
        self._telegram_cars_lookup = telegram_cars_lookup
        self._telegram_subscriber_count = telegram_subscriber_count

    def is_manager(self, user_id: int) -> bool:
        return user_id in self._managers

    def _menu(self, user_id: int, text: str = max_texts.MENU_TEXT) -> Reply:
        return Reply(text=text, keyboard=kb.main_menu(is_manager=self.is_manager(user_id)))

    async def handle(self, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        if event.kind == "started":
            self._storage.clear_state(user_id)
            await responder.send(user_id, self._menu(user_id, max_texts.GREETING_TEXT))
        elif event.kind == "message":
            await self._on_text(event, responder)
        elif event.kind == "callback":
            await self._on_callback(event, responder)

    # ---- текст ----

    async def _on_text(self, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        text = (event.text or "").strip()
        if not text:
            return
        lowered = text.lower()
        if lowered in _START_COMMANDS:
            self._storage.clear_state(user_id)
            await responder.answer(event, self._menu(user_id, max_texts.GREETING_TEXT))
            return
        if lowered in ("/help", "справка"):
            await responder.answer(event, Reply(text=max_texts.HELP_TEXT, keyboard=kb.back_to_menu()))
            return
        if lowered in ("/cancel", "отмена"):
            self._storage.clear_state(user_id)
            await responder.answer(event, self._menu(user_id))
            return

        state = self._storage.get_state(user_id)
        if state is not None and state.step == STEP_AWAITING_SEARCH and self.is_manager(user_id):
            await responder.answer(event, self._manager_search(text, user_id=user_id))
            return
        if state is not None and state.step == STEP_AWAITING_PLATE:
            await responder.answer(event, self._plate_input(text, user_id=user_id))
            return
        if looks_like_car_number(text):
            # Прямой ввод номера из меню — как у Telegram-бота: подтверждение
            # перед добавлением, опечатка не включает мониторинг молча.
            await responder.answer(event, self._plate_input(text, user_id=user_id))
            return
        await responder.answer(event, self._menu(user_id, max_texts.UNKNOWN_TEXT))

    def _plate_input(self, text: str, *, user_id: int) -> Reply:
        try:
            car_number = normalize_car_number(text)
        except FineValidationError as exc:
            self._storage.set_state(user_id, STEP_AWAITING_PLATE)
            return Reply(text=f"❌ {exc}\n\n{ge_texts.CAR_NUMBER_PROMPT}", keyboard=kb.cancel(), markdown=False)
        self._storage.set_state(user_id, STEP_AWAITING_CONFIRM, {"car_number": car_number})
        return Reply(text=ge_texts.format_add_car_confirm_prompt(car_number), keyboard=kb.add_confirm(car_number))

    # ---- кнопки ----

    async def _on_callback(self, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        parsed = kb.parse(event.payload)
        if parsed is None:
            await responder.notify(event, max_texts.STALE_BUTTON_TEXT)
            return
        action, subscription_id = parsed

        if subscription_id is None:
            await self._on_menu_action(action, event, responder)
            return

        subscription = self._service.get_owned(subscription_id, max_user_id=user_id)
        if subscription is None:
            await responder.notify(event, max_texts.NOT_YOURS_TEXT)
            return

        if action == "car":
            await responder.answer(event, self._car_card(subscription))
        elif action == "chk":
            await self._run_check(event, responder, subscription.id)
        elif action == "off":
            updated = self._service.turn_off(subscription.id, max_user_id=user_id)
            await responder.answer(event, Reply(
                text=max_texts.format_turn_off(updated),
                keyboard=kb.car_card(updated, payable_enabled=self._service.payable_enabled),
            ))
        elif action == "on":
            updated = self._service.turn_on(subscription.id, max_user_id=user_id)
            await responder.answer(event, Reply(
                text=max_texts.format_turn_on(updated),
                keyboard=kb.car_card(updated, payable_enabled=self._service.payable_enabled),
            ))
        elif action == "del":
            await responder.answer(event, Reply(
                text=ge_texts.format_delete_confirm_prompt(subscription.car_number),
                keyboard=kb.delete_confirm(subscription.id),
            ))
        elif action == "delok":
            self._service.delete(subscription.id, max_user_id=user_id)
            cars = self._service.list_cars(user_id)
            await responder.answer(event, Reply(
                text=f"{max_texts.DELETED_TEXT}\n\n{max_texts.format_cars_list(cars)}",
                keyboard=kb.cars_list(cars),
            ))
        elif action == "pay":
            await responder.answer(event, self._payable_card(subscription, notes=[]))
        elif action == "paid":
            paid = self._service.paid_statuses(subscription.car_number)
            await responder.answer(event, Reply(
                text=max_texts.format_paid_fines(subscription.car_number, paid),
                keyboard=[[callback(ge_texts.BACK_BUTTON_LABEL, kb.action("car", subscription.id))]],
            ))
        elif action == "amt":
            await self._run_refresh(event, responder, subscription.id)
        else:
            await responder.notify(event, max_texts.STALE_BUTTON_TEXT)

    async def _on_menu_action(self, action: str, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        if action in ("menu", "cancel"):
            self._storage.clear_state(user_id)
            await responder.answer(event, self._menu(user_id))
        elif action == "add":
            self._storage.set_state(user_id, STEP_AWAITING_PLATE)
            await responder.answer(event, Reply(text=ge_texts.CAR_NUMBER_PROMPT, keyboard=kb.cancel()))
        elif action == "addok":
            await self._confirm_add(event, responder)
        elif action == "cars":
            cars = self._service.list_cars(user_id)
            await responder.answer(event, Reply(text=max_texts.format_cars_list(cars), keyboard=kb.cars_list(cars)))
        elif action == "check":
            cars = self._service.list_cars(user_id)
            if not cars:
                await responder.answer(event, Reply(
                    text=ge_texts.NO_CARS_TEXT, keyboard=[[callback(ge_texts.ADD_CAR_LABEL, kb.ADD)]],
                ))
            elif len(cars) == 1:
                await self._run_check(event, responder, cars[0].id)
            else:
                await responder.answer(event, Reply(
                    text=ge_texts.CHECK_NOW_PICK_PROMPT, keyboard=kb.cars_list(cars, prefix="chk"),
                ))
        elif action == "help":
            await responder.answer(event, Reply(text=max_texts.HELP_TEXT, keyboard=kb.back_to_menu()))
        elif action in ("link", "linkcode", "import"):
            await self._on_link_action(action, event, responder)
        elif action in ("stats", "search"):
            if not self.is_manager(user_id):
                await responder.notify(event, max_texts.MANAGER_ONLY_TEXT)
            elif action == "stats":
                await responder.answer(event, self._manager_stats())
            else:
                self._storage.set_state(user_id, STEP_AWAITING_SEARCH)
                await responder.answer(event, Reply(text=max_texts.SEARCH_PROMPT, keyboard=kb.cancel()))
        else:
            await responder.notify(event, max_texts.STALE_BUTTON_TEXT)

    # ---- добавление ----

    async def _confirm_add(self, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        state = self._storage.get_state(user_id)
        car_number = (state.payload or {}).get("car_number") if state and state.step == STEP_AWAITING_CONFIRM else None
        if not car_number:
            await responder.answer(event, self._menu(user_id, ge_texts.STALE_DIALOG_TEXT))
            return
        # Диалог закрывается ДО медленной проверки: повторное нажатие
        # «✅ Добавить» уже ничего не находит.
        self._storage.clear_state(user_id)

        async def work() -> Reply:
            outcome = await self._service.add_car(max_user_id=user_id, car_number=car_number)
            summary = ge_texts.format_add_car_summary(
                car_number=car_number, check_ok=outcome.check_ok, new_fines_count=outcome.new_fines_count,
                provider_unavailable=outcome.provider_unavailable,
                **(self._service.unverified_totals(car_number) if outcome.check_ok else {}),
            )
            if outcome.already_added:
                summary = f"{max_texts.ALREADY_ADDED_LINE}\n\n{summary}"
            keyboard = [
                [callback(ge_texts.MY_CARS_LABEL, kb.action("car", outcome.subscription.id))],
                [callback(kb.MENU_LABEL, kb.MENU)],
            ]
            return Reply(text=summary, keyboard=keyboard)

        await responder.run_slow(event, max_texts.ADDING_TEXT, work)

    # ---- карточка, проверка, суммы ----

    def _car_card(self, subscription) -> Reply:
        return Reply(
            text=max_texts.format_car_card(subscription),
            keyboard=kb.car_card(subscription, payable_enabled=self._service.payable_enabled),
        )

    async def _run_check(self, event: MaxEvent, responder: MaxResponder, subscription_id: int) -> None:
        user_id = event.user.user_id

        async def work() -> Reply | None:
            outcome = await self._service.check_now(subscription_id, max_user_id=user_id)
            if outcome is None:
                return Reply(text=max_texts.NOT_YOURS_TEXT, keyboard=kb.back_to_menu(), markdown=False)
            if outcome is CHECK_ALREADY_IN_PROGRESS:
                return Reply(text=max_texts.IN_PROGRESS_TEXT, markdown=False)
            return self._check_result(outcome)

        await responder.run_slow(event, max_texts.CHECKING_TEXT, work)

    def _check_result(self, outcome) -> Reply:
        statuses = None
        if outcome.check_ok and outcome.fines:
            statuses = self._service.subscriber_statuses(outcome.car_number)
        has_payable = bool(outcome.fines) and self._service.has_payable(outcome.fines, statuses)
        payable = None
        if self._service.payable_enabled and statuses is not None:
            payable = self._service.check_now_amounts(outcome.car_number, outcome.fines, statuses)
        unverified = self._service.unverified_totals(outcome.car_number) if outcome.check_ok else {}
        text = ge_texts.format_check_now_result(outcome, statuses=statuses, payable=payable, tz=self._tz, **unverified)
        keyboard = kb.check_result(
            outcome.subscription_id, refresh=has_payable and self._service.payable_enabled,
            payment_url=self._payment_url, show_payment=has_payable,
        )
        return Reply(text=text, keyboard=keyboard)

    def _payable_card(self, subscription, *, notes: list[str]) -> Reply:
        if not self._service.payable_enabled:
            return Reply(text=max_texts.PAYABLE_DISABLED_TEXT, keyboard=kb.back_to_menu(), markdown=False)
        receipts = self._service.debt_receipts(subscription.car_number)
        text = ge_texts.format_payable_amount_card(
            car_number=subscription.car_number, receipts=receipts, tz=self._tz, notes=notes,
        )
        rows = kb.amount_card(subscription.id, self._payment_url) if receipts else []
        rows.append([callback(ge_texts.BACK_BUTTON_LABEL, kb.action("car", subscription.id))])
        return Reply(text=text, keyboard=rows)

    async def _run_refresh(self, event: MaxEvent, responder: MaxResponder, subscription_id: int) -> None:
        user_id = event.user.user_id

        async def work() -> Reply:
            result = await self._service.refresh_amount(subscription_id, max_user_id=user_id)
            if result is None:
                return Reply(text=max_texts.PAYABLE_DISABLED_TEXT, keyboard=kb.back_to_menu(), markdown=False)
            if result is CHECK_ALREADY_IN_PROGRESS:
                return Reply(text=max_texts.IN_PROGRESS_TEXT, markdown=False)
            subscription, outcome = result
            return self._payable_card(subscription, notes=_refresh_notes(outcome))

        await responder.run_slow(event, max_texts.REFRESHING_TEXT, work)

    # ---- привязка Telegram ----

    async def _on_link_action(self, action: str, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        if self._links is None:
            await responder.notify(event, "Привязка Telegram пока недоступна.")
            return
        linked_telegram_id = self._links.linked_telegram_user(user_id)
        if action == "link":
            await responder.answer(event, Reply(
                text=max_texts.format_link_menu(linked=linked_telegram_id is not None, telegram_bot=self._telegram_bot),
                keyboard=kb.link_menu(linked=linked_telegram_id is not None and self._telegram_cars_lookup is not None),
            ))
        elif action == "linkcode":
            code = self._links.issue_code(user_id)
            await responder.answer(event, Reply(
                text=max_texts.format_link_code(code, telegram_bot=self._telegram_bot), keyboard=kb.back_to_menu(),
            ))
        elif action == "import":
            if linked_telegram_id is None or self._telegram_cars_lookup is None:
                await responder.answer(event, Reply(text=max_texts.LINK_REQUIRED_TEXT, keyboard=kb.back_to_menu()))
                return
            imported, skipped = [], 0
            for car_number in self._telegram_cars_lookup(linked_telegram_id):
                if self._service.repository.get_for_user_and_car(user_id, car_number) is not None:
                    skipped += 1
                    continue
                self._service.import_car(max_user_id=user_id, car_number=car_number)
                imported.append(car_number)
            await responder.answer(event, Reply(
                text=max_texts.format_import_result(imported, skipped),
                keyboard=kb.cars_list(self._service.list_cars(user_id)),
            ))

    # ---- менеджеры (только чтение) ----

    def _manager_stats(self) -> Reply:
        repository = self._service.repository
        return Reply(
            text=max_texts.format_manager_stats(
                users=self._storage.count_users(), subscriptions=repository.count_by_status(),
                fine_deliveries=repository.fine_delivery_counts(),
                amount_deliveries=repository.amount_delivery_counts(),
            ),
            keyboard=kb.back_to_menu(),
        )

    def _manager_search(self, text: str, *, user_id: int) -> Reply:
        try:
            car_number = normalize_car_number(text)
        except FineValidationError as exc:
            return Reply(text=f"❌ {exc}\n\n{max_texts.SEARCH_PROMPT}", keyboard=kb.cancel(), markdown=False)
        self._storage.clear_state(user_id)
        tasks = self._service.task_statuses(car_number)
        debt = self._service.debt_receipts(car_number)
        text = max_texts.format_manager_search(
            car_number, task_statuses=tasks,
            telegram_subscribers=self._telegram_subscriber_count(car_number) if self._telegram_subscriber_count else 0,
            max_subscribers=len(self._service.repository.list_for_car(car_number)),
            unpaid_count=len(debt), unpaid_amount=sum(receipt.base_amount or 0.0 for receipt in debt),
            paid_count=len(self._service.paid_statuses(car_number)),
        )
        keyboard = [[callback(kb.MANAGER_SEARCH_LABEL, kb.SEARCH)], [callback(kb.MENU_LABEL, kb.MENU)]]
        return Reply(text=text, keyboard=keyboard)


def _refresh_notes(outcome) -> list[str]:
    """Те же пометки к карточке, что у Telegram-бота после «🔄 Уточнить сумму»."""
    notes: list[str] = []
    for result in outcome.results:
        if result.increased_from is not None and result.amount is not None:
            notes.append(format_amount_increase_line(result.increased_from, result.amount.amount))
    statuses_seen = {result.status for result in outcome.results}
    failed = sum(1 for result in outcome.results if result.status == STATUS_FAILED)
    if statuses_seen & {STATUS_SUSPENDED, STATUS_NOT_CONFIGURED}:
        notes.append(ge_texts.PAYABLE_AMOUNT_UNAVAILABLE_TEXT)
    elif failed:
        notes.append(ge_texts.format_payable_amount_failed_note(failed))
    if STATUS_LIMITED in statuses_seen:
        notes.append(ge_texts.PAYABLE_AMOUNT_LIMIT_TEXT)
    if STATUS_CACHED in statuses_seen:
        notes.append(ge_texts.PAYABLE_AMOUNT_RECENT_TEXT)
    return notes
