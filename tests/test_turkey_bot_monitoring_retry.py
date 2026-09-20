"""
Тесты reader/turkey_bot/monitoring/monitoring_service.py — retry
orchestration (см. задачу "Retry orchestration Unified Turkey checks"
п.2/п.3/п.5): +5-минутный повторный проход ТОЛЬКО для providers,
упавших именно на CAPTCHA (captcha_unavailable/captcha_rejected) в
первом (25-попыточном) проходе — не третьего retry, provider-specific
state, никаких дублирующих уведомлений. Никаких реальных HTTP/CAPTCHA —
только детерминированный fake check_service, отслеживающий переданные
аргументы (max_attempts/mode/providers)."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.turkey_bot.monitoring.monitoring_service import (
    TurkeyMonitoringService,
)
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import (
    TurkeyCheckRunRepository,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 9, 21, 13, 0, 0, tzinfo=timezone.utc)
_USER_ID = 685137235
_CHAT_ID = 685137235
_PLATE = "M295YB196"


def _provider(name, status, *, total=Decimal(0), error_type=None):
    return ProviderCheckResult(
        provider=name, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=error_type, checked_at=_NOW,
    )


def _result(*providers):
    return UnifiedCheckResult(
        plate=_PLATE, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


class _ScriptedCheckService:
    """Возвращает следующий результат из последовательности при каждом
    вызове check() — записывает (plate, kwargs) каждого вызова, чтобы
    тесты могли проверить, что monitoring service запросило РОВНО те
    providers/max_attempts/mode, что нужно (см. задачу п.5: "не делать
    заново GIB/KGM")."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, dict]] = []

    async def check(self, plate, **kwargs):
        self.calls.append((plate, kwargs))
        return self._results.pop(0)


class _RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send(self, *, telegram_chat_id, text):
        self.sent.append((telegram_chat_id, text))


def _make_service(results):
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    check_service = _ScriptedCheckService(results)
    notifier = _RecordingNotifier()
    service = TurkeyMonitoringService(check_service, runs, subscriptions, notifier)
    subscription = subscriptions.enable(telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate=_PLATE)
    return service, runs, subscriptions, notifier, check_service, subscription


async def test_all_providers_success_schedules_no_retry():
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    service, _runs, _subs, _notifier, check_service, subscription = _make_service([first])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")

    assert not service.has_due_retries(_NOW + timedelta(minutes=10))
    assert len(check_service.calls) == 1


async def test_all_providers_captcha_error_schedules_retry_in_5_minutes():
    """Явное требование задачи п.2/п.3: "если после 25 попыток provider
    всё ещё ERROR из-за CAPTCHA... создать retry ровно через 5 минут" —
    check_now_and_notify_if_changed вычисляет `now` сам (реальное
    datetime.now(timezone.utc)), поэтому тест сверяется с реальным
    временем ДО/ПОСЛЕ вызова, а не с фиксированной константой."""
    first = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.ERROR, error_type="captcha_unavailable"),
    )
    service, _runs, _subs, _notifier, _check_service, subscription = _make_service([first])

    before = datetime.now(timezone.utc)
    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    after = datetime.now(timezone.utc)

    assert not service.has_due_retries(before)
    assert not service.has_due_retries(after + timedelta(minutes=4))
    assert service.has_due_retries(after + timedelta(minutes=5, seconds=1))


async def test_transport_error_does_not_schedule_a_retry():
    """Retry — ТОЛЬКО для captcha_unavailable/captcha_rejected (см. задачу
    п.3: "остался неполным из-за CAPTCHA") — transport_error НЕ считается
    "неполным из-за CAPTCHA"."""
    first = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="transport_error"),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    service, _runs, _subs, _notifier, _check_service, subscription = _make_service([first])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")

    assert not service.has_due_retries(_NOW + timedelta(minutes=10))


async def test_retry_checks_only_the_previously_failed_providers():
    """Явное требование задачи п.5: "13:05: проверяем ТОЛЬКО Avrasya. Не
    делать заново GIB/KGM." (пример из задачи, здесь — тот же принцип с
    подстановкой конкретного провалившегося провайдера)."""
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    service, _runs, _subs, _notifier, check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert len(check_service.calls) == 2
    retry_plate, retry_kwargs = check_service.calls[1]
    assert retry_plate == _PLATE
    assert retry_kwargs["providers"] == ("avrasya",)
    assert retry_kwargs["mode"] == "scheduled_retry"


async def test_retry_success_produces_final_successful_provider_result():
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.HAS_DEBT, total=Decimal(500)))
    service, runs, _subs, _notifier, _check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    retry_runs = [r for r in runs.list_by_plate(_PLATE, limit=10) if r.provider_result("avrasya").status == ProviderStatus.HAS_DEBT]
    assert len(retry_runs) == 1
    assert retry_runs[0].provider_result("avrasya").total_amount == Decimal(500)


async def test_retry_all_fail_stays_error_with_no_second_retry():
    """Явное требование задачи п.3: "После второго блока из 25: если
    provider всё ещё не проверен -> ERROR до следующего обычного слота...
    После +5 минут третьего retry нет."."""
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"))
    service, _runs, _subs, _notifier, check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert not service.has_due_retries(_NOW + timedelta(minutes=15))
    assert len(check_service.calls) == 2  # ровно 2: первый проход + один retry, третьего нет


async def test_no_infinite_retries_across_many_polls():
    """Дополнительная защита от регрессии: даже если Scheduler опрашивает
    has_due_retries/run_due_retries много раз подряд после истечения due
    retry, ни один retry не выполняется повторно (очередь однократно
    потребляется, см. run_due_retries — удаляет due-записи ДО запуска)."""
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    service, _runs, _subs, _notifier, check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    later = _NOW + timedelta(minutes=5)
    for _ in range(5):
        await service.run_due_retries(later)

    assert len(check_service.calls) == 2


async def test_error_provider_never_produces_debt_resolved_notification_even_after_retry():
    """Явное требование задачи п.4 (safety-критично перед batch) — ERROR
    (в т.ч. после исчерпанных retry) НИКОГДА не трактуется как "долг
    исчез", даже если раньше у этого provider был зафиксирован реальный
    долг."""
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    subscription = subscriptions.enable(telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate=_PLATE)
    # Ранее зафиксированный реальный долг по Avrasya (baseline для сравнения).
    baseline = _result(_provider("avrasya", ProviderStatus.HAS_DEBT, total=Decimal(2580)))
    runs.save(baseline, telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, initiator="manual")

    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"))
    notifier = _RecordingNotifier()
    check_service = _ScriptedCheckService([first, retry_result])
    service = TurkeyMonitoringService(check_service, runs, subscriptions, notifier)

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert notifier.sent == []  # никакого "долг погашен" из ERROR


async def test_partial_run_still_notifies_for_successful_providers():
    """Явное требование задачи п.4: "PARTIAL: успешные providers можно
    сравнивать" — ERROR одного провайдера не блокирует уведомление по
    реально изменившемуся другому провайдеру в ТОМ ЖЕ run."""
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    subscription = subscriptions.enable(telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate=_PLATE)
    baseline = _result(_provider("gib", ProviderStatus.NO_DEBT))
    runs.save(baseline, telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, initiator="manual")

    first = _result(
        _provider("gib", ProviderStatus.HAS_DEBT, total=Decimal(80)),  # реальное изменение
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    notifier = _RecordingNotifier()
    check_service = _ScriptedCheckService([first])
    service = TurkeyMonitoringService(check_service, runs, subscriptions, notifier)

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")

    assert len(notifier.sent) == 1
    assert "Новая задолженность" in notifier.sent[0][1]


async def test_retry_does_not_resend_notification_already_sent_in_first_pass():
    """Явное требование задачи п.5/п.8: "не создавать duplicate user
    notifications" — GIB меняется и уведомляет В ПЕРВОМ проходе; retry
    (только Avrasya) НЕ должен снова сравнивать/уведомлять по GIB."""
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    subscription = subscriptions.enable(telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate=_PLATE)
    baseline = _result(_provider("gib", ProviderStatus.NO_DEBT))
    runs.save(baseline, telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, initiator="manual")

    first = _result(
        _provider("gib", ProviderStatus.HAS_DEBT, total=Decimal(80)),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    notifier = _RecordingNotifier()
    check_service = _ScriptedCheckService([first, retry_result])
    service = TurkeyMonitoringService(check_service, runs, subscriptions, notifier)

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    assert len(notifier.sent) == 1  # GIB new_debt

    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert len(notifier.sent) == 1  # retry (Avrasya NO_DEBT, ничего нового) не добавил дублей


async def test_retry_run_saved_with_distinct_initiator_not_duplicating_first_pass_row():
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    service, runs, _subs, _notifier, _check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert runs.count_by_initiator("monitoring_13:00") == 1
    assert runs.count_by_initiator("monitoring_13:00_retry") == 1
    assert runs.count_total() == 2  # ровно 2 run'а, не 25/50


async def test_next_check_at_not_touched_by_retry_pass():
    """Retry не должен трогать next_check_at повторно (уже выставлен
    первым проходом) — иначе подписка "сдвинулась" бы дальше положенного
    следующего слота."""
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    service, _runs, subscriptions, _notifier, _check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    after_first = subscriptions.get(telegram_user_id=_USER_ID, plate=_PLATE).next_check_at

    await service.run_due_retries(_NOW + timedelta(minutes=5))
    after_retry = subscriptions.get(telegram_user_id=_USER_ID, plate=_PLATE).next_check_at

    assert after_first == after_retry
