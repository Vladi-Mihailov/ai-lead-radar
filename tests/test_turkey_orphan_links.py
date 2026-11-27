"""🔗 Привязки без менеджера (Turkey bot): владелец машины-якоря больше не
менеджер. Клиент сохраняет доступ и уведомления; бывший менеджер управлять
не может; администратор находит такие привязки, отключает их или передаёт
действующему менеджеру — без второй проверки номера и без повторов.

Настоящие репозитории поверх SQLite (tmp_path); проверки и уведомления —
фейки. «Потерял статус менеджера» = новый список trusted (как после правки
config.yaml и перезапуска бота)."""

import re
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.client_links import TurkeyClientLinkRepository, TurkeyClientLinkService, TurkeyLinkedClientNotifier
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import TurkeyConversationStateRepository
from reader.turkey_bot.keyboards import decode_orphan_callback, encode_orphan_callback, main_menu_keyboard
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.monitoring_service import TurkeyMonitoringService
from reader.turkey_bot.monitoring.subscription_repository import TurkeyMonitoringSubscriptionRepository
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.models import ProviderStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository
from tests.test_turkey_client_linking import _CLIENT, _Notifier, _PLATE, _ScriptedCheckService, _T0, _result

_FORMER = 900001     # менеджер, который добавил машину и потом потерял статус
_MANAGER2 = 900002   # действующий менеджер
_MANAGER3 = 900003   # ещё один действующий менеджер
_ADMIN = 800001      # администратор бота (fine_monitor.allowed_user_ids)
_STRANGER = 700009


class _Fx:
    def __init__(self, tmp_path):
        self.db = tmp_path / "users.db"
        self.now = _T0
        self.trusted = {_FORMER, _MANAGER2, _MANAGER3}
        self.states = TurkeyConversationStateRepository(self.db)
        self.known = TurkeyBotKnownUsersRepository(self.db)
        self.garage = TurkeyUserCarsRepository(self.db)
        self.runs = TurkeyCheckRunRepository(self.db)
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(self.db)
        self.links = TurkeyClientLinkRepository(self.db)
        self.check = _ScriptedCheckService()
        self.notifier = _Notifier()
        self.service = TurkeyClientLinkService(
            self.links, self.garage, self.subscriptions, self.runs, self.known,
            bot_username="ExampleTRbot", clock=lambda: self.now, is_manager=lambda uid: uid in self.trusted,
        )
        self.monitoring = TurkeyMonitoringService(
            self.check, self.runs, self.subscriptions, self.notifier,
            linked_clients=TurkeyLinkedClientNotifier(self.links, self.runs, self.subscriptions),
        )
        for user_id, username, first in ((_CLIENT, "client_one", "Client"), (_FORMER, "former_mgr", "Former"),
                                         (_MANAGER2, "manager_two", "Second"), (_MANAGER3, "manager_three", "Third"),
                                         (_ADMIN, "admin_x", "Admin")):
            self.known.record_seen(telegram_user_id=user_id, telegram_chat_id=user_id, telegram_username=username,
                                   first_name=first)
        self.controller = self.build_controller()

    def build_controller(self):
        return ConversationController(
            self.states, self.garage, self.runs, self.subscriptions,
            TurkeyStatisticsService(self.known, self.runs, self.subscriptions), self.check,
            trusted_operator_user_ids=frozenset(self.trusted), client_link_service=self.service,
            admin_user_ids=frozenset({_ADMIN}),
        )

    def demote(self, user_id=_FORMER):
        self.trusted.discard(user_id)
        self.controller = self.build_controller()

    async def linked_car(self):
        """Менеджер добавил машину, мониторинг включён, есть полная проверка
        (GİB 500), клиент привязан по ссылке."""
        await self.controller.handle_text(_PLATE, chat_id=_FORMER, telegram_user_id=_FORMER)
        car = self.garage.get_owned_car_by_number(_FORMER, _PLATE)
        self.subscriptions.enable(telegram_user_id=_FORMER, telegram_chat_id=_FORMER, plate=_PLATE,
                                  next_check_at=_T0 - timedelta(hours=1))
        self.runs.save(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 - timedelta(days=1)),
                       telegram_user_id=_FORMER, telegram_chat_id=_FORMER, initiator="monitoring_21:00")
        invite = self.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER,
                                                           telegram_user_id=_FORMER)
        token = re.search(r"start=trlink_([A-Za-z0-9_-]+)", invite.text).group(1)
        opened = await self.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
        confirmed = self.controller.handle_client_link_answer(opened.client_link_confirm_id, True,
                                                              telegram_user_id=_CLIENT)
        return car, confirmed.car_card, self.links.active_for_anchor(_FORMER, _PLATE)

    def admin(self, action, first, extra=None, token=None, *, user=_ADMIN):
        return self.controller.handle_orphan_action(action, first, extra, token, telegram_user_id=user)

    def close(self):
        for repo in (self.states, self.known, self.garage, self.runs, self.subscriptions):
            repo.close()
        self.links.close_db()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fx(tmp_path)
    yield fixture
    fixture.close()


def _due(fx, owner):
    subscription = fx.subscriptions.get(telegram_user_id=owner, plate=_PLATE)
    fx.subscriptions.mark_checked(subscription.id, checked_at=_T0, next_check_at=_T0)


def _active_subscriptions(fx):
    conn = sqlite3.connect(fx.db)
    try:
        return conn.execute(
            "SELECT telegram_user_id FROM turkey_monitoring_subscriptions WHERE plate = ? AND active = 1", (_PLATE,),
        ).fetchall()
    finally:
        conn.close()


# ---- Бывший менеджер теряет управление, клиент — нет ----

async def test_former_manager_loses_every_management_action(fx):
    car, _client_car, _link = await fx.linked_car()
    fx.demote()
    for action in ("screen", "invite", "unlink", "unlink_yes", "replace", "name", "name_clear", "cancel"):
        assert fx.controller.handle_client_link_action(action, car.id, 0, None, chat_id=_FORMER,
                                                       telegram_user_id=_FORMER) is None
    # И другой действующий менеджер этой привязкой управлять не может — только администратор.
    assert fx.controller.handle_client_link_action("unlink_yes", car.id, 0, None, chat_id=_MANAGER2,
                                                   telegram_user_id=_MANAGER2) is None
    detail = fx.controller.handle_manager_car_open(car.id, 0, telegram_user_id=_MANAGER2)
    assert detail.manager_car_detail_car_id is None
    assert fx.links.active_for_anchor(_FORMER, _PLATE) is not None  # никого не назначили, ничего не закрыли


async def test_client_keeps_the_car_and_notifications(fx):
    _car, client_car, _link = await fx.linked_car()
    fx.demote()
    assert [c.car_number for c in fx.garage.list_cars(_CLIENT)] == [_PLATE]
    card = fx.controller.handle_car_open(client_car.id, telegram_user_id=_CLIENT)
    assert card.car_card_monitoring_active is True
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert sorted(chat for chat, _ in fx.notifier.sent) == [_CLIENT, _FORMER]


async def test_invites_of_a_former_manager_cannot_be_confirmed(fx):
    await fx.controller.handle_text(_PLATE, chat_id=_FORMER, telegram_user_id=_FORMER)
    car = fx.garage.get_owned_car_by_number(_FORMER, _PLATE)
    invite = fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER,
                                                     telegram_user_id=_FORMER)
    token = re.search(r"start=trlink_([A-Za-z0-9_-]+)", invite.text).group(1)
    fx.demote()
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert opened.text == texts.CLIENT_LINK_INVALID_TEXT
    # Запрос по @username/выбору, созданный до потери статуса, тоже не подтверждается.
    fx.trusted.add(_FORMER)
    fx.controller = fx.build_controller()
    fx.known.record_seen(telegram_user_id=_STRANGER, telegram_chat_id=_STRANGER, telegram_username=None)
    request = fx.controller.handle_client_link_action("pick_user", car.id, 0, _STRANGER, chat_id=_FORMER,
                                                      telegram_user_id=_FORMER)
    link_id = request.outgoing_messages[0].confirm_link_id
    fx.demote()
    assert fx.controller.handle_client_link_answer(link_id, True, telegram_user_id=_STRANGER).text \
        == texts.CLIENT_LINK_INVALID_TEXT
    assert fx.links.active_links() == []


# ---- Только администратор ----

async def test_orphan_screens_are_admin_only(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.demote()
    for user in (_MANAGER2, _CLIENT, _FORMER):
        assert fx.admin("list", 0, user=user) is None
        assert fx.admin("disable", link.id, user=user) is None
        assert fx.admin("transfer_to", link.id, _MANAGER2, user=user) is None
    not_admin = await fx.controller.handle_text(texts.ORPHAN_LINKS_LABEL, chat_id=_MANAGER2, telegram_user_id=_MANAGER2)
    assert not_admin.orphan_list is None
    labels = lambda rows: [getattr(button, "button", button).text for row in rows for button in row]  # noqa: E731
    assert texts.ORPHAN_LINKS_LABEL in labels(main_menu_keyboard(is_admin=True))
    assert texts.ORPHAN_LINKS_LABEL in labels(main_menu_keyboard(is_trusted=True, is_admin=True))
    assert texts.ORPHAN_LINKS_LABEL not in labels(main_menu_keyboard(is_trusted=True))


async def test_admin_list_shows_only_links_without_a_manager(fx):
    _car, _client_car, link = await fx.linked_car()
    assert fx.admin("list", 0).text == texts.ORPHAN_LINKS_EMPTY_TEXT  # менеджер ещё действующий
    fx.demote()
    listed = await fx.controller.handle_text(texts.ORPHAN_LINKS_LABEL, chat_id=_ADMIN, telegram_user_id=_ADMIN)
    assert listed.orphan_list == (0, 1, ((link.id, _PLATE),))
    assert "👤 Клиент: Client (@client_one)" in listed.text and "👔 Бывший менеджер: Former (@former_mgr)" in listed.text


# ---- ❌ Отключить ----

async def test_disable_needs_a_fresh_one_time_token(fx):
    car, _client_car, link = await fx.linked_car()
    fx.demote()
    runs_before = fx.runs.count_total()
    prompt = fx.admin("disable", link.id)
    _link_id, action, _extra, token, _back = prompt.orphan_confirm
    assert action == "disable_yes"
    assert fx.admin("disable_yes", link.id, token="00000000").text.startswith(texts.ORPHAN_STALE_TEXT)
    assert fx.links.active_for_anchor(_FORMER, _PLATE) is not None  # неверный токен сжёг и верный
    token = fx.admin("disable", link.id).orphan_confirm[3]
    done = fx.admin("disable_yes", link.id, token=token)
    assert done.text.startswith(texts.format_orphan_disabled(_PLATE))
    assert [m.chat_id for m in done.outgoing_messages] == [_CLIENT]
    assert fx.links.active_links() == []
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None  # машину создавала привязка
    assert fx.admin("disable_yes", link.id, token=token).text.startswith(texts.ORPHAN_STALE_TEXT)  # повтор
    # Машина и мониторинг бывшего менеджера, история — как были.
    assert fx.garage.get_owned_car_by_number(_FORMER, _PLATE) is not None
    assert fx.subscriptions.get(telegram_user_id=_FORMER, plate=_PLATE).active
    assert fx.runs.count_total() == runs_before


# ---- 👤 Передать менеджеру ----

async def test_transfer_to_a_manager_who_has_the_car_reanchors_without_resends(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.garage.add_car(telegram_user_id=_MANAGER2, car_number=_PLATE)
    fx.subscriptions.enable(telegram_user_id=_MANAGER2, telegram_chat_id=_MANAGER2, plate=_PLATE, next_check_at=_T0)
    fx.runs.save(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 - timedelta(hours=2)),
                 telegram_user_id=_MANAGER2, telegram_chat_id=_MANAGER2, initiator="monitoring_21:00")
    fx.demote()
    baseline_before = fx.links.get(link.id).baseline

    prompt = fx.admin("transfer_to", link.id, _MANAGER2)
    assert "уже есть эта машина" in prompt.text
    done = fx.admin("transfer_yes", link.id, _MANAGER2, prompt.orphan_confirm[3])
    assert "передана менеджеру Second (@manager_two)" in done.text
    assert [m.chat_id for m in done.outgoing_messages] == [_MANAGER2]
    moved = fx.links.get(link.id)
    assert moved.owner_telegram_user_id == _MANAGER2 and moved.baseline == baseline_before

    # То же состояние — клиенту ничего (без повторов); изменение — клиенту один раз.
    fx.check.results += [_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 + timedelta(hours=1))] * 2
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent if chat == _CLIENT] == []
    _due(fx, _MANAGER2)
    _due(fx, _FORMER)
    fx.check.results += [_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=1))] * 2
    await fx.monitoring.run_scheduled_batch("21:00")
    assert len([chat for chat, _ in fx.notifier.sent if chat == _CLIENT]) == 1
    # Новый менеджер управляет привязкой.
    car2 = fx.garage.get_owned_car_by_number(_MANAGER2, _PLATE)
    assert fx.controller.handle_client_link_action("screen", car2.id, 0, None, chat_id=_MANAGER2,
                                                   telegram_user_id=_MANAGER2) is not None


async def test_transfer_to_a_manager_without_the_car_moves_the_single_monitoring(fx):
    car, _client_car, link = await fx.linked_car()
    fx.garage.set_client_display_name(car.id, "Иван Иванов")
    fx.demote()
    prompt = fx.admin("transfer_to", link.id, _MANAGER2)
    assert "перейдут этому менеджеру" in prompt.text
    done = fx.admin("transfer_yes", link.id, _MANAGER2, prompt.orphan_confirm[3])
    assert "передана" in done.text and "выключен" not in done.text

    assert fx.garage.get_owned_car_by_number(_FORMER, _PLATE) is None
    car2 = fx.garage.get_owned_car_by_number(_MANAGER2, _PLATE)
    assert car2.id == car.id and fx.garage.get_client_display_name(car2.id) == "Иван Иванов"
    assert _active_subscriptions(fx) == [(_MANAGER2,)]  # проверка номера одна

    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.check.calls == [_PLATE]
    assert [chat for chat, _ in fx.notifier.sent if chat == _CLIENT] == []  # старое не повторяется


async def test_transfer_refusals_never_assign_anyone(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.demote()
    # Не менеджер — отказ, привязка остаётся на месте.
    assert fx.admin("transfer_to", link.id, _STRANGER).text.startswith(texts.ORPHAN_REFUSAL_TEXTS["not_manager"])
    # Выбор — только из действующих менеджеров, без бывшего.
    picker = fx.admin("transfer", link.id)
    assert {manager_id for manager_id, _ in picker.orphan_picker[1]} == {_MANAGER2, _MANAGER3}
    # Токен выдан для одного менеджера — для другого не подходит.
    token = fx.admin("transfer_to", link.id, _MANAGER2).orphan_confirm[3]
    assert fx.admin("transfer_yes", link.id, _MANAGER3, token).text.startswith(texts.ORPHAN_STALE_TEXT)
    assert fx.links.get(link.id).owner_telegram_user_id == _FORMER


async def test_transfer_conflict_and_a_restored_manager(fx):
    car, _client_car, link = await fx.linked_car()
    car2 = fx.garage.add_car(telegram_user_id=_MANAGER2, car_number=_PLATE)
    other = fx.service.request_user(car2, created_by=_MANAGER2, client_telegram_user_id=_STRANGER, method="pick")
    fx.known.record_seen(telegram_user_id=_STRANGER, telegram_chat_id=_STRANGER, telegram_username=None)
    assert fx.service.confirm(other.id, telegram_user_id=_STRANGER).status == "ok"
    fx.demote()
    assert fx.admin("transfer_to", link.id, _MANAGER2).text.startswith(texts.ORPHAN_REFUSAL_TEXTS["conflict"])
    # Статус менеджера вернули — привязка снова его, администратор её не трогает.
    token = fx.admin("disable", link.id).orphan_confirm[3]
    fx.trusted.add(_FORMER)
    fx.controller = fx.build_controller()
    fx.controller._orphan_tokens[(_ADMIN, link.id)] = ("disable_yes", None, token)
    refused = fx.admin("disable_yes", link.id, token=token)
    assert refused.text.startswith(texts.ORPHAN_REFUSAL_TEXTS["not_orphan"])
    assert fx.links.active_for_anchor(_FORMER, _PLATE) is not None


def test_orphan_callbacks_roundtrip_and_fit_telegram():
    cases = [
        ("list", 3, None, None), ("disable", 12, None, None), ("disable_yes", 12, None, "0a1b2c3d"),
        ("transfer", 12, None, None), ("transfer_to", 12, 9876543210, None), ("transfer_yes", 12, 9876543210, "0a1b2c3d"),
    ]
    for action, first, extra, token in cases:
        data = encode_orphan_callback(action, first, extra, token)
        assert len(data) <= 64 and decode_orphan_callback(data) == (action, first, extra, token)
    for garbage in (b"trodisy:12:XYZ", b"trodisy:12", b"trotry:1:2", b"trolst:-1", b"trotrm:a:b", None):
        assert decode_orphan_callback(garbage) is None
