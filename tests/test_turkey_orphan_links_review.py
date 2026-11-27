"""Привязки без менеджера (Turkey bot): случаи из независимого review."""

import re
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.handlers import register
from reader.turkey_bot.keyboards import encode_orphan_callback
from reader.turkey_bot.unified.models import ProviderStatus
from tests.test_turkey_admin_link_search import _Client, _Event
from tests.test_turkey_client_linking import _CLIENT, _PLATE, _T0, _result
from tests.test_turkey_orphan_links import _ADMIN, _FORMER, _MANAGER2, _STRANGER, _Fx, _active_subscriptions, _due


@pytest.fixture
def fx(tmp_path):
    fixture = _Fx(tmp_path)
    yield fixture
    fixture.close()


def _transfer(fx, link_id, manager):
    prompt = fx.admin("transfer_to", link_id, manager)
    return fx.admin("transfer_yes", link_id, manager, prompt.orphan_confirm[3])


# ---- #1: открытое приглашение бывшего менеджера после передачи/отключения ----

async def test_open_invite_cannot_bind_to_a_car_that_was_moved_away(fx):
    car, _client_car, link = await fx.linked_car()
    invite = fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    token = re.search(r"trlink_([A-Za-z0-9_-]+)", invite.text).group(1)
    fx.demote()
    _transfer(fx, link.id, _MANAGER2)                 # машина ушла к менеджеру 2
    fx.trusted.add(_FORMER)                           # статус вернули в течение 72 ч
    fx.controller = fx.build_controller()
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_STRANGER, telegram_user_id=_STRANGER)
    assert opened.text == texts.CLIENT_LINK_INVALID_TEXT
    assert fx.links.active_for_anchor(_FORMER, _PLATE) is None


async def test_confirm_refuses_when_the_anchor_car_is_gone(fx):
    await fx.controller.handle_text(_PLATE, chat_id=_FORMER, telegram_user_id=_FORMER)
    car = fx.garage.get_owned_car_by_number(_FORMER, _PLATE)
    fx.known.record_seen(telegram_user_id=_STRANGER, telegram_chat_id=_STRANGER, telegram_username=None)
    request = fx.controller.handle_client_link_action("pick_user", car.id, 0, _STRANGER, chat_id=_FORMER,
                                                      telegram_user_id=_FORMER)
    fx.garage.delete_car(car.id, telegram_user_id=_FORMER)  # машины больше нет
    link_id = request.outgoing_messages[0].confirm_link_id
    assert fx.controller.handle_client_link_answer(link_id, True, telegram_user_id=_STRANGER).text \
        == texts.CLIENT_LINK_INVALID_TEXT
    assert fx.links.active_links() == []


async def test_admin_disable_cancels_the_anchors_open_invite(fx):
    car, _client_car, link = await fx.linked_car()
    fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    fx.demote()
    fx.admin("disable_yes", link.id, token=fx.admin("disable", link.id).orphan_confirm[3])
    assert fx.links.pending_for_anchor(_FORMER, _PLATE) is None


# ---- #2: новому менеджеру старый долг не приходит как новый ----

async def test_moved_car_keeps_the_managers_reference_state(fx):
    _car, _client_car, link = await fx.linked_car()   # история: GİB 500 у бывшего менеджера
    fx.demote()
    runs_before = fx.runs.count_total()
    _transfer(fx, link.id, _MANAGER2)
    assert fx.runs.count_total() == runs_before       # история не теряется и не копируется
    _due(fx, _MANAGER2)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.notifier.sent == []                     # ни менеджеру, ни клиенту


# ---- #3: бывший менеджер не может оставить клиента без уведомлений ----

async def test_former_manager_cannot_stop_or_delete_the_anchor(fx):
    car, _client_car, _link = await fx.linked_car()
    fx.demote()
    for action in ("monitor_off", "delete_prompt", "delete_confirm"):
        reply = await fx.controller.handle_car_action(action, car.id, chat_id=_FORMER, telegram_user_id=_FORMER)
        assert reply.text.startswith(texts.CLIENT_LINK_ANCHOR_LOCKED_TEXT)
    assert fx.subscriptions.get(telegram_user_id=_FORMER, plate=_PLATE).active
    assert fx.garage.get_owned_car_by_number(_FORMER, _PLATE) is not None
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert _CLIENT in [chat for chat, _ in fx.notifier.sent]


async def test_trusted_manager_can_stop_his_anchor_and_the_admin_sees_it(fx):
    car, _client_car, link = await fx.linked_car()
    reply = await fx.controller.handle_car_action("monitor_off", car.id, chat_id=_FORMER, telegram_user_id=_FORMER)
    assert not reply.text.startswith(texts.CLIENT_LINK_ANCHOR_LOCKED_TEXT)
    assert not fx.subscriptions.get(telegram_user_id=_FORMER, plate=_PLATE).active
    await fx.controller.handle_text(texts.ADMIN_LINK_SEARCH_LABEL, chat_id=_ADMIN, telegram_user_id=_ADMIN)
    found = await fx.controller.handle_text(_PLATE, chat_id=_ADMIN, telegram_user_id=_ADMIN)
    assert "📡 Мониторинг: ⚪ выключен" in found.text
    fx.demote()
    assert "📡 Мониторинг: ⚪ выключен" in fx.admin("list", 0).text


# ---- #4: машина-цель сама пришла привязкой ----

async def test_transfer_onto_a_linked_client_car_is_refused(fx):
    _car, _client_car, link = await fx.linked_car()
    # Менеджер 2 — сам клиент другой привязки к этому номеру (его машина пришла привязкой).
    other_owner_car = fx.garage.add_car(telegram_user_id=_STRANGER, car_number=_PLATE)
    other = fx.service.request_user(other_owner_car, created_by=_STRANGER, client_telegram_user_id=_MANAGER2,
                                    method="pick")
    fx.trusted.add(_STRANGER)
    assert fx.service.confirm(other.id, telegram_user_id=_MANAGER2).status == "ok"
    fx.trusted.discard(_STRANGER)
    fx.demote()
    refused = fx.admin("transfer_to", link.id, _MANAGER2)
    assert refused.text.startswith(texts.ORPHAN_REFUSAL_TEXTS["linked_car"])
    assert fx.links.get(link.id).owner_telegram_user_id == _FORMER


# ---- #6: передача — всё или ничего ----

async def test_failed_move_changes_nothing(fx):
    car, _client_car, link = await fx.linked_car()
    fx.garage.add_car(telegram_user_id=_MANAGER2, car_number=_PLATE)  # у получателя уже есть строка
    ok = fx.links.move_anchor(link.id, old_owner=_FORMER, new_owner=_MANAGER2, plate=_PLATE, move_car=True, now=fx.now)
    assert ok is False
    assert fx.garage.get_owned_car_by_number(_FORMER, _PLATE).id == car.id
    assert fx.subscriptions.get(telegram_user_id=_FORMER, plate=_PLATE).active
    assert fx.links.get(link.id).owner_telegram_user_id == _FORMER


async def test_admin_sees_a_failed_transfer(fx, monkeypatch):
    _car, _client_car, link = await fx.linked_car()
    fx.demote()
    monkeypatch.setattr(fx.links, "move_anchor", lambda *args, **kwargs: False)
    done = _transfer(fx, link.id, _MANAGER2)
    assert done.text.startswith(texts.ORPHAN_REFUSAL_TEXTS["failed"]) and done.outgoing_messages == ()
    assert fx.links.get(link.id).owner_telegram_user_id == _FORMER


# ---- Недостающие тесты ----

async def test_move_merges_into_the_new_managers_old_subscription_row(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.subscriptions.enable(telegram_user_id=_MANAGER2, telegram_chat_id=_MANAGER2, plate=_PLATE, next_check_at=_T0)
    fx.subscriptions.disable(telegram_user_id=_MANAGER2, plate=_PLATE)  # строка осталась после удаления машины
    fx.demote()
    done = _transfer(fx, link.id, _MANAGER2)
    assert "передана" in done.text
    assert _active_subscriptions(fx) == [(_MANAGER2,)]
    assert not fx.subscriptions.get(telegram_user_id=_FORMER, plate=_PLATE).active


async def test_transfer_reports_that_monitoring_is_off(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.subscriptions.disable(telegram_user_id=_FORMER, plate=_PLATE)
    fx.demote()
    done = _transfer(fx, link.id, _MANAGER2)
    assert "⚠️ Мониторинг этой машины у менеджера выключен" in done.text
    assert _active_subscriptions(fx) == []  # состояние ВЫКЛ перешло как есть, никто его не включал


async def test_no_car_and_own_client_refusals(fx):
    car, _client_car, link = await fx.linked_car()
    fx.trusted.add(_CLIENT)
    fx.demote()
    assert fx.admin("transfer_to", link.id, _CLIENT).text.startswith(texts.ORPHAN_REFUSAL_TEXTS["own_client"])
    fx.garage.delete_car(car.id, telegram_user_id=_FORMER)  # машина исчезла в обход привязки
    assert fx.admin("transfer_to", link.id, _MANAGER2).text.startswith(texts.ORPHAN_REFUSAL_TEXTS["no_car"])


async def test_orphan_list_pages(fx):
    for index in range(6):
        plate = f"34ABC{100 + index}"
        car = fx.garage.add_car(telegram_user_id=_FORMER, car_number=plate)
        link = fx.service.request_user(car, created_by=_FORMER, client_telegram_user_id=_CLIENT, method="pick")
        assert fx.service.confirm(link.id, telegram_user_id=_CLIENT).status == "ok"
    fx.demote()
    first = fx.admin("list", 0)
    second = fx.admin("list", 1)
    assert first.orphan_list[1] == 2 and len(first.orphan_list[2]) == 5
    assert second.orphan_list[0] == 1 and len(second.orphan_list[2]) == 1
    assert fx.admin("list", 99).orphan_list[0] == 1  # страница ограничивается


async def test_handler_routes_orphan_buttons_for_admins_only(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.demote()
    client = _Client()
    register(client, fx.controller, fx.known)
    admin_event = _Event(encode_orphan_callback("list", 0), _ADMIN)
    await client.callback_handler(admin_event)
    assert admin_event.edits and admin_event.edits[0].startswith("🔗 Привязки без менеджера: 1")
    stranger = _Event(encode_orphan_callback("disable", link.id), _MANAGER2)
    await client.callback_handler(stranger)
    assert stranger.answers == [((texts.UNKNOWN_BUTTON_TEXT,), {"alert": True})] and stranger.edits == []


def test_move_anchor_is_one_transaction_on_the_shared_file(tmp_path):
    """move_anchor трогает таблицы других репозиториев — они в том же файле."""
    fx = _Fx(tmp_path)
    try:
        conn = sqlite3.connect(fx.db)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.close()
        assert {"turkey_client_links", "turkey_bot_user_cars", "turkey_monitoring_subscriptions",
                "turkey_check_runs"} <= tables
    finally:
        fx.close()
