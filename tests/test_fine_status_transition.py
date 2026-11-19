"""Переход со старой логики (снимок суммы задачи + detected_fines) на
статусы оплаты — регрессия на данных той же формы, что и в production
(синтетические номера и квитанции; сами данные production в публичный
репозиторий не попадают, на них тот же прогон делается read-only скриптом).

Что проверяется:
- первичное заполнение: квитанции из последнего ответа police.ge — 🔴,
  отсутствовавшие в старом ответе — ⏳ 'unverified', ни одной ⚪;
- старые таблицы (detected_fines, задачи, доставки) не меняются;
- в статистике ни одна машина с долгом по старой логике не пропадает и не
  теряет сумму; история не превращается в подтверждённый долг — в том
  числе строки старых задач без записанной проверки, строки, записанные
  за секунды до снимка, и номер, где более новая нулевая проверка другой
  задачи спорит со старым долгом;
- клиентская доставка после перехода не отправляет ничего, чего не
  отправила бы старая логика, а ⏳ новому подписчику уходит, только когда
  новая проверка её подтвердит;
- ⚪ — только после двух НОВЫХ успешных полных проверок с интервалом;
  ошибки, неподтверждённый пустой ответ, неполный ответ и кэш ничего не
  скрывают."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.fines.check_service import FineCheckService
from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.fines.models import UNLIMITED_END_DATE, ParsedFineRecord
from reader.fines.payment_status import (
    PRESENCE_GONE,
    PRESENCE_LISTED,
    PRESENCE_MISSING,
    PRESENCE_UNVERIFIED,
    FineStatusRepository,
    FineStatusTracker,
)
from reader.fines.provider import FineProvider, FineProviderError, ProviderSearchResult
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.conversation import ConversationController
from reader.public_bot.conversation_state_repository import BotConversationStateRepository
from reader.public_bot.debt_refresh_service import DebtRefreshService
from reader.public_bot.delivery_repository import ClientFineDeliveryRepository
from reader.public_bot.delivery_service import ClientDeliveryService
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_repository import FineSubscriptionRepository
from reader.public_bot.subscription_service import SubscriptionService
from reader.users.repository import UserRepository

_TBILISI = ZoneInfo("Asia/Tbilisi")
_TRUSTED = 900000001


async def _instant(_seconds):
    pass


def _record(car, protocol_no, amount=50.0):
    return ParsedFineRecord(
        car_number=car, external_fine_id=protocol_no, penalty_date=None, due_date=None,
        delivered_status="Не вручено", fingerprint=f"fp-{car}-{protocol_no}", raw_data={}, amount=amount,
    )


class _Provider(FineProvider):
    def __init__(self):
        self.responses: dict[str, list] = {}

    async def search_by_plate(self, plate):
        return (await self.search_by_plate_detailed(plate)).records

    async def search_by_plate_detailed(self, plate, *, refresh_page=False):
        queue = self.responses.get(plate) or [ProviderSearchResult(records=[])]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


class _Sender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, *, buttons=None):
        self.sent.append((chat_id, text))


class _LegacyDb:
    """База "как в production до перехода": задачи разных статусов,
    снимки сумм, detected_fines (текущие, исторические, найденные feed'ом
    после снимка, без номера квитанции), подписки и уже доставленные
    сообщения."""

    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.tasks = FineMonitoringTaskRepository(self.db_path)
        self.fines = DetectedFineRepository(self.db_path)
        self.subscriptions = FineSubscriptionRepository(self.db_path)
        self.deliveries = ClientFineDeliveryRepository(self.db_path)

    def task(self, car, *, status="active", snapshot=None, checked_at=None):
        task = self.tasks.create(
            car_number=car, label=None, start_date=date(2026, 9, 1), end_date=UNLIMITED_END_DATE,
            telegram_chat_id=-1, created_by_user_id=1,
        )
        if snapshot is not None:
            self.tasks.record_successful_check(task.id, total_amount=snapshot)
            self._sql("UPDATE fine_monitoring_tasks SET last_successful_checked_at = ? WHERE id = ?",
                      checked_at or "2026-11-16 10:00:00", task.id)
        if status != "active":
            self._sql("UPDATE fine_monitoring_tasks SET status = ? WHERE id = ?", status, task.id)
        return task

    def fine(self, task, protocol, amount, *, last_seen, notified=True):
        fine = self.fines.create(
            monitoring_task_id=task.id, car_number=task.car_number, external_fine_id=protocol,
            fingerprint=f"fp-{task.car_number}-{protocol}", penalty_date=None, due_date=None,
            delivered_status=None, raw_data="{}", amount=amount,
        )
        self._sql("UPDATE detected_fines SET last_seen_at = ?, first_detected_at = ? WHERE id = ?",
                  last_seen, last_seen, fine.id)
        if notified:
            self.fines.mark_notification_sent(fine.id)
        return fine

    def subscribe(self, task, user_id, *, delivered=()):
        subscription = self.subscriptions.create(
            monitoring_task_id=task.id, car_number=task.car_number, telegram_user_id=user_id,
            telegram_chat_id=user_id, telegram_username=None, start_date=date(2026, 9, 1),
            end_date=UNLIMITED_END_DATE,
        )
        for fine in delivered:
            self.deliveries.record_attempt(fine.id, subscription.id, "owner")
            self.deliveries.mark_delivered(fine.id, subscription.id, "owner")
        return subscription

    def _sql(self, query, *params):
        self.tasks._conn.execute(query, params)
        self.tasks._conn.commit()

    def dump(self) -> dict:
        """Содержимое старых таблиц — чтобы убедиться, что переход их не трогает."""
        conn = self.tasks._conn
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("fine_monitoring_tasks", "detected_fines", "client_fine_deliveries",
                          "fine_monitoring_subscriptions")
        }

    def close(self):
        for repository in (self.deliveries, self.subscriptions, self.fines, self.tasks):
            repository.close()


@pytest.fixture()
def legacy(tmp_path):
    db = _LegacyDb(tmp_path)
    # 1) активная машина с долгом: две квитанции в последнем ответе
    db.debt_task = db.task("AA111BB", snapshot=150)
    db.debt_fines = [db.fine(db.debt_task, "GE1", 50, last_seen="2026-11-16 10:00:00"),
                     db.fine(db.debt_task, "GE2", 100, last_seen="2026-11-16 10:00:00")]
    # 2) машина, у которой ВСЁ "исчезло" по старому снимку 0 (мог быть ложный ноль)
    db.zero_task = db.task("BB222CC", snapshot=0)
    db.zero_fines = [db.fine(db.zero_task, "GE3", 30, last_seen="2026-10-01 10:00:00"),
                     db.fine(db.zero_task, "GE4", 70, last_seen="2026-10-01 10:00:00")]
    # 3) частичное исчезновение: GE5 в ответе, GE6 — нет
    db.partial_task = db.task("CC333DD", snapshot=40)
    db.fine(db.partial_task, "GE5", 40, last_seen="2026-11-16 10:00:00")
    db.fine(db.partial_task, "GE6", 25, last_seen="2026-09-20 10:00:00")
    # 4) завершённая задача + активная по одному номеру, конфликт присутствия
    old_task = db.task("DD444EE", status="completed", snapshot=0, checked_at="2026-10-05 10:00:00")
    db.fine(old_task, "GE7", 60, last_seen="2026-10-01 10:00:00")
    new_task = db.task("DD444EE", snapshot=60, checked_at="2026-11-16 10:00:00")
    db.fine(new_task, "GE7", 60, last_seen="2026-11-16 10:00:00")
    # 5) найдено feed'ом после последнего снимка (снимок 0)
    feed_task = db.task("EE555FF", snapshot=0, checked_at="2026-11-01 10:00:00")
    db.fine(feed_task, "GE8", 90, last_seen="2026-11-10 10:00:00", notified=False)
    # 6) старая запись без номера квитанции
    legacy_task = db.task("FF666GG", snapshot=0)
    db.fines.create(monitoring_task_id=legacy_task.id, car_number="FF666GG", external_fine_id=None,
                    fingerprint="fp-legacy", penalty_date=None, due_date=None, delivered_status=None,
                    raw_data="{}", amount=10)
    # подписки: клиенты уже получили свои штрафы
    db.subscribe(db.debt_task, 7001, delivered=db.debt_fines)
    db.subscribe(db.zero_task, 7002, delivered=db.zero_fines)
    yield db
    db.close()


def _statuses(tracker):
    return {
        (s.car_number, s.protocol_no): s
        for car in ("AA111BB", "BB222CC", "CC333DD", "DD444EE", "EE555FF", "FF666GG")
        for s in tracker.statuses_for_car(car)
    }


def test_first_start_never_marks_anything_gone_and_leaves_old_tables_untouched(legacy):
    before = legacy.dump()
    repository = FineStatusRepository(legacy.db_path)
    tracker = FineStatusTracker(repository)

    created = tracker.seed_from_detected_fines()

    statuses = _statuses(tracker)
    assert created == len(statuses) == 8
    assert not [s for s in statuses.values() if s.presence == PRESENCE_GONE]
    listed = {key for key, s in statuses.items() if s.presence == PRESENCE_LISTED}
    unverified = {key for key, s in statuses.items() if s.presence == PRESENCE_UNVERIFIED}
    assert listed == {("AA111BB", "GE1"), ("AA111BB", "GE2"), ("CC333DD", "GE5"), ("DD444EE", "GE7"),
                      ("EE555FF", "GE8")}
    assert unverified == {("BB222CC", "GE3"), ("BB222CC", "GE4"), ("CC333DD", "GE6")}
    assert legacy.dump() == before
    assert tracker.seed_from_detected_fines() == 0
    repository.close()


async def test_statistics_keep_every_old_debt_and_show_history_separately(legacy):
    old_groups = {g.car_number: g.total_amount for g in legacy.tasks.list_debt_car_groups()}
    repository = FineStatusRepository(legacy.db_path)
    tracker = FineStatusTracker(repository)
    tracker.seed_from_detected_fines()
    controller = _controller(legacy, tracker)

    text = controller._format_statistics_reply().text
    rows, _ = controller._debt_rows_with_statuses(legacy.tasks.list_debt_car_groups())
    by_car = {car: breakdown for car, _, breakdown in rows}

    # каждая машина с долгом по старой логике — в списке, с той же суммой
    for car, total in old_groups.items():
        assert car in by_car, car
        assert by_car[car].unpaid_amount + by_car[car].needs_review_amount == total, (car, total)
        assert f"💰 Задолженность: {total:g} ₾" in text, (car, total)
    assert "🔴 Подтверждённая задолженность: 340 ₾ (5 шт.)" in text  # 150 + 40 + 60 + 90 (feed)
    # история — не долг и не ⚪, но видна отдельной строкой
    assert "⏳ Ждут повторной проверки police.ge: 125 ₾ (3 шт.)" in text
    assert "BB" + "222" not in text.replace("*", "")  # машина только с историей — без строки долга
    repository.close()


async def test_delivery_after_transition_sends_nothing_new(legacy):
    # новые подписки: на номер с одной квитанцией под двумя задачами и на номер с историей
    dd_task = [t for t in legacy.tasks.list_by_car_number("DD444EE") if t.status == "active"][0]
    legacy.subscribe(dd_task, 7003)
    legacy.subscribe(legacy.zero_task, 7004)

    def pending_old():
        """Что отправила бы старая логика: каждая недоставленная строка."""
        return sorted(
            (sub.telegram_chat_id, fine.external_fine_id)
            for sub in legacy.subscriptions.list_all_deliverable(today=date(2026, 11, 18))
            for fine in legacy.fines.list_by_car_number(sub.car_number)
            if not legacy.deliveries.is_delivered(fine.id, sub.id, "owner")
        )

    old_pending = pending_old()
    repository = FineStatusRepository(legacy.db_path)
    tracker = FineStatusTracker(repository)
    tracker.seed_from_detected_fines()
    sender = _Sender()
    new = ClientDeliveryService(legacy.fines, legacy.subscriptions, legacy.deliveries, sender, tz=_TBILISI,
                                status_lookup=tracker)

    await new.run_once()

    sent = sorted((chat, protocol) for chat, text in sender.sent for protocol in ("GE3", "GE4", "GE7")
                  if f"№: {protocol}" in text)
    # старая логика дважды прислала бы GE7 (две задачи номера); новая — один раз
    assert old_pending.count((7003, "GE7")) == 2
    assert sent.count((7003, "GE7")) == 1
    # ничего сверх старой логики, а история (⏳) новому подписчику "новым
    # штрафом" не уходит, пока новая проверка её не подтвердит
    assert set(sent) <= set(old_pending)
    assert (7004, "GE3") not in sent and (7004, "GE4") not in sent
    # клиенты, уже получившие свои штрафы, ничего нового не получают
    assert not [chat for chat, _ in sender.sent if chat in (7001, 7002)]

    # новая полная проверка нашла GE3 — она уходит; GE4 — нет (ещё ⏳)
    tracker.record_full_check("BB222CC", [_record("BB222CC", "GE3", 30)], complete=True)
    sender.sent.clear()
    await new.run_once()
    assert [(chat, protocol) for chat, text in sender.sent for protocol in ("GE3", "GE4")
            if f"№: {protocol}" in text] == [(7004, "GE3")]
    repository.close()


async def test_gone_only_after_two_new_complete_checks_with_gap(legacy):
    repository = FineStatusRepository(legacy.db_path)
    clock = {"now": datetime(2026, 11, 19, 19, 0)}
    tracker = FineStatusTracker(repository, disappearance_min_gap=timedelta(hours=12), clock=lambda: clock["now"])
    tracker.seed_from_detected_fines()
    provider = _Provider()
    service = FineCheckService(provider, legacy.tasks, legacy.fines, sleep=_instant, status_tracker=tracker,
                               dedupe_known_receipts=True)
    zero = legacy.tasks.get(legacy.zero_task.id)
    empty = ProviderSearchResult(records=[])

    # ошибка, неподтверждённый пустой ответ (повторный запрос нашёл штраф),
    # неполный ответ — ничего не меняют
    provider.responses["BB222CC"] = [FineProviderError("timeout")]
    await service.check_task(zero)
    provider.responses["BB222CC"] = [empty, ProviderSearchResult(records=[_record("BB222CC", "GE3", 30)])]
    await service.check_task(legacy.tasks.get(zero.id))
    statuses = _statuses(tracker)
    assert statuses[("BB222CC", "GE3")].presence == PRESENCE_LISTED  # нашёлся — снова долг
    assert statuses[("BB222CC", "GE4")].presence == PRESENCE_UNVERIFIED  # неполный ответ ничего не доказал
    provider.responses["BB222CC"] = [ProviderSearchResult(records=[], reported_count=1)]
    await service.check_task(legacy.tasks.get(zero.id))
    assert _statuses(tracker)[("BB222CC", "GE4")].presence == PRESENCE_UNVERIFIED

    # первая НОВАЯ подтверждённая проверка отсутствия — ещё не ⚪
    provider.responses["BB222CC"] = [empty, empty]
    await service.check_task(legacy.tasks.get(zero.id))
    statuses = _statuses(tracker)
    assert statuses[("BB222CC", "GE4")].presence == PRESENCE_UNVERIFIED
    assert statuses[("BB222CC", "GE3")].presence == PRESENCE_MISSING

    # вторая — раньше 12 часов: всё ещё нет
    clock["now"] += timedelta(hours=6)
    provider.responses["BB222CC"] = [empty, empty]
    await service.check_task(legacy.tasks.get(zero.id))
    assert _statuses(tracker)[("BB222CC", "GE4")].presence == PRESENCE_UNVERIFIED

    # через 12+ часов — ⚪
    clock["now"] += timedelta(hours=7)
    provider.responses["BB222CC"] = [empty, empty]
    await service.check_task(legacy.tasks.get(zero.id))
    statuses = _statuses(tracker)
    assert statuses[("BB222CC", "GE4")].presence == PRESENCE_GONE
    assert statuses[("BB222CC", "GE3")].presence == PRESENCE_GONE
    repository.close()


async def test_cached_read_during_outage_changes_nothing(legacy):
    repository = FineStatusRepository(legacy.db_path)
    tracker = FineStatusTracker(repository)
    tracker.seed_from_detected_fines()

    class _Down(_Provider):
        def is_available(self):
            return False

    service = FineCheckService(_Down(), legacy.tasks, legacy.fines, sleep=_instant, status_tracker=tracker)
    before = {key: (s.presence, s.payment_status) for key, s in _statuses(tracker).items()}

    result = await service.check_task_or_read_cached(legacy.tasks.get(legacy.debt_task.id))

    assert result.from_cache
    assert {key: (s.presence, s.payment_status) for key, s in _statuses(tracker).items()} == before
    repository.close()


def _seed_and_rows(db):
    """Первичное заполнение + строки статистики (машина -> разбивка)."""
    repository = FineStatusRepository(db.db_path)
    tracker = FineStatusTracker(repository)
    tracker.seed_from_detected_fines()
    rows, totals = _controller(db, tracker)._debt_rows_with_statuses(db.tasks.list_debt_car_groups())
    return repository, tracker, {car: breakdown for car, _, breakdown in rows}, totals


def test_receipt_of_a_task_without_recorded_check_is_history_when_a_newer_check_missed_it(tmp_path):
    """Старая задача без записанной успешной проверки (поле появилось
    позже) хранит квитанцию, которую более свежая проверка другой задачи
    номера уже не нашла: это история (⏳), а не подтверждённый долг."""
    db = _LegacyDb(tmp_path)
    old_task = db.task("GG777HH", status="completed")  # last_successful_checked_at пуст
    db.fine(old_task, "GE9", 80, last_seen="2025-03-01 10:00:00")
    db.task("GG777HH", snapshot=0, checked_at="2026-11-16 10:00:00")
    assert db.tasks.list_debt_car_groups() == []  # старая статистика долга его не показывала

    repository, tracker, rows, totals = _seed_and_rows(db)

    assert tracker.statuses_by_protocol("GG777HH")["GE9"].presence == PRESENCE_UNVERIFIED
    assert "GG777HH" not in rows
    assert (totals.unpaid_amount, totals.unverified_amount) == (0, 80)
    repository.close()
    db.close()


def test_old_debt_reference_wins_over_a_newer_zero_check_of_another_task(tmp_path):
    """Прежняя статистика брала самую свежую задачу с положительным
    снимком; более новая нулевая проверка другой задачи номера (возможно,
    ложный ноль) долг не прячет — квитанция остаётся 🔴 до новой проверки."""
    db = _LegacyDb(tmp_path)
    debt_task = db.task("HH888II", snapshot=100, checked_at="2026-10-01 10:00:00")
    db.fine(debt_task, "GE10", 100, last_seen="2026-10-01 10:00:00")
    db.task("HH888II", snapshot=0, checked_at="2026-11-16 10:00:00")
    assert {g.car_number: g.total_amount for g in db.tasks.list_debt_car_groups()} == {"HH888II": 100}

    repository, tracker, rows, _ = _seed_and_rows(db)

    assert tracker.statuses_by_protocol("HH888II")["GE10"].presence == PRESENCE_LISTED
    assert rows["HH888II"].unpaid_amount == 100
    repository.close()
    db.close()


def test_plate_without_any_recorded_check_is_history_only(tmp_path):
    db = _LegacyDb(tmp_path)
    task = db.task("II999JJ")  # ни одной записанной успешной проверки
    db.fine(task, "GE11", 20, last_seen="2026-08-01 10:00:00")
    db.fine(task, "GE12", 30, last_seen="2026-08-01 10:00:00")

    repository, tracker, rows, totals = _seed_and_rows(db)

    assert {s.presence for s in tracker.statuses_for_car("II999JJ")} == {PRESENCE_UNVERIFIED}
    assert "II999JJ" not in rows
    assert (totals.unpaid_amount, totals.unverified_amount) == (0, 50)
    repository.close()
    db.close()


@pytest.mark.parametrize(("last_seen", "presence"), [
    ("2026-11-16 09:59:40", PRESENCE_LISTED), ("2026-11-16 09:57:00", PRESENCE_UNVERIFIED),
])
def test_rows_written_before_the_snapshot_never_drop_the_car_from_debt(tmp_path, last_seen, presence):
    """Старые версии писали снимок ПОСЛЕ строк detected_fines. Строки за
    секунды до снимка — квитанции того же ответа (🔴); строки сильно
    раньше — ⏳, но тогда статусы старше снимка, и машина остаётся в списке
    с суммой снимка. Пропасть из списка или посчитаться дважды она не может."""
    db = _LegacyDb(tmp_path)
    task = db.task("JJ000KK", snapshot=100, checked_at="2026-11-16 10:00:00")
    db.fine(task, "GE13", 40, last_seen=last_seen)
    db.fine(task, "GE14", 60, last_seen=last_seen)

    repository, tracker, rows, totals = _seed_and_rows(db)

    assert {s.presence for s in tracker.statuses_for_car("JJ000KK")} == {presence}
    assert rows["JJ000KK"].unpaid_amount + rows["JJ000KK"].needs_review_amount == 100
    assert totals.unpaid_amount + totals.unverified_amount == 100
    repository.close()
    db.close()


def test_rows_on_both_sides_of_the_tolerance_keep_the_old_total(tmp_path):
    """Один старый ответ, строки которого записаны за 20 с и за 90 с до
    снимка: первая — 🔴, вторая — ⏳, а машина показывает прежнюю сумму
    снимка целиком (полной проверки со статусами ещё не было), без двойного
    счёта."""
    db = _LegacyDb(tmp_path)
    task = db.task("MM333NN", snapshot=100, checked_at="2026-11-16 10:00:00")
    db.fine(task, "GE20", 40, last_seen="2026-11-16 09:59:40")
    db.fine(task, "GE21", 60, last_seen="2026-11-16 09:58:30")

    repository, tracker, rows, totals = _seed_and_rows(db)

    statuses = tracker.statuses_by_protocol("MM333NN")
    assert (statuses["GE20"].presence, statuses["GE21"].presence) == (PRESENCE_LISTED, PRESENCE_UNVERIFIED)
    assert rows["MM333NN"].unpaid_amount == 100
    assert (totals.unpaid_amount, totals.unverified_amount) == (100, 0)
    repository.close()
    db.close()


def test_receipt_missed_by_a_newer_check_of_its_own_task_stays_history(tmp_path):
    """Старая задача с положительным снимком держит долг прежней статистики
    (GE17), а строка GE18 активной задачи старше её собственной более новой
    (нулевой) проверки — GE18 остаётся ⏳, сумма машины не меняется."""
    db = _LegacyDb(tmp_path)
    old_task = db.task("KK111LL", status="completed", snapshot=100, checked_at="2026-09-01 10:00:00")
    db.fine(old_task, "GE17", 100, last_seen="2026-09-01 10:00:00")
    active = db.task("KK111LL", snapshot=0, checked_at="2026-11-16 10:00:00")
    db.fine(active, "GE18", 70, last_seen="2026-10-01 10:00:00")
    assert {g.car_number: g.total_amount for g in db.tasks.list_debt_car_groups()} == {"KK111LL": 100}

    repository, tracker, rows, totals = _seed_and_rows(db)

    statuses = tracker.statuses_by_protocol("KK111LL")
    assert (statuses["GE17"].presence, statuses["GE18"].presence) == (PRESENCE_LISTED, PRESENCE_UNVERIFIED)
    assert rows["KK111LL"].unpaid_amount + rows["KK111LL"].needs_review_amount == 100
    assert totals.unverified_amount == 70
    repository.close()
    db.close()


def test_feed_find_missed_by_a_newer_check_does_not_raise_the_old_total(tmp_path):
    """Feed нашёл квитанцию (строки у активной задачи и у задачи без
    проверки), а более новая нулевая проверка её уже не показала: она ⏳, и
    прежний долг машины не растёт — ни через строку задачи без проверки, ни
    через строку самой опорной задачи."""
    db = _LegacyDb(tmp_path)
    reference = db.task("NN444OO", snapshot=100, checked_at="2026-09-01 10:00:00")
    db.fine(reference, "GE22", 100, last_seen="2026-09-01 10:00:00")
    db.fine(reference, "GE23", 40, last_seen="2026-11-10 10:00:00", notified=False)  # feed, после снимка
    newer = db.task("NN444OO", snapshot=0, checked_at="2026-11-16 10:00:00")
    db.fine(newer, "GE23", 40, last_seen="2026-11-10 10:00:00", notified=False)
    db.fine(newer, "GE24", 70, last_seen="2026-10-01 10:00:00")
    never_checked = db.task("NN444OO")
    db.fine(never_checked, "GE23", 40, last_seen="2026-11-10 10:00:00", notified=False)
    assert {g.car_number: g.total_amount for g in db.tasks.list_debt_car_groups()} == {"NN444OO": 100}

    repository, tracker, rows, totals = _seed_and_rows(db)

    statuses = tracker.statuses_by_protocol("NN444OO")
    assert statuses["GE22"].presence == PRESENCE_LISTED
    assert statuses["GE23"].presence == PRESENCE_UNVERIFIED
    assert statuses["GE24"].presence == PRESENCE_UNVERIFIED
    assert rows["NN444OO"].unpaid_amount + rows["NN444OO"].needs_review_amount == 100
    repository.close()
    db.close()


def test_feed_find_under_a_task_without_recorded_check_counts_after_the_plate_check(tmp_path):
    """У задачи нет записанной проверки, но у номера есть успешная проверка
    другой задачи: строка, увиденная после неё (её нашёл feed), — 🔴."""
    db = _LegacyDb(tmp_path)
    db.task("LL222MM", snapshot=0, checked_at="2026-11-01 10:00:00")
    legacy_task = db.task("LL222MM")  # без записанной проверки
    db.fine(legacy_task, "GE19", 90, last_seen="2026-11-10 10:00:00", notified=False)

    repository, tracker, rows, _ = _seed_and_rows(db)

    assert tracker.statuses_by_protocol("LL222MM")["GE19"].presence == PRESENCE_LISTED
    assert rows["LL222MM"].unpaid_amount == 90
    repository.close()
    db.close()


def _controller(legacy, tracker):
    users = UserRepository(legacy.db_path)
    states = BotConversationStateRepository(legacy.db_path)
    known = BotKnownUsersRepository(legacy.db_path)
    check_service = FineCheckService(_Provider(), legacy.tasks, legacy.fines, sleep=_instant, status_tracker=tracker)
    return ConversationController(
        states, SubscriptionService(legacy.tasks, legacy.subscriptions, users, check_service),
        BotStatisticsService(known, legacy.subscriptions, legacy.fines), known, tz=_TBILISI,
        trusted_operator_user_ids=frozenset({_TRUSTED}),
        debt_refresh_service=DebtRefreshService(legacy.tasks, check_service, sleep=_instant),
        fine_status_tracker=tracker,
    )



def test_seeding_makes_statuses_current_only_where_they_explain_the_snapshot(legacy):
    """Если квитанции номера, выросшие из снимка прежней статистики, дают
    его сумму, статусы актуальны на время снимка: отметка 🟢 сразу убирает
    квитанцию из долга. Не дают — страховка "снимок новее статусов"
    остаётся. Настоящее время полной проверки не перезаписывается."""
    short_task = legacy.task("GG777HH", snapshot=150)
    legacy.fine(short_task, "GE9", 50, last_seen="2026-11-16 10:00:00")  # снимок 150 объясняют только 50
    repository = FineStatusRepository(legacy.db_path)
    real_check = datetime(2026, 11, 18, 9, 0)
    repository.record_full_check_time("CC333DD", real_check)
    tracker = FineStatusTracker(repository)

    tracker.seed_from_detected_fines()
    tracker.seed_from_detected_fines()  # повторный старт ничего не меняет

    checks = tracker.last_full_check_by_car(["AA111BB", "DD444EE", "CC333DD", "GG777HH", "BB222CC"])
    assert checks["AA111BB"] == datetime(2026, 11, 16, 10, 0)
    assert checks["DD444EE"] == datetime(2026, 11, 16, 10, 0)
    assert checks["CC333DD"] == real_check
    assert "GG777HH" not in checks and "BB222CC" not in checks  # не объяснён / снимок 0

    tracker.mark_paid(tracker.statuses_by_protocol("AA111BB")["GE2"].id, actor_user_id=_TRUSTED, note="TBC 1")
    rows, _ = _controller(legacy, tracker)._debt_rows_with_statuses(legacy.tasks.list_debt_car_groups())
    by_car = {car: breakdown for car, _, breakdown in rows}

    assert by_car["AA111BB"].unpaid_amount == 50 and by_car["AA111BB"].paid_amount == 100
    assert by_car["GG777HH"].unpaid_amount == 150  # страховка: показан снимок
    repository.close()


def test_restart_does_not_take_a_newer_snapshot_from_a_check_without_statuses_as_explained(legacy):
    """Обзор f2dc7c6: при перезапуске устаревшая 🟢 'listed' не должна
    "объяснить" снимок более новой проверки без статусов, чьи квитанции в
    статусах ⏳ — иначе их долг пропал бы из «Задолженности»."""
    task = legacy.task("HH888JJ", snapshot=0, checked_at="2026-11-16 10:00:00")
    legacy.fine(task, "GE10", 100, last_seen="2026-11-17 10:00:00")  # найдена feed'ом после нулевой проверки
    old = legacy.fine(task, "GE11", 80, last_seen="2026-11-01 10:00:00")  # история — ⏳
    repository = FineStatusRepository(legacy.db_path)
    tracker = FineStatusTracker(repository)
    tracker.seed_from_detected_fines()
    statuses = tracker.statuses_by_protocol("HH888JJ")
    assert statuses["GE10"].presence == PRESENCE_LISTED and statuses["GE11"].presence == PRESENCE_UNVERIFIED
    tracker.mark_paid(statuses["GE10"].id, actor_user_id=_TRUSTED, note="TBC 1")
    # проверка без статусов (сбой записи статусов / операторский процесс) нашла GE11
    legacy._sql("UPDATE fine_monitoring_tasks SET last_successful_total_amount = 80, "
                "last_successful_checked_at = '2026-11-18 10:00:00' WHERE id = ?", task.id)
    legacy._sql("UPDATE detected_fines SET last_seen_at = '2026-11-18 10:00:00' WHERE id = ?", old.id)

    tracker.seed_from_detected_fines()  # перезапуск

    assert "HH888JJ" not in tracker.last_full_check_by_car(["HH888JJ"])
    rows, _ = _controller(legacy, tracker)._debt_rows_with_statuses(legacy.tasks.list_debt_car_groups())
    by_car = {car: breakdown for car, _, breakdown in rows}
    assert by_car["HH888JJ"].unpaid_amount == 80  # страховка: снимок, а не 0 ₾
    repository.close()
