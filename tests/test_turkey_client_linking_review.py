"""Привязка клиента в Turkey-боте: случаи из независимого review.

Тот же фикстур, что и в test_turkey_client_linking.py (настоящие
репозитории, фейковые проверки и уведомления)."""

from datetime import timedelta
from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from tests.test_turkey_client_linking import _CLIENT, _MANAGER, _OTHER, _PLATE, _T0, _Fixture, _result

_SECOND_OWNER = 900002


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _provider(name, total, at, status=ProviderStatus.HAS_DEBT):
    return ProviderCheckResult(
        provider=name, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total, items=(),
        error_type="transport_error" if status == ProviderStatus.ERROR else None, checked_at=at,
    )


def _multi(*providers, at):
    return UnifiedCheckResult(
        plate=_PLATE, started_at=at, finished_at=at, overall_status=derive_overall_status(providers),
        total_amount=total_amount_for(providers), providers=providers,
    )


def _due_again(fx, owner=_MANAGER):
    subscription = fx.subscriptions.get(telegram_user_id=owner, plate=_PLATE)
    fx.subscriptions.mark_checked(subscription.id, checked_at=_T0, next_check_at=_T0)


# ---- #1: тот же клиент подтверждает новое приглашение ----

async def test_same_client_reconfirming_keeps_the_car_and_state(fx):
    car, _ = await fx.manager_car()
    first = await fx.link_client(car)
    fx.action("replace", car)
    again = await fx.link_client(car)  # новая ссылка, тот же клиент
    assert again.car_card is not None
    assert [c.car_number for c in fx.garage.list_cars(_CLIENT)] == [_PLATE]
    assert fx.links.active_for_anchor(_MANAGER, _PLATE).client_car_created is True
    assert again.outgoing_messages and all(m.chat_id != _CLIENT for m in again.outgoing_messages)
    # Флаг перенесён: отвязка по-прежнему убирает машину, созданную привязкой.
    fx.action("unlink_yes", car)
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None
    assert first.car_card.id == again.car_card.id


# ---- #2: якорь — только своя машина менеджера; чужой мониторинг не трогаем ----

async def test_regular_users_car_and_linked_car_cannot_be_anchors(fx):
    car, _ = await fx.manager_car()
    confirmed = await fx.link_client(car)
    client_row = confirmed.car_card  # строка, пришедшая привязкой
    await fx.controller.handle_text("06USR77", chat_id=_OTHER, telegram_user_id=_OTHER)
    user_row = fx.garage.get_owned_car_by_number(_OTHER, "06USR77")
    for row in (client_row, user_row):
        assert fx.controller.handle_client_link_action("invite", row.id, 0, None, chat_id=_MANAGER,
                                                       telegram_user_id=_MANAGER) is None
        detail = fx.controller.handle_manager_car_open(row.id, 0, telegram_user_id=_MANAGER)
        assert detail.manager_car_detail_car_id is None and "Telegram:" not in detail.text


async def test_client_play_never_creates_a_second_subscription(fx):
    car, _ = await fx.manager_car()
    confirmed = await fx.link_client(car)
    fx.subscriptions.disable(telegram_user_id=_MANAGER, plate=_PLATE)
    card = await fx.controller.handle_car_action("monitor_on", confirmed.car_card.id, chat_id=_CLIENT,
                                                 telegram_user_id=_CLIENT)
    assert fx.subscriptions.get(telegram_user_id=_CLIENT, plate=_PLATE) is None
    assert card.car_card_monitoring_active is False and texts.CLIENT_LINK_CARD_LINE_ANCHOR_OFF in card.text
    # Менеджер возобновил мониторинг — номер проверяется один раз, клиент снова получает копию.
    fx.subscriptions.enable(telegram_user_id=_MANAGER, telegram_chat_id=_MANAGER, plate=_PLATE, next_check_at=_T0)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.check.calls == [_PLATE]
    assert sorted(chat for chat, _ in fx.notifier.sent) == [_CLIENT, _MANAGER]


# ---- #3: менеджер удалил свою машину ----

async def test_manager_deleting_the_car_closes_the_link(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    fx.action("pick_user", car, _OTHER)  # висящее приглашение тоже закрывается
    runs_before = fx.runs.count_total()
    reply = await fx.controller.handle_car_action("delete_confirm", car.id, chat_id=_MANAGER, telegram_user_id=_MANAGER)
    assert [m.chat_id for m in reply.outgoing_messages] == [_CLIENT]
    assert fx.links.active_for_anchor(_MANAGER, _PLATE) is None
    assert fx.links.pending_for_anchor(_MANAGER, _PLATE) is None
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None
    assert fx.runs.count_total() == runs_before
    # Повторное добавление номера не оживляет старую привязку.
    await fx.controller.handle_text(_PLATE, chat_id=_MANAGER, telegram_user_id=_MANAGER)
    assert fx.links.active_for_anchor(_MANAGER, _PLATE) is None


# ---- #4: один клиент, два владельца с тем же номером ----

async def test_client_linked_to_two_owners_gets_one_message(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    second_car = fx.garage.add_car(telegram_user_id=_SECOND_OWNER, car_number=_PLATE)
    fx.subscriptions.enable(telegram_user_id=_SECOND_OWNER, telegram_chat_id=_SECOND_OWNER, plate=_PLATE,
                            next_check_at=_T0)
    link = fx.service.request_user(second_car, created_by=_MANAGER, client_telegram_user_id=_CLIENT, method="pick")
    assert fx.service.confirm(link.id, telegram_user_id=_CLIENT).status == "ok"

    for _ in range(2):  # у каждого владельца своя проверка (как и раньше)
        fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    to_client = [text for chat, text in fx.notifier.sent if chat == _CLIENT]
    assert len(to_client) == 1


# ---- #5: часть сообщений не доставлена ----

async def test_partial_delivery_resends_only_what_failed(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    at = _T0 + timedelta(hours=1)
    result = _multi(_provider("gib", Decimal(800), at), _provider("kgm", Decimal(70), at), at=at)

    class _KgmFails:
        def __init__(self):
            self.sent = []

        async def send(self, *, telegram_chat_id, text):
            if telegram_chat_id == _CLIENT and "KGM" in text:
                raise RuntimeError("blocked")
            self.sent.append((telegram_chat_id, text))

    flaky = _KgmFails()
    fx.monitoring._notifier = flaky
    fx.check.results.append(result)
    await fx.monitoring.run_scheduled_batch("21:00")
    client_first = [text for chat, text in flaky.sent if chat == _CLIENT]
    assert len(client_first) == 1 and "KGM" not in client_first[0]

    fx.monitoring._notifier = fx.notifier
    _due_again(fx)
    later = _T0 + timedelta(days=1)
    fx.check.results.append(_multi(_provider("gib", Decimal(800), later), _provider("kgm", Decimal(70), later), at=later))
    await fx.monitoring.run_scheduled_batch("21:00")
    client_second = [text for chat, text in fx.notifier.sent if chat == _CLIENT]
    assert len(client_second) == 1 and "KGM" in client_second[0]  # только недоставленное, без повтора GİB


# ---- #7, #8: экран подтверждения ----

async def test_confirmation_shows_the_last_reliable_state_and_the_menu(fx):
    car, _ = await fx.manager_car()  # надёжная проверка: GİB 500
    error_at = _T0 - timedelta(hours=1)
    fx.runs.save(_multi(_provider("gib", Decimal(0), error_at, ProviderStatus.ERROR), at=error_at),
                 telegram_user_id=_MANAGER, telegram_chat_id=_MANAGER, initiator="manual")
    confirmed = await fx.link_client(car)
    assert "500" in confirmed.text and "ошибк" not in confirmed.text.lower()
    assert confirmed.extra_texts == (texts.CLIENT_LINK_MENU_HINT,) and confirmed.show_main_menu is True
