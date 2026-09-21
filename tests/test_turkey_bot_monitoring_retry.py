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

from reader.turkey_bot.monitoring import monitoring_service as monitoring_service_module
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


class _FixedDatetime(datetime):
    """Подменяет datetime.now() ВНУТРИ monitoring_service.py на
    фиксированный synthetic _NOW (тот же приём, что и в
    tests/test_turkey_bot_monitoring_schedule.py::_FixedDatetime для
    scheduler_job.py) — необходимо, потому что check_now_and_notify_if_
    changed() сама вызывает datetime.now(timezone.utc) без параметра (см.
    её докстрок в monitoring_service.py: "оставлен отдельным методом" —
    никакого способа передать ей own "now" снаружи нет и не должно
    появляться, это не retry-логика, а просто источник текущего времени).

    БЕЗ этой подмены тест ставил retry на реальное "текущее" время (из
    production-кода), а проверял его через `run_due_retries(_NOW +
    timedelta(minutes=5))` с ХАРДКОЖЕННЫМ _NOW — пока реальная календарная
    дата была раньше _NOW, _NOW+5мин случайно оказывался позже реального
    времени постановки retry, и тест проходил. Как только реальная дата
    ДОГНАЛА _NOW (см. задачу "почини 5 падающих тестов" — сегодняшняя
    дата тоже 2026-09-21), это совпадение сломалось: реальное "сейчас"
    внутри check_now_and_notify_if_changed могло оказаться ПОЗЖЕ 13:00:00
    UTC в тот же день, из-за чего retry_at (реальное now + 5 минут)
    оказывался ПОЗЖЕ _NOW + 5 минут — на момент запроса run_due_retries
    retry ещё не считался наступившим. С подменой real datetime.now()
    внутри monitoring_service.py на _NOW тест снова полностью
    детерминирован и не зависит от того, какой сегодня реальный день."""

    _fixed: "datetime"

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime = _NOW) -> None:
    frozen = _FixedDatetime
    frozen._fixed = fixed_now
    monkeypatch.setattr(monitoring_service_module, "datetime", frozen)


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


async def test_retry_checks_only_the_previously_failed_providers(monkeypatch):
    """Явное требование задачи п.5: "13:05: проверяем ТОЛЬКО Avrasya. Не
    делать заново GIB/KGM." (пример из задачи, здесь — тот же принцип с
    подстановкой конкретного провалившегося провайдера)."""
    _freeze_now(monkeypatch)
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


async def test_retry_success_produces_final_successful_provider_result(monkeypatch):
    _freeze_now(monkeypatch)
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


async def test_retry_all_fail_stays_error_with_no_second_retry(monkeypatch):
    """Явное требование задачи п.3: "После второго блока из 25: если
    provider всё ещё не проверен -> ERROR до следующего обычного слота...
    После +5 минут третьего retry нет."."""
    _freeze_now(monkeypatch)
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


async def test_no_infinite_retries_across_many_polls(monkeypatch):
    """Дополнительная защита от регрессии: даже если Scheduler опрашивает
    has_due_retries/run_due_retries много раз подряд после истечения due
    retry, ни один retry не выполняется повторно (очередь однократно
    потребляется, см. run_due_retries — удаляет due-записи ДО запуска)."""
    _freeze_now(monkeypatch)
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


async def test_error_provider_never_produces_debt_resolved_notification_even_after_retry(monkeypatch):
    """Явное требование задачи п.4 (safety-критично перед batch) — ERROR
    (в т.ч. после исчерпанных retry) НИКОГДА не трактуется как "долг
    исчез", даже если раньше у этого provider был зафиксирован реальный
    долг.

    _freeze_now — без него эта проверка тоже была vacuous (см. задачу
    "почини 5 падающих тестов" — тот же root cause): notifier.sent == []
    выполняется одинаково что при реально прошедшем retry (ERROR ->
    ERROR, уведомления нет), что при НЕ выполнившемся retry вовсе
    (нечему было генерировать уведомление) — добавлена явная проверка
    len(check_service.calls) == 2, чтобы тест реально доказывал именно
    первое, а не второе."""
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

    _freeze_now(monkeypatch)
    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert len(check_service.calls) == 2  # retry ДЕЙСТВИТЕЛЬНО выполнился, проверка не vacuous
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


async def test_retry_does_not_resend_notification_already_sent_in_first_pass(monkeypatch):
    """Явное требование задачи п.5/п.8: "не создавать duplicate user
    notifications" — GIB меняется и уведомляет В ПЕРВОМ проходе; retry
    (только Avrasya) НЕ должен снова сравнивать/уведомлять по GIB.

    _freeze_now — тот же root cause, что и у 5 падавших тестов: без него
    вторая проверка (len(notifier.sent) == 1 ПОСЛЕ run_due_retries) была
    vacuous — она проходит одинаково и если retry реально выполнился и
    ничего нового не уведомил, и если retry вообще не выполнился."""
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

    _freeze_now(monkeypatch)
    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    assert len(notifier.sent) == 1  # GIB new_debt

    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert len(check_service.calls) == 2  # retry ДЕЙСТВИТЕЛЬНО выполнился, проверка не vacuous
    assert len(notifier.sent) == 1  # retry (Avrasya NO_DEBT, ничего нового) не добавил дублей


async def test_retry_run_saved_with_distinct_initiator_not_duplicating_first_pass_row(monkeypatch):
    _freeze_now(monkeypatch)
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


async def test_next_check_at_not_touched_by_retry_pass(monkeypatch):
    """Retry не должен трогать next_check_at повторно (уже выставлен
    первым проходом) — иначе подписка "сдвинулась" бы дальше положенного
    следующего слота.

    Без _freeze_now эта проверка была ЛОЖНО-ПОЛОЖИТЕЛЬНОЙ (vacuous pass)
    при том же расхождении реального/synthetic времени, что и у 5
    исходно падавших тестов файла (см. _FixedDatetime выше): если retry
    вообще не выполняется (реальный retry_at ещё не наступил к моменту
    run_due_retries(_NOW + 5мин)), next_check_at естественно не меняется
    — assert проходил, ничего не проверяя по сути. С _freeze_now retry
    гарантированно выполняется, и проверка снова осмысленна."""
    _freeze_now(monkeypatch)
    first = _result(
        _provider("gib", ProviderStatus.NO_DEBT),
        _provider("avrasya", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_result = _result(_provider("avrasya", ProviderStatus.NO_DEBT))
    service, _runs, subscriptions, _notifier, check_service, subscription = _make_service([first, retry_result])

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    after_first = subscriptions.get(telegram_user_id=_USER_ID, plate=_PLATE).next_check_at

    await service.run_due_retries(_NOW + timedelta(minutes=5))
    after_retry = subscriptions.get(telegram_user_id=_USER_ID, plate=_PLATE).next_check_at

    assert len(check_service.calls) == 2  # retry ДЕЙСТВИТЕЛЬНО выполнился, проверка не vacuous
    assert after_first == after_retry
