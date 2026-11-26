"""Диалог MAX-бота Турции: события MAX -> MaxTurkeyService -> ответы.

Клиентские функции — те же, что у Telegram-бота Турции: добавить авто,
мои авто, проверить сейчас (GİB, Avrasya Tüneli, KGM одной проверкой),
включить/отключить мониторинг, история, удаление. Новых функций, которых
нет у Telegram-бота (например, разделения на оплаченные/неоплаченные), здесь
нет. Менеджеры (отдельный список MAX user_id) видят статистику MAX и поиск
номера — только чтение БД, без живых проверок.

Права: user_id отправителя события; id машины из кнопки сверяется с ним на
каждом нажатии.
"""

from __future__ import annotations

import logging

from reader.max_bot.keyboards import callback
from reader.max_bot.linking import MaxTelegramLinkService
from reader.max_bot.runtime import MaxResponder, Reply
from reader.max_bot.storage import MaxBotStorage
from reader.max_bot.turkey import keyboards as kb
from reader.max_bot.turkey import texts as max_texts
from reader.max_bot.turkey.repository import MaxTrCar
from reader.max_bot.turkey.service import CHECK_IN_PROGRESS, MaxTurkeyService
from reader.max_bot.updates import MaxEvent
from reader.turkey_bot import texts as tr_texts
from reader.turkey_bot.unified.models import OverallStatus
from reader.turkey_bot.validation import normalize_plate

logger = logging.getLogger(__name__)

STEP_AWAITING_PLATE = "awaiting_plate"
STEP_AWAITING_SEARCH = "awaiting_search"

_START_COMMANDS = {"/start", "/menu", "start", "меню", "старт"}


class MaxTurkeyBot:
    def __init__(
        self,
        service: MaxTurkeyService,
        storage: MaxBotStorage,
        *,
        manager_user_ids: frozenset[int],
        payment_help_url: str | None,
        telegram_bot_username: str,
        link_service: MaxTelegramLinkService | None = None,
        telegram_cars_lookup=None,
        telegram_subscriber_count=None,
    ):
        self._service = service
        self._storage = storage
        self._managers = manager_user_ids
        self._payment_url = payment_help_url
        self._telegram_bot = telegram_bot_username
        self._links = link_service
        # telegram_user_id -> [(номер, мониторинг включён)] (только чтение);
        # None — перенос выключен.
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
        plate = normalize_plate(text)
        if plate is not None:
            # Голый ввод номера — то же «➕ Добавить авто», как у Telegram-бота.
            await responder.answer(event, self._add_car(plate, user_id=user_id))
            return
        if state is not None and state.step == STEP_AWAITING_PLATE:
            await responder.answer(event, Reply(text=tr_texts.INVALID_PLATE_TEXT, keyboard=kb.cancel()))
            return
        await responder.answer(event, self._menu(user_id, max_texts.UNKNOWN_TEXT))

    def _add_car(self, plate: str, *, user_id: int) -> Reply:
        self._storage.clear_state(user_id)
        car, created = self._service.add_car(max_user_id=user_id, plate=plate)
        monitoring_active = self._service.monitoring_active(user_id, car.plate)
        return Reply(
            text=max_texts.format_car_added(car, created=created, monitoring_active=monitoring_active),
            keyboard=kb.car_card(car, monitoring_active=monitoring_active),
        )

    # ---- кнопки ----

    async def _on_callback(self, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        parsed = kb.parse(event.payload)
        if parsed is None:
            await responder.notify(event, max_texts.STALE_BUTTON_TEXT)
            return
        action, car_id = parsed
        if car_id is None:
            await self._on_menu_action(action, event, responder)
            return

        car = self._service.get_owned_car(car_id, max_user_id=user_id)
        if car is None:
            await responder.notify(event, max_texts.NOT_YOURS_TEXT)
            return

        if action == "car":
            await responder.answer(event, self._car_card(car))
        elif action == "chk":
            await self._run_check(event, responder, car)
        elif action == "on":
            self._service.enable_monitoring(car)
            await responder.answer(event, self._car_card(car))
        elif action == "off":
            self._service.disable_monitoring(car)
            await responder.answer(event, self._car_card(car))
        elif action == "hist":
            messages = tr_texts.format_history_messages(car.plate, self._service.history(car.plate))
            await responder.answer(event, Reply(
                text=messages[0], extra_texts=tuple(messages[1:]), keyboard=kb.back_to_car(car.id),
            ))
        elif action == "del":
            await responder.answer(event, Reply(
                text=tr_texts.format_delete_confirm_prompt(car.plate), keyboard=kb.delete_confirm(car.id),
            ))
        elif action == "delok":
            self._service.delete_car(car)
            await responder.answer(event, self._cars_list(user_id))
        else:
            await responder.notify(event, max_texts.STALE_BUTTON_TEXT)

    async def _on_menu_action(self, action: str, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        if action in ("menu", "cancel"):
            self._storage.clear_state(user_id)
            await responder.answer(event, self._menu(user_id))
        elif action == "add":
            self._storage.set_state(user_id, STEP_AWAITING_PLATE)
            await responder.answer(event, Reply(text=tr_texts.ASK_PLATE_FOR_NEW_CAR_TEXT, keyboard=kb.cancel()))
        elif action == "cars":
            self._storage.clear_state(user_id)
            await responder.answer(event, self._cars_list(user_id))
        elif action == "check":
            cars = self._service.list_cars(user_id)
            if not cars:
                await responder.answer(event, Reply(
                    text=tr_texts.EMPTY_MY_CARS_TEXT, keyboard=[[callback(tr_texts.ADD_CAR_LABEL, kb.ADD)]],
                ))
            elif len(cars) == 1:
                await self._run_check(event, responder, cars[0])
            else:
                await responder.answer(event, Reply(
                    text=max_texts.CHECK_PICK_PROMPT, keyboard=kb.cars_list(cars, self._car_labels(user_id, cars),
                                                                             prefix="chk"),
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

    # ---- экраны ----

    def _car_labels(self, user_id: int, cars: list[MaxTrCar]) -> dict[int, str]:
        return {
            car.id: max_texts.format_car_button(
                car, monitoring_active=self._service.monitoring_active(user_id, car.plate),
            )
            for car in cars
        }

    def _cars_list(self, user_id: int) -> Reply:
        cars = self._service.list_cars(user_id)
        if not cars:
            return Reply(text=tr_texts.EMPTY_MY_CARS_TEXT, keyboard=[
                [callback(tr_texts.ADD_CAR_LABEL, kb.ADD)], [callback(kb.MENU_LABEL, kb.MENU)],
            ])
        return Reply(text=tr_texts.MY_CARS_HEADER, keyboard=kb.cars_list(cars, self._car_labels(user_id, cars)))

    def _car_card(self, car: MaxTrCar) -> Reply:
        monitoring_active = self._service.monitoring_active(car.max_user_id, car.plate)
        return Reply(
            text=max_texts.format_car_card(car, monitoring_active=monitoring_active),
            keyboard=kb.car_card(car, monitoring_active=monitoring_active),
        )

    async def _run_check(self, event: MaxEvent, responder: MaxResponder, car: MaxTrCar) -> None:
        async def work() -> Reply:
            result = await self._service.check_now(car)
            if result is CHECK_IN_PROGRESS:
                return Reply(text=max_texts.IN_PROGRESS_TEXT, markdown=False)
            return Reply(
                text=tr_texts.format_unified_check_result(result),
                keyboard=kb.check_result(
                    car.id, payment_url=self._payment_url,
                    show_payment=result.overall_status == OverallStatus.HAS_DEBT,
                ),
            )

        await responder.run_slow(event, max_texts.CHECKING_TEXT, work)

    # ---- привязка Telegram ----

    async def _on_link_action(self, action: str, event: MaxEvent, responder: MaxResponder) -> None:
        user_id = event.user.user_id
        if self._links is None:
            await responder.notify(event, max_texts.LINK_UNAVAILABLE_TEXT)
            return
        linked_telegram_id = self._links.linked_telegram_user(user_id)
        if action == "link":
            await responder.answer(event, Reply(
                text=max_texts.format_link_menu(linked=linked_telegram_id is not None, telegram_bot=self._telegram_bot),
                keyboard=kb.link_menu(
                    can_import=linked_telegram_id is not None and self._telegram_cars_lookup is not None,
                ),
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
            imported, skipped = self._service.import_cars(user_id, self._telegram_cars_lookup(linked_telegram_id))
            reply = self._cars_list(user_id)
            await responder.answer(event, Reply(
                text=max_texts.format_import_result(imported, skipped), keyboard=reply.keyboard,
            ))

    # ---- менеджеры (только чтение) ----

    def _manager_stats(self) -> Reply:
        repository = self._service.repository
        return Reply(
            text=max_texts.format_manager_stats(
                users=self._storage.count_users(), cars=repository.count_cars(),
                monitoring=repository.count_monitoring(),
            ),
            keyboard=kb.back_to_menu(),
        )

    def _manager_search(self, text: str, *, user_id: int) -> Reply:
        plate = normalize_plate(text)
        if plate is None:
            return Reply(text=f"{tr_texts.INVALID_PLATE_TEXT}\n\n{max_texts.SEARCH_PROMPT}", keyboard=kb.cancel())
        self._storage.clear_state(user_id)
        overview = self._service.plate_overview(plate)
        telegram_subscribers = self._telegram_subscriber_count(plate) if self._telegram_subscriber_count else 0
        return Reply(
            text=max_texts.format_manager_search(overview, telegram_subscribers=telegram_subscribers),
            keyboard=[[callback(kb.MANAGER_SEARCH_LABEL, kb.SEARCH)], [callback(kb.MENU_LABEL, kb.MENU)]],
        )
