"""👤 Привязка Telegram-клиента к автомобилю менеджера (Turkey bot).

Настоящие репозитории поверх SQLite (tmp_path), ConversationController и
TurkeyMonitoringService; проверки GİB/Avrasya/KGM — скриптованный фейк,
уведомления — записывающий фейк. Никаких HTTP/Telegram.
"""

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reader.turkey_bot import texts
from reader.turkey_bot.client_links import (
    STATUS_ACTIVE,
    STATUS_REPLACED,
    STATUS_REVOKED,
    TurkeyClientLinkRepository,
    TurkeyClientLinkService,
    TurkeyLinkedClientNotifier,
)
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import TurkeyConversationStateRepository
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.monitoring_service import TurkeyMonitoringService
from reader.turkey_bot.monitoring.subscription_repository import TurkeyMonitoringSubscriptionRepository
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

_MANAGER = 900001
_CLIENT = 700001
_OTHER = 700002
_STRANGER = 700003
_PLATE = "34ABC123"
_T0 = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


def _result(gib_status, gib_total=Decimal(0), *, at):
    """Полная проверка: GİB с заданным статусом, Avrasya и KGM — без долга."""
    providers = (
        ProviderCheckResult(
            provider="gib", status=gib_status, debt_count=1 if gib_status == ProviderStatus.HAS_DEBT else 0,
            principal_amount=gib_total, penalty_amount=Decimal(0), total_amount=gib_total,
            items=(), error_type=None, checked_at=at,
        ),
        *(
            ProviderCheckResult(
                provider=name, status=ProviderStatus.NO_DEBT, debt_count=0, principal_amount=Decimal(0),
                penalty_amount=Decimal(0), total_amount=Decimal(0), items=(), error_type=None, checked_at=at,
            )
            for name in ("avrasya", "kgm")
        ),
    )
    return UnifiedCheckResult(
        plate=_PLATE, started_at=at, finished_at=at, overall_status=derive_overall_status(providers),
        total_amount=total_amount_for(providers), providers=providers,
    )


class _ScriptedCheckService:
    def __init__(self):
        self.results: list[UnifiedCheckResult] = []
        self.calls: list[str] = []

    async def check(self, plate, **kwargs):
        self.calls.append(plate)
        return self.results.pop(0)


class _Notifier:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send(self, *, telegram_chat_id, text):
        self.sent.append((telegram_chat_id, text))


class _Fixture:
    def __init__(self, tmp_path):
        self.db = tmp_path / "users.db"
        self.now = _T0
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
            bot_username="ExampleTRbot", clock=lambda: self.now,
        )
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions,
            TurkeyStatisticsService(self.known, self.runs, self.subscriptions), self.check,
            trusted_operator_user_ids=frozenset({_MANAGER}), client_link_service=self.service,
        )
        self.monitoring = TurkeyMonitoringService(
            self.check, self.runs, self.subscriptions, self.notifier,
            linked_clients=TurkeyLinkedClientNotifier(self.links, self.runs, self.subscriptions),
        )
        for user_id, username, first in ((_CLIENT, "client_one", "Client"), (_OTHER, "client_two", "Other"),
                                         (_STRANGER, None, "Stranger"), (_MANAGER, "manager_x", "Manager")):
            self.known.record_seen(telegram_user_id=user_id, telegram_chat_id=user_id, telegram_username=username,
                                   first_name=first)

    async def manager_car(self):
        """Менеджер добавил машину и включил мониторинг; у номера уже есть
        исторический долг (до привязки клиента)."""
        reply = await self.controller.handle_text(_PLATE, chat_id=_MANAGER, telegram_user_id=_MANAGER)
        car = self.garage.get_owned_car_by_number(_MANAGER, _PLATE)
        self.subscriptions.enable(telegram_user_id=_MANAGER, telegram_chat_id=_MANAGER, plate=_PLATE,
                                  next_check_at=_T0 - timedelta(hours=1))
        self.runs.save(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 - timedelta(days=1)),
                       telegram_user_id=_MANAGER, telegram_chat_id=_MANAGER, initiator="monitoring_21:00")
        return car, reply

    def action(self, action, car, extra=None, *, user=_MANAGER):
        return self.controller.handle_client_link_action(
            action, car.id, 0, extra, chat_id=user, telegram_user_id=user,
        )

    def answer(self, link_id, *, confirm=True, user=_CLIENT):
        return self.controller.handle_client_link_answer(link_id, confirm, telegram_user_id=user)

    async def invite_token(self, car):
        reply = self.action("invite", car)
        return re.search(r"start=trlink_([A-Za-z0-9_-]+)", reply.text).group(1), reply

    async def link_client(self, car, client=_CLIENT):
        token, _ = await self.invite_token(car)
        opened = await self.controller.handle_text(f"/start trlink_{token}", chat_id=client, telegram_user_id=client)
        return self.answer(opened.client_link_confirm_id, user=client)

    def close(self):
        for repo in (self.states, self.known, self.garage, self.runs, self.subscriptions):
            repo.close()
        self.links.close_db()


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def _db_rows(fx, sql):
    conn = sqlite3.connect(fx.db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---- Менеджер: кнопка после добавления и экран привязки ----

async def test_manager_gets_link_button_after_adding_a_car_and_users_do_not(fx):
    car, reply = await fx.manager_car()
    assert reply.offer_client_link is True and reply.check_now_confirmation_car_id == car.id
    user_reply = await fx.controller.handle_text("06XYZ99", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert user_reply.offer_client_link is False


async def test_link_actions_are_manager_only_and_check_the_car(fx):
    car, _ = await fx.manager_car()
    assert fx.action("screen", car, user=_CLIENT) is None
    assert fx.controller.handle_client_link_action("screen", 999999, 0, None, chat_id=_MANAGER,
                                                   telegram_user_id=_MANAGER) is None
    screen = fx.action("screen", car)
    assert "🔗 Telegram: не привязан" in screen.text
    assert screen.client_link_screen == (car.id, 0, "none", True, False)


# ---- Ссылка-приглашение ----

async def test_invite_link_flow_confirms_once_and_shows_only_the_plate_before(fx):
    car, _ = await fx.manager_car()
    token, invite = await fx.invite_token(car)
    assert "https://t.me/ExampleTRbot?start=trlink_" in invite.text
    # В БД — только хеш токена.
    assert token not in str(_db_rows(fx, "SELECT * FROM turkey_client_links"))

    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert opened.client_link_confirm_id is not None
    assert "500" not in opened.text and "TRY" not in opened.text and "₺" not in opened.text  # ни штрафов, ни сумм
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None  # до подтверждения машины нет

    # Пересланную ссылку другой пользователь не подтвердит.
    stolen = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_OTHER, telegram_user_id=_OTHER)
    assert stolen.text == texts.CLIENT_LINK_NOT_FOR_YOU_TEXT
    assert fx.answer(opened.client_link_confirm_id, user=_OTHER).text == texts.CLIENT_LINK_NOT_FOR_YOU_TEXT

    confirmed = fx.answer(opened.client_link_confirm_id)
    assert confirmed.text.startswith("✅ Автомобиль") and confirmed.car_card is not None
    assert confirmed.car_card_monitoring_active is True
    assert [m.chat_id for m in confirmed.outgoing_messages] == [_MANAGER]
    cars = fx.controller.handle_my_cars(chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert [c.car_number for c in cars.my_cars] == [_PLATE]
    assert cars.my_cars_monitoring_active == {confirmed.car_card.id: True}

    # Повтор токена после подтверждения — недействителен.
    replay = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert replay.text == texts.CLIENT_LINK_INVALID_TEXT


async def test_expired_invite_is_refused_and_manager_can_issue_a_new_one(fx):
    car, _ = await fx.manager_car()
    token, _ = await fx.invite_token(car)
    fx.now = _T0 + timedelta(hours=73)
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert opened.text == texts.CLIENT_LINK_EXPIRED_TEXT
    screen = fx.action("screen", car)
    assert "Приглашение истекло" in screen.text and screen.client_link_screen[2] == "expired"

    new_token, _ = await fx.invite_token(car)
    assert new_token != token
    assert (await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT,
                                            telegram_user_id=_CLIENT)).text == texts.CLIENT_LINK_INVALID_TEXT
    reopened = await fx.controller.handle_text(f"/start trlink_{new_token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert reopened.client_link_confirm_id is not None


async def test_confirm_after_expiry_is_refused(fx):
    car, _ = await fx.manager_car()
    token, _ = await fx.invite_token(car)
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    fx.now = _T0 + timedelta(hours=72, seconds=1)
    assert fx.answer(opened.client_link_confirm_id).text == texts.CLIENT_LINK_EXPIRED_TEXT
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None


async def test_unrelated_start_payload_is_a_plain_start(fx):
    reply = await fx.controller.handle_text("/start promo", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert reply.text == texts.WELCOME_TEXT


# ---- @username и выбор из пользователей бота ----

async def test_username_request_goes_to_the_client_by_user_id(fx):
    car, _ = await fx.manager_car()
    prompt = fx.action("username", car)
    assert prompt.show_cancel_button
    sent = await fx.controller.handle_text("@client_one", chat_id=_MANAGER, telegram_user_id=_MANAGER)
    request = sent.outgoing_messages[0]
    assert request.chat_id == _CLIENT and request.confirm_link_id is not None
    assert "500" not in request.text
    # Клиент сменил username — привязка по user_id всё равно его.
    fx.known.record_seen(telegram_user_id=_CLIENT, telegram_chat_id=_CLIENT, telegram_username="renamed")
    assert fx.answer(request.confirm_link_id).car_card is not None
    assert fx.links.active_for_anchor(_MANAGER, _PLATE).client_telegram_user_id == _CLIENT


async def test_unknown_username_suggests_an_invite(fx):
    car, _ = await fx.manager_car()
    fx.action("username", car)
    reply = await fx.controller.handle_text("@nobody_here", chat_id=_MANAGER, telegram_user_id=_MANAGER)
    assert "ещё не писал этому боту" in reply.text and reply.outgoing_messages == ()
    assert reply.client_link_screen[3] is True  # способы привязки снова на экране


async def test_pick_list_excludes_the_owner_and_requests_the_chosen_user(fx):
    car, _ = await fx.manager_car()
    pick = fx.action("pick", car, 0)
    user_ids = [user_id for user_id, _label in pick.client_link_pick[2]]
    assert _MANAGER not in user_ids and {_CLIENT, _OTHER, _STRANGER} <= set(user_ids)
    sent = fx.action("pick_user", car, _OTHER)
    assert sent.outgoing_messages[0].chat_id == _OTHER
    assert fx.action("pick_user", car, 123456) is None  # не пользователь бота


async def test_decline_notifies_the_manager_and_adds_nothing(fx):
    car, _ = await fx.manager_car()
    sent = fx.action("pick_user", car, _CLIENT)
    declined = fx.answer(sent.outgoing_messages[0].confirm_link_id, confirm=False)
    assert declined.text == texts.CLIENT_LINK_DECLINED_TEXT
    assert declined.outgoing_messages[0].chat_id == _MANAGER
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None
    assert fx.links.active_for_anchor(_MANAGER, _PLATE) is None


async def test_double_confirm_creates_nothing_twice(fx):
    car, _ = await fx.manager_car()
    sent = fx.action("pick_user", car, _CLIENT)
    link_id = sent.outgoing_messages[0].confirm_link_id
    first = fx.answer(link_id)
    second = fx.answer(link_id)
    assert first.outgoing_messages and second.outgoing_messages == ()
    assert len(fx.garage.list_cars(_CLIENT)) == 1
    assert len(_db_rows(fx, "SELECT id FROM turkey_client_links WHERE status = 'active'")) == 1


# ---- Управление: просмотр, замена, отвязка ----

async def test_manager_detail_shows_the_linked_client(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    detail = fx.controller.handle_manager_car_open(car.id, 0, telegram_user_id=_MANAGER)
    assert "🔗 Telegram: @client_one · привязан 20.11.2026" in detail.text
    assert detail.manager_car_detail_car_id == car.id
    screen = fx.action("screen", car)
    assert screen.client_link_screen == (car.id, 0, "active", False, False)


async def test_replace_moves_the_link_and_keeps_monitoring(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car, _CLIENT)
    sent = fx.action("pick_user", car, _OTHER)
    confirmed = fx.answer(sent.outgoing_messages[0].confirm_link_id, user=_OTHER)
    assert {m.chat_id for m in confirmed.outgoing_messages} == {_MANAGER, _CLIENT}
    statuses = dict(_db_rows(fx, "SELECT client_telegram_user_id, status FROM turkey_client_links "
                                 "WHERE client_telegram_user_id IS NOT NULL"))
    assert statuses == {_CLIENT: STATUS_REPLACED, _OTHER: STATUS_ACTIVE}
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None  # строку создавала привязка
    assert fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).active


async def test_unlink_keeps_history_and_manager_monitoring(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    runs_before = fx.runs.count_total()
    prompt = fx.action("unlink", car)
    assert prompt.client_link_unlink_confirm == (car.id, 0)
    done = fx.action("unlink_yes", car)
    assert [m.chat_id for m in done.outgoing_messages] == [_CLIENT]
    assert fx.links.active_for_anchor(_MANAGER, _PLATE) is None
    assert _db_rows(fx, "SELECT status FROM turkey_client_links")[-1][0] == STATUS_REVOKED
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is None
    assert fx.runs.count_total() == runs_before
    assert fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).active


async def test_client_who_already_had_the_car_keeps_it_after_unlink(fx):
    car, _ = await fx.manager_car()
    await fx.controller.handle_text(_PLATE, chat_id=_CLIENT, telegram_user_id=_CLIENT)  # своя строка до привязки
    await fx.link_client(car)
    fx.action("unlink_yes", car)
    assert fx.garage.get_owned_car_by_number(_CLIENT, _PLATE) is not None


async def test_linking_never_changes_the_managers_monitoring(fx):
    car, _ = await fx.manager_car()
    fx.subscriptions.disable(telegram_user_id=_MANAGER, plate=_PLATE)  # осознанная остановка / общий ⛔
    confirmed = await fx.link_client(car)
    assert not fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).active
    assert fx.subscriptions.get(telegram_user_id=_CLIENT, plate=_PLATE) is None  # своей подписки клиенту не создаём
    assert confirmed.car_card_monitoring_active is False
    assert "Уведомления включатся вместе с мониторингом" in confirmed.text


# ---- Уведомления: одна проверка, оба получателя, без старых штрафов ----

async def test_client_gets_only_new_changes_from_the_same_check(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)

    # Плановая проверка: долг тот же (исторический) — никому ничего.
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(500), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.notifier.sent == [] and fx.check.calls == [_PLATE]

    # Следующая: сумма выросла — уведомление и менеджеру, и клиенту, проверка одна.
    fx.subscriptions.mark_checked(fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).id,
                                  checked_at=_T0, next_check_at=_T0)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.check.calls == [_PLATE, _PLATE]
    assert sorted(chat for chat, _ in fx.notifier.sent) == [_CLIENT, _MANAGER]

    # Повтор того же состояния — клиенту не дублируется.
    fx.notifier.sent.clear()
    fx.subscriptions.mark_checked(fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).id,
                                  checked_at=_T0, next_check_at=_T0)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=2)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert fx.notifier.sent == []


async def test_muted_removed_or_self_monitoring_client_gets_no_linked_copy(fx):
    car, _ = await fx.manager_car()
    confirmed = await fx.link_client(car)
    client_car = confirmed.car_card

    # ⏹ у клиента — только его уведомления, мониторинг менеджера остаётся.
    card = await fx.controller.handle_car_action("monitor_off", client_car.id, chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert card.car_card_monitoring_active is False
    assert fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).active
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(900), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_MANAGER]

    # ▶️ у клиента — снова через менеджера, своей подписки (второй проверки) нет.
    card = await fx.controller.handle_car_action("monitor_on", client_car.id, chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert card.car_card_monitoring_active is True and texts.CLIENT_LINK_CARD_LINE in card.text
    assert fx.subscriptions.get(telegram_user_id=_CLIENT, plate=_PLATE) is None

    # Клиент удалил машину — привязка закрыта, уведомлений нет.
    await fx.controller.handle_car_action("delete_confirm", client_car.id, chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert fx.links.active_for_client(_CLIENT, _PLATE) is None
    fx.notifier.sent.clear()
    fx.subscriptions.mark_checked(fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).id,
                                  checked_at=_T0, next_check_at=_T0)
    fx.check.results.append(_result(ProviderStatus.NO_DEBT, at=_T0 + timedelta(days=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_MANAGER]


async def test_client_with_own_subscription_is_not_notified_twice(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)
    fx.subscriptions.enable(telegram_user_id=_CLIENT, telegram_chat_id=_CLIENT, plate=_PLATE,
                            next_check_at=_T0 + timedelta(days=30))  # своя подписка, в этом батче не к проверке
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_MANAGER]


async def test_failed_client_delivery_keeps_the_change_for_next_time(fx):
    car, _ = await fx.manager_car()
    await fx.link_client(car)

    class _FlakyNotifier(_Notifier):
        async def send(self, *, telegram_chat_id, text):
            if telegram_chat_id == _CLIENT:
                raise RuntimeError("blocked")
            await super().send(telegram_chat_id=telegram_chat_id, text=text)

    fx.monitoring._notifier = _FlakyNotifier()
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(hours=1)))
    await fx.monitoring.run_scheduled_batch("21:00")  # не падает
    fx.monitoring._notifier = fx.notifier
    fx.subscriptions.mark_checked(fx.subscriptions.get(telegram_user_id=_MANAGER, plate=_PLATE).id,
                                  checked_at=_T0, next_check_at=_T0)
    fx.check.results.append(_result(ProviderStatus.HAS_DEBT, Decimal(800), at=_T0 + timedelta(days=1)))
    await fx.monitoring.run_scheduled_batch("21:00")
    assert [chat for chat, _ in fx.notifier.sent] == [_CLIENT]  # менеджеру уже было, клиенту — сейчас


def test_schema_is_additive_and_reopen_is_safe(tmp_path):
    first = TurkeyClientLinkRepository(tmp_path / "users.db")
    first.close_db()
    second = TurkeyClientLinkRepository(tmp_path / "users.db")
    second.close_db()


def test_one_active_link_per_car_is_enforced_by_the_database(tmp_path):
    repo = TurkeyClientLinkRepository(tmp_path / "users.db")
    try:
        repo._conn.execute(
            "INSERT INTO turkey_client_links (owner_telegram_user_id, plate, status, method, created_by, "
            "created_at, expires_at) VALUES (1, 'P', 'active', 'invite', 1, 'x', 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute(
                "INSERT INTO turkey_client_links (owner_telegram_user_id, plate, status, method, created_by, "
                "created_at, expires_at) VALUES (1, 'P', 'active', 'invite', 1, 'x', 'x')")
    finally:
        repo.close_db()


def test_invite_payload_fits_telegram_limits():
    token_payload = "trlink_" + "A" * 22
    assert len(token_payload) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", token_payload)


async def test_concurrent_confirms_link_once(fx):
    car, _ = await fx.manager_car()
    sent = fx.action("pick_user", car, _CLIENT)
    link_id = sent.outgoing_messages[0].confirm_link_id

    async def confirm():
        return fx.answer(link_id)

    results = await asyncio.gather(confirm(), confirm())
    assert sum(bool(r.outgoing_messages) for r in results) == 1
    assert len(fx.garage.list_cars(_CLIENT)) == 1


# ---- Защиты на уровне репозитория (второй слой, на случай гонки) ----

def _pending(repo, *, invited=None, token="tok-1", now=_T0):
    return repo.create_pending(
        owner_telegram_user_id=_MANAGER, plate=_PLATE, method="invite", created_by=_MANAGER,
        invited_user_id=invited, token=token, now=now,
    )


def test_activate_is_single_use_and_erases_the_token(tmp_path):
    repo = TurkeyClientLinkRepository(tmp_path / "users.db")
    try:
        link = _pending(repo, invited=_CLIENT)
        assert repo.activate(link.id, client_telegram_user_id=_CLIENT, now=_T0, baseline={}, client_car_created=True)
        assert repo.get_by_token("tok-1") is None
        assert repo.activate(link.id, client_telegram_user_id=_CLIENT, now=_T0, baseline={},
                             client_car_created=True) is None
    finally:
        repo.close_db()


def test_activate_refuses_an_expired_or_foreign_invite(tmp_path):
    repo = TurkeyClientLinkRepository(tmp_path / "users.db")
    try:
        link = _pending(repo, invited=_CLIENT)
        assert repo.activate(link.id, client_telegram_user_id=_OTHER, now=_T0, baseline={},
                             client_car_created=True) is None
        late = _T0 + timedelta(hours=72)
        assert repo.activate(link.id, client_telegram_user_id=_CLIENT, now=late, baseline={},
                             client_car_created=True) is None
        assert repo.get(link.id).status == "pending"
    finally:
        repo.close_db()


def test_bind_invite_cannot_be_taken_over(tmp_path):
    repo = TurkeyClientLinkRepository(tmp_path / "users.db")
    try:
        link = _pending(repo)
        assert repo.bind_invite(link.id, _CLIENT) is True
        assert repo.bind_invite(link.id, _OTHER) is False
        assert repo.get(link.id).invited_user_id == _CLIENT
    finally:
        repo.close_db()


async def test_open_invite_refuses_a_link_that_is_no_longer_pending_even_with_a_token(fx):
    car, _ = await fx.manager_car()
    token, _ = await fx.invite_token(car)
    link = fx.links.get_by_token(token)
    fx.links._conn.execute("UPDATE turkey_client_links SET status = 'declined' WHERE id = ?", (link.id,))
    fx.links._conn.commit()
    opened = await fx.controller.handle_text(f"/start trlink_{token}", chat_id=_CLIENT, telegram_user_id=_CLIENT)
    assert opened.text == texts.CLIENT_LINK_INVALID_TEXT
