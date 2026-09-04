"""
Тесты ClientDeliveryService — доставка обнаруженных штрафов клиентам
@GEShtrafbot (owner/trusted_operator), см. design report Stage 4.
Repository — настоящие (SQLite, tmp_path), отправитель — фейковый (без
реального Telegram). now передаётся явно в run_once(), чтобы точно
проверить bounded backoff без реального времени/sleep.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon.errors import FloodWaitError  # noqa: E402

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository  # noqa: E402
from reader.public_bot.delivery_service import (  # noqa: E402
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF,
    ClientDeliveryService,
)
from reader.public_bot.delivery_texts import CTA_TEXT_BLOCK  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_CTA_CONTACT_USERNAME = "tplgee"

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")
# ВАЖНО: ClientFineDeliveryRepository.record_attempt() пишет last_attempt_at
# через SQL CURRENT_TIMESTAMP (реальное время выполнения, тот же приём, что
# и везде в проекте, см. reader/fines/task_repository.py) — run_once(now=...)
# не подменяет ЭТО поле, только то, с чем оно потом сравнивается
# (_is_due_for_attempt). Поэтому "сейчас" в этих тестах обязано быть
# основано на РЕАЛЬНОМ текущем времени, а не на произвольной дате в прошлом —
# иначе last_attempt_at (всегда реальное) окажется ПОЗЖЕ фиктивного now,
# и backoff-проверка "уже пора повторить" будет ложно-отрицательной.
#
# Тот же довод требует вычислять его ЗАНОВО в начале КАЖДОГО теста (а не
# один раз при импорте модуля): при прогоне полного набора тестов между
# импортом этого файла и выполнением конкретного теста может пройти много
# реального времени, и фиксированная на момент импорта константа тоже
# оказалась бы в прошлом относительно последующих record_attempt().
def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeSender:
    def __init__(self, *, fail_for=(), flood_wait_for=()):
        self.sent: list[tuple[int, str]] = []
        # Параллельно self.sent — (chat_id, text, buttons) для тестов,
        # которым нужно проверить именно CTA-кнопки (см. ниже), без
        # переписывания всех существующих 2-tuple-присваиваний выше.
        self.sent_full: list[tuple[int, str, list | None]] = []
        self._fail_for = set(fail_for)
        self._flood_wait_for = set(flood_wait_for)

    async def send_message(self, chat_id: int, text: str, *, buttons: list | None = None) -> None:
        if chat_id in self._flood_wait_for:
            raise FloodWaitError(request=None, capture=30)
        if chat_id in self._fail_for:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, text))
        self.sent_full.append((chat_id, text, buttons))


class _Fixture:
    def __init__(self, tmp_path, *, fail_for=(), flood_wait_for=()):
        db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(db_path)
        self.detected_fine_repository = DetectedFineRepository(db_path)
        self.subscription_repository = FineSubscriptionRepository(db_path)
        self.delivery_repository = ClientFineDeliveryRepository(db_path)
        self.sender = _FakeSender(fail_for=fail_for, flood_wait_for=flood_wait_for)
        self.service = ClientDeliveryService(
            self.detected_fine_repository, self.subscription_repository,
            self.delivery_repository, self.sender, tz=_TBILISI,
            payment_help_contact_username=_CTA_CONTACT_USERNAME,
        )

    def make_task(self, car_number, *, scope="client_bot") -> int:
        task = self.task_repository.create(
            car_number=car_number, label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope=scope,
        )
        return task.id

    def make_fine(self, task_id, car_number, *, fingerprint="fp-1") -> int:
        fine = self.detected_fine_repository.create(
            monitoring_task_id=task_id, car_number=car_number,
            external_fine_id="AB1", fingerprint=fingerprint,
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data="{}",
        )
        return fine.id

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.delivery_repository.close()


# ---- базовая доставка ----


async def test_delivers_to_owner_for_normal_subscription(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        result = await fx.service.run_once(now=now)

        assert result.delivered == 1
        assert result.failed == 0
        assert fx.sender.sent == [(777, fx.sender.sent[0][1])]
        assert "AA001AA" in fx.sender.sent[0][1]
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "owner") is True
    finally:
        fx.close()


async def test_delivered_fine_is_never_resent(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        first = await fx.service.run_once(now=now)
        second = await fx.service.run_once(now=now + timedelta(hours=5))

        assert first.delivered == 1
        assert second.delivered == 0
        assert len(fx.sender.sent) == 1  # не отправлено повторно
    finally:
        fx.close()


async def test_expired_subscription_is_excluded_from_delivery(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),  # уже истекла
        )

        result = await fx.service.run_once(now=now)

        assert result.delivered == 0
        assert fx.sender.sent == []
    finally:
        fx.close()


# ---- delegated: owner + trusted_operator ----


async def test_delegated_active_subscription_delivers_to_both_recipients(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
        )

        result = await fx.service.run_once(now=now)

        assert result.delivered == 2
        sent_chat_ids = {chat_id for chat_id, _ in fx.sender.sent}
        assert sent_chat_ids == {777, 555}
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "owner") is True
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "trusted_operator") is True
    finally:
        fx.close()


async def test_pending_claim_delivers_only_to_trusted_operator(tmp_path):
    """См. design report Stage 4: "trusted creator при pending claim
    продолжает получать уведомления" — owner ещё не claimed, доставка ему
    невозможна (telegram_chat_id пуст) и не должна даже пытаться."""
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        pending = fx.subscription_repository.create_pending_claim(
            monitoring_task_id=task_id, car_number="AA001AA",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=now + timedelta(days=7),
        )

        result = await fx.service.run_once(now=now)

        assert result.delivered == 1
        assert fx.sender.sent == [(555, fx.sender.sent[0][1])]
        assert fx.delivery_repository.is_delivered(fine_id, pending.id, "trusted_operator") is True
        assert fx.delivery_repository.get(fine_id, pending.id, "owner") is None  # даже не пытались
    finally:
        fx.close()


async def test_after_claim_owner_starts_receiving_delivery_too(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        pending = fx.subscription_repository.create_pending_claim(
            monitoring_task_id=task_id, car_number="AA001AA",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=now + timedelta(days=7),
        )
        await fx.service.run_once(now=now)  # только trusted_operator

        fx.subscription_repository.claim(
            "tok-1", telegram_user_id=777, telegram_chat_id=777, telegram_username="real_owner",
            now=now,
        )
        result = await fx.service.run_once(now=now + timedelta(minutes=1))

        assert result.delivered == 1  # теперь и owner
        assert fx.delivery_repository.is_delivered(fine_id, pending.id, "owner") is True
    finally:
        fx.close()


# ---- изоляция получателей: провал одного не блокирует другого ----


async def test_one_recipient_failure_does_not_block_the_other(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path, fail_for=[777])
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
        )

        result = await fx.service.run_once(now=now)

        assert result.delivered == 1  # trusted_operator
        assert result.failed == 1  # owner
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "owner") is False
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "trusted_operator") is True
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == 1
    finally:
        fx.close()


# ---- bounded backoff: initial, +1m, +5m, +15m, +1h, +3h, terminal ----


async def test_failed_delivery_is_not_retried_before_backoff_delay(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path, fail_for=[777])
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await fx.service.run_once(now=now)  # attempt 1, fails
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == 1

        # Слишком рано — второй попытки быть не должно (backoff[1] = 1 минута).
        too_soon = now + timedelta(seconds=30)
        await fx.service.run_once(now=too_soon)
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == 1

        # После задержки — повторная попытка (снова падает, attempt_count растёт).
        after_delay = now + RETRY_BACKOFF[1] + timedelta(seconds=1)
        await fx.service.run_once(now=after_delay)
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == 2
    finally:
        fx.close()


async def test_retry_eventually_succeeds_after_backoff(tmp_path):
    """Тот же провал, что и выше, но со второй попытки отправка проходит —
    подтверждает, что backoff не блокирует ВОССТАНОВЛЕНИЕ после временного
    сбоя, только его темп."""
    now = _now()
    fx = _Fixture(tmp_path, fail_for=[777])
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await fx.service.run_once(now=now)
        fx.sender._fail_for.discard(777)  # получатель "исправился"

        after_delay = now + RETRY_BACKOFF[1] + timedelta(seconds=1)
        result = await fx.service.run_once(now=after_delay)

        assert result.delivered == 1
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "owner") is True
    finally:
        fx.close()


async def test_terminal_give_up_after_max_attempts(tmp_path):
    """После исчерпания всего расписания backoff — доставка больше НИКОГДА
    не ретраится (нет бесконечного spam/retry loop, см. явное требование
    задачи)."""
    fx = _Fixture(tmp_path, fail_for=[777])
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2027, 12, 1),
        )

        now = _now()
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            await fx.service.run_once(now=now)
            now = now + timedelta(hours=4)  # заведомо дольше самой длинной паузы (3 часа)

        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == MAX_DELIVERY_ATTEMPTS

        # Ещё одна попытка спустя произвольно долгое время — БЕЗ эффекта.
        await fx.service.run_once(now=now + timedelta(days=30))
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == MAX_DELIVERY_ATTEMPTS
    finally:
        fx.close()


# ---- FloodWaitError: корректная обработка, без обхода лимита ----


async def test_flood_wait_stops_the_entire_tick(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path, flood_wait_for=[777])
    try:
        task_a = fx.make_task("AA001AA")
        fine_a = fx.make_fine(task_a, "AA001AA")
        sub_a = fx.subscription_repository.create(
            monitoring_task_id=task_a, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner_a",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        task_b = fx.make_task("BB002BB")
        fine_b = fx.make_fine(task_b, "BB002BB")
        sub_b = fx.subscription_repository.create(
            monitoring_task_id=task_b, car_number="BB002BB",
            telegram_user_id=888, telegram_chat_id=888, telegram_username="owner_b",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        assert sub_a.id < sub_b.id  # порядок обхода — id ASC (list_all_deliverable)

        result = await fx.service.run_once(now=now)

        assert result.flood_wait_hit is True
        # Получатель, идущий ПОСЛЕ того, кто вызвал FloodWaitError, вообще
        # не был тронут в этом тике — не пытаемся "обойти" лимит Telegram
        # продолжая слать остальным.
        assert fx.delivery_repository.get(fine_b, sub_b.id, "owner") is None
        assert fx.sender.sent == []
        # Попытка получателю A всё же зафиксирована (attempt_count вырос) —
        # обычный backoff учтёт её на следующем тике.
        assert fx.delivery_repository.get(fine_a, sub_a.id, "owner").attempt_count == 1
    finally:
        fx.close()


async def test_flood_wait_recipient_is_retried_on_next_tick_respecting_backoff(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path, flood_wait_for=[777])
    try:
        task_id = fx.make_task("AA001AA")
        fine_id = fx.make_fine(task_id, "AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await fx.service.run_once(now=now)
        assert fx.delivery_repository.get(fine_id, sub.id, "owner").attempt_count == 1

        # "исправляем" получателя и ждём обычный backoff — не пытаемся
        # обойти лимит Telegram собственным укороченным расписанием.
        fx.sender._flood_wait_for.discard(777)
        after_delay = now + RETRY_BACKOFF[1] + timedelta(seconds=1)
        result = await fx.service.run_once(now=after_delay)

        assert result.delivered == 1
        assert fx.delivery_repository.is_delivered(fine_id, sub.id, "owner") is True
    finally:
        fx.close()


# ---- коммерческий CTA-блок: ТОЛЬКО owner, destination из config ----


async def test_owner_notification_includes_cta_block(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await fx.service.run_once(now=now)

        assert len(fx.sender.sent_full) == 1
        _, text, buttons = fx.sender.sent_full[0]
        assert CTA_TEXT_BLOCK in text
        assert buttons is not None
    finally:
        fx.close()


async def test_owner_cta_buttons_both_point_to_configured_contact(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await fx.service.run_once(now=now)

        _, _, buttons = fx.sender.sent_full[0]
        # Один ряд, обе кнопки рядом — утверждённый макет
        # ([💳 Оплатить в рублях] [🛡 Оформить страховку]).
        assert len(buttons) == 1
        assert len(buttons[0]) == 2
        expected_url = f"https://t.me/{_CTA_CONTACT_USERNAME}"
        for button in buttons[0]:
            assert button.url == expected_url
        labels = [button.text for button in buttons[0]]
        assert labels == ["💳 Оплатить в рублях", "🛡 Оформить страховку"]
    finally:
        fx.close()


async def test_owner_cta_destination_follows_config_not_hardcoded(tmp_path):
    """Destination — параметр конструктора (из settings.public_bot.
    payment_help_contact_username в реальном wiring, см.
    reader/public_bot/main.py), а не константа в коде: другой username в
    config должен дать другую ссылку без изменения Python."""
    now = _now()
    db_path = tmp_path / "users.db"
    task_repository = FineMonitoringTaskRepository(db_path)
    detected_fine_repository = DetectedFineRepository(db_path)
    subscription_repository = FineSubscriptionRepository(db_path)
    delivery_repository = ClientFineDeliveryRepository(db_path)
    sender = _FakeSender()
    service = ClientDeliveryService(
        detected_fine_repository, subscription_repository, delivery_repository,
        sender, tz=_TBILISI, payment_help_contact_username="another_contact",
    )
    try:
        task = task_repository.create(
            car_number="AA001AA", label=None, start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID, monitoring_scope="client_bot",
        )
        detected_fine_repository.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            external_fine_id="AB1", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status="Не вручено", raw_data="{}",
        )
        subscription_repository.create(
            monitoring_task_id=task.id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )

        await service.run_once(now=now)

        _, _, buttons = sender.sent_full[0]
        flat = [button for row in buttons for button in row]
        assert all(button.url == "https://t.me/another_contact" for button in flat)
    finally:
        task_repository.close()
        detected_fine_repository.close()
        subscription_repository.close()
        delivery_repository.close()


async def test_trusted_operator_notification_has_no_cta_block(tmp_path):
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create_pending_claim(
            monitoring_task_id=task_id, car_number="AA001AA",
            owner_username_hint="unknown_person",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            claim_token="tok-1", claim_token_expires_at=now + timedelta(days=7),
        )

        await fx.service.run_once(now=now)

        assert len(fx.sender.sent_full) == 1
        _, text, buttons = fx.sender.sent_full[0]
        assert CTA_TEXT_BLOCK not in text
        assert buttons is None
    finally:
        fx.close()


async def test_delegated_active_subscription_cta_only_on_owner_recipient(tmp_path):
    """owner И trusted_operator получают уведомление об одном и том же
    штрафе (delegated, status='active') — CTA должен быть строго у одного
    из двух, независимо от порядка/группировки доставки."""
    now = _now()
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        fx.make_fine(task_id, "AA001AA")
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
            owner_username_hint="owner",
            created_by_telegram_user_id=555, created_by_telegram_chat_id=555,
        )

        await fx.service.run_once(now=now)

        assert len(fx.sender.sent_full) == 2
        by_chat_id = {chat_id: (text, buttons) for chat_id, text, buttons in fx.sender.sent_full}
        owner_text, owner_buttons = by_chat_id[777]
        trusted_text, trusted_buttons = by_chat_id[555]
        assert CTA_TEXT_BLOCK in owner_text
        assert owner_buttons is not None
        assert CTA_TEXT_BLOCK not in trusted_text
        assert trusted_buttons is None
    finally:
        fx.close()


# ---- гигиена: expire_elapsed вызывается каждый тик ----


async def test_run_once_expires_elapsed_subscriptions(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task("AA001AA")
        sub = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="AA001AA",
            telegram_user_id=777, telegram_chat_id=777, telegram_username="owner",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert fx.subscription_repository.get(sub.id).status == "active"  # ещё не помечена

        await fx.service.run_once(now=_now())

        assert fx.subscription_repository.get(sub.id).status == "expired"
    finally:
        fx.close()
