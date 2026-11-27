"""🔎 Найти привязку по номеру (администратор Turkey-бота) и чек-лист
безопасности владельца для привязок клиентов.

Тот же фикстур, что и в test_turkey_orphan_links.py: настоящие репозитории
поверх SQLite, фейковые проверки и уведомления."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from telethon import events

from reader.turkey_bot import texts
from reader.turkey_bot.handlers import register
from reader.turkey_bot.keyboards import (
    decode_admin_search_results_callback,
    encode_admin_search_results_callback,
    encode_client_link_callback,
    encode_orphan_callback,
)
from reader.turkey_bot.unified.models import ProviderStatus
from tests.test_turkey_client_linking import _CLIENT, _PLATE, _T0, _result
from tests.test_turkey_orphan_links import _ADMIN, _FORMER, _MANAGER2, _MANAGER3, _STRANGER, _Fx, _active_subscriptions, _due

_CLIENT2 = 700002


@pytest.fixture
def fx(tmp_path):
    fixture = _Fx(tmp_path)
    yield fixture
    fixture.close()


async def _search(fx, raw_plate, user=_ADMIN):
    await fx.controller.handle_text(texts.ADMIN_LINK_SEARCH_LABEL, chat_id=user, telegram_user_id=user)
    return await fx.controller.handle_text(raw_plate, chat_id=user, telegram_user_id=user)


async def _second_manager_link(fx, client=_CLIENT2):
    """Тот же номер у второго (действующего) менеджера, свой клиент."""
    fx.known.record_seen(telegram_user_id=client, telegram_chat_id=client, telegram_username="client_two",
                         first_name="Client2")
    car2 = fx.garage.add_car(telegram_user_id=_MANAGER2, car_number=_PLATE)
    fx.subscriptions.enable(telegram_user_id=_MANAGER2, telegram_chat_id=_MANAGER2, plate=_PLATE, next_check_at=_T0)
    sent = fx.controller.handle_client_link_action("pick_user", car2.id, 0, client, chat_id=_MANAGER2,
                                                   telegram_user_id=_MANAGER2)
    fx.controller.handle_client_link_answer(sent.outgoing_messages[0].confirm_link_id, True, telegram_user_id=client)
    return car2, fx.links.active_for_anchor(_MANAGER2, _PLATE)


# ---- Поиск по номеру ----

async def test_admin_finds_every_link_of_a_plate_with_manager_rights_and_monitoring(fx):
    car, _client_car, link = await fx.linked_car()
    await _second_manager_link(fx)
    car3 = fx.garage.add_car(telegram_user_id=_MANAGER3, car_number=_PLATE)
    fx.controller.handle_client_link_action("invite", car3.id, 0, None, chat_id=_MANAGER3, telegram_user_id=_MANAGER3)
    fx.subscriptions.disable(telegram_user_id=_FORMER, plate=_PLATE)
    fx.demote()

    found = await _search(fx, "34 abc 123")  # та же нормализация, что и при добавлении машины
    assert found.text.startswith(f"🔎 Привязки по номеру {texts.format_plate_for_telegram(_PLATE)}: 3")
    assert "👔 Менеджер: Former (@former_mgr) — права сняты" in found.text
    assert "👔 Менеджер: Second (@manager_two) — действующий" in found.text
    assert "👤 Клиент: Client (@client_one) · привязан 20.11.2026" in found.text
    assert "⏳ Ждёт подтверждения до" in found.text and "👤 Приглашение по ссылке — ещё не открыто" in found.text
    assert "📡 Мониторинг: ⚪ выключен" in found.text and "📡 Мониторинг: 🟢 включён" in found.text
    assert {plate for _id, plate in found.admin_link_search} == {_PLATE} and len(found.admin_link_search) == 2


async def test_search_is_admin_only_and_handles_bad_or_empty_input(fx):
    await fx.linked_car()
    prompt = await fx.controller.handle_text(texts.ADMIN_LINK_SEARCH_LABEL, chat_id=_MANAGER2, telegram_user_id=_MANAGER2)
    state = fx.states.get(_MANAGER2)
    assert prompt.text != texts.ADMIN_LINK_SEARCH_PROMPT
    assert state is None or state.step != "awaiting_admin_link_plate"  # не админ — обычный ввод номера
    not_admin = await _search(fx, _PLATE, user=_MANAGER2)
    assert not_admin.admin_link_search is None
    assert fx.controller.handle_admin_link_search_results(_PLATE, telegram_user_id=_MANAGER2) is None
    bad = await _search(fx, "!!")
    assert bad.text == texts.INVALID_PLATE_TEXT and bad.show_cancel_button
    empty = await fx.controller.handle_text("06XYZ99", chat_id=_ADMIN, telegram_user_id=_ADMIN)
    assert empty.text == texts.format_admin_link_search_empty("06XYZ99")


async def test_admin_unlinks_a_trusted_managers_link_without_touching_another(fx):
    _car, _client_car, link = await fx.linked_car()
    _car2, link2 = await _second_manager_link(fx)
    own = fx.subscriptions.enable(telegram_user_id=_CLIENT2, telegram_chat_id=_CLIENT2, plate=_PLATE,
                                  next_check_at=_T0 + timedelta(days=30))  # своя подписка клиента 2
    prompt = fx.admin("s_disable", link.id)
    _lid, action, _extra, token, back = prompt.orphan_confirm
    assert action == "s_disable_yes" and decode_admin_search_results_callback(back) == _PLATE
    done = fx.admin("s_disable_yes", link.id, token=token)
    assert done.text.startswith(texts.format_orphan_disabled(_PLATE)) and done.admin_link_search is not None
    assert fx.links.get(link.id).status == "revoked"
    assert fx.links.get(link2.id).status == "active"  # чужая привязка цела
    assert fx.subscriptions.get(telegram_user_id=_CLIENT2, plate=_PLATE).active == own.active


async def test_admin_transfers_a_trusted_managers_link_to_another_manager(fx):
    car, _client_car, link = await fx.linked_car()   # владелец — действующий менеджер
    picker = fx.admin("s_transfer", link.id)
    assert _FORMER not in {manager_id for manager_id, _ in picker.orphan_picker[1]}  # не самому себе
    prompt = fx.admin("s_transfer_to", link.id, _MANAGER2)
    done = fx.admin("s_transfer_yes", link.id, _MANAGER2, prompt.orphan_confirm[3])
    assert "передана менеджеру Second (@manager_two)" in done.text
    assert fx.links.get(link.id).owner_telegram_user_id == _MANAGER2
    assert _active_subscriptions(fx) == [(_MANAGER2,)]  # машина и её мониторинг перешли, проверка одна
    assert fx.garage.get_owned_car_by_number(_FORMER, _PLATE) is None
    assert fx.garage.get_owned_car_by_number(_MANAGER2, _PLATE).id == car.id


async def test_moving_the_car_cancels_the_old_owners_open_invite(fx):
    car, _client_car, link = await fx.linked_car()
    invite = fx.controller.handle_client_link_action("replace", car.id, 0, None, chat_id=_FORMER,
                                                     telegram_user_id=_FORMER)
    assert invite is not None
    fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    assert fx.links.pending_for_anchor(_FORMER, _PLATE) is not None
    prompt = fx.admin("s_transfer_to", link.id, _MANAGER2)
    fx.admin("s_transfer_yes", link.id, _MANAGER2, prompt.orphan_confirm[3])
    assert fx.links.pending_for_anchor(_FORMER, _PLATE) is None


async def test_scoped_tokens_do_not_cross_over(fx):
    _car, _client_car, link = await fx.linked_car()
    fx.demote()
    orphan_token = fx.admin("disable", link.id).orphan_confirm[3]
    assert fx.admin("s_disable_yes", link.id, token=orphan_token).text.startswith(texts.ORPHAN_STALE_TEXT)
    assert fx.links.get(link.id).status == "active"


def test_search_callbacks_fit_telegram():
    data = encode_admin_search_results_callback("A123AA123")
    assert len(data) <= 64 and decode_admin_search_results_callback(data) == "A123AA123"
    assert decode_admin_search_results_callback(b"trosr:../x") is None
    assert len(encode_orphan_callback("s_transfer_yes", 999999, 9876543210, "0a1b2c3d")) <= 64


# ---- Чек-лист безопасности владельца ----

class _Client:
    def __init__(self):
        self.callback_handler = None
        self.sent = []

    def on(self, event_filter):
        def decorator(func):
            if isinstance(event_filter, events.CallbackQuery):
                self.callback_handler = func
            return func
        return decorator

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))


class _Event:
    def __init__(self, data, sender_id):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = True
        self.answers = []
        self.edits = []

    async def get_sender(self):
        return None

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit(self, text, *, buttons=None):
        self.edits.append(text)

    async def respond(self, text, *, buttons=None):
        self.edits.append(text)


async def test_former_manager_old_buttons_are_refused(fx):
    car, _client_car, link = await fx.linked_car()
    fx.demote()
    client = _Client()
    register(client, fx.controller, fx.known)
    for data in (
        encode_client_link_callback("unlink_yes", car.id, 0), encode_client_link_callback("invite", car.id, 0),
        encode_client_link_callback("name_clear", car.id, 0), encode_client_link_callback("replace", car.id, 0),
        encode_orphan_callback("disable", link.id),
    ):
        event = _Event(data, _FORMER)
        await client.callback_handler(event)
        assert event.answers == [((texts.UNKNOWN_BUTTON_TEXT,), {"alert": True})] and event.edits == []
    assert fx.links.get(link.id).status == "active" and client.sent == []


async def test_repeated_invite_creates_no_duplicates(fx):
    car, _client_car, _link = await fx.linked_car()
    first = fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    second = fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    old_token = re.search(r"trlink_([A-Za-z0-9_-]+)", first.text).group(1)
    new_token = re.search(r"trlink_([A-Za-z0-9_-]+)", second.text).group(1)
    stale = await fx.controller.handle_text(f"/start trlink_{old_token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert stale.text == texts.CLIENT_LINK_INVALID_TEXT
    opened = await fx.controller.handle_text(f"/start trlink_{new_token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    fx.controller.handle_client_link_answer(opened.client_link_confirm_id, True, telegram_user_id=_CLIENT)
    fx.controller.handle_client_link_answer(opened.client_link_confirm_id, True, telegram_user_id=_CLIENT)
    assert len(fx.links.links_for_plate(_PLATE)) == 1 and len(fx.garage.list_cars(_CLIENT)) == 1


async def test_no_link_and_no_client_data_without_confirmation(fx):
    await fx.controller.handle_text(_PLATE, chat_id=_FORMER, telegram_user_id=_FORMER)
    car = fx.garage.get_owned_car_by_number(_FORMER, _PLATE)
    fx.runs.save(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 - timedelta(days=1)),
                 telegram_user_id=_FORMER, telegram_chat_id=_FORMER, initiator="monitoring_21:00")
    invite = fx.controller.handle_client_link_action("invite", car.id, 0, None, chat_id=_FORMER, telegram_user_id=_FORMER)
    token = re.search(r"trlink_([A-Za-z0-9_-]+)", invite.text).group(1)
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert "500" not in opened.text and fx.links.active_links() == [] and fx.garage.list_cars(_CLIENT) == []
    fx.now = _T0 + timedelta(hours=72, seconds=1)  # истекло до подтверждения
    expired = fx.controller.handle_client_link_answer(opened.client_link_confirm_id, True, telegram_user_id=_CLIENT)
    assert expired.text == texts.CLIENT_LINK_EXPIRED_TEXT and fx.links.active_links() == []


async def test_client_notifications_do_not_depend_on_the_former_manager(fx):
    _car, _client_car, _link = await fx.linked_car()
    fx.demote()

    class _FormerBlockedBot:
        def __init__(self):
            self.sent = []

        async def send(self, *, telegram_chat_id, text):
            if telegram_chat_id == _FORMER:
                raise RuntimeError("bot blocked by user")
            self.sent.append((telegram_chat_id, text))

    blocked = _FormerBlockedBot()
    fx.monitoring._notifier = blocked
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in blocked.sent] == [_CLIENT]


async def test_next_new_event_reaches_manager_and_client_exactly_once_before_and_after_transfer(fx):
    """Интеграционный: привязка → старое состояние никому не повторяется →
    новое событие: менеджер и клиент — ровно по одному сообщению; бывший
    менеджер лишился прав, привязка передана → следующее новое событие:
    новый менеджер и клиент — снова ровно по одному, без старых."""
    _car, _client_car, link = await fx.linked_car()

    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.notifier.sent == []  # исторический долг не приходит как новый

    _due(fx, _FORMER)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert sorted(chat for chat, _ in fx.notifier.sent) == [_CLIENT, _FORMER]

    fx.demote()
    prompt = fx.admin("transfer_to", link.id, _MANAGER2)
    fx.admin("transfer_yes", link.id, _MANAGER2, prompt.orphan_confirm[3])
    runs_before = fx.runs.count_total()
    fx.notifier.sent.clear()

    _due(fx, _MANAGER2)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=2)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_MANAGER2]  # клиенту 800 уже приходило — не повторяется
    # (новому менеджеру — один раз как первое знакомство с машиной: его своя точка отсчёта пуста)

    fx.notifier.sent.clear()
    _due(fx, _MANAGER2)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(1200), at=_T0 + timedelta(days=3)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert sorted(chat for chat, _ in fx.notifier.sent) == [_CLIENT, _MANAGER2]
    assert fx.runs.count_total() == runs_before + 2  # история цела и дописывается
    assert fx.check.calls.count(_PLATE) == len(fx.check.calls)
