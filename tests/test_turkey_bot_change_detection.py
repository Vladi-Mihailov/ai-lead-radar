"""Тесты ChangeDetector (см. design report "Перестроить UX Turkey test
bot" п.10) — сравнение по provider (не только total на верхнем уровне),
ERROR провайдера НИКОГДА не считается "долг исчез", unchanged snapshot не
должен генерировать уведомление. Плюс интеграционные тесты
TurkeyMonitoringService.run_scheduled_batch (см. п.8/п.11/п.12) — без
реальной сети, только fake check_service/notifier."""

from datetime import datetime, timezone
from decimal import Decimal

from reader.turkey_bot.monitoring.change_detector import ChangeType, detect_changes
from reader.turkey_bot.monitoring.monitoring_service import TurkeyMonitoringService
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

_NOW = datetime.now(timezone.utc)


def _provider(provider: str, status: ProviderStatus, total: Decimal, *, error_type: str | None = None):
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=error_type, checked_at=_NOW,
    )


def _result(*providers: ProviderCheckResult) -> UnifiedCheckResult:
    from reader.turkey_bot.unified.models import (
        derive_overall_status,
        total_amount_for,
    )
    return UnifiedCheckResult(
        plate="A123AA123", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


# ---- Unit-тесты detect_changes() ----

def test_no_previous_and_no_debt_now_gives_no_changes():
    current = _result(_provider("gib", ProviderStatus.NO_DEBT, Decimal(0)))
    changes = detect_changes({"gib": None}, current)
    assert changes == []


def test_new_debt_detected():
    current = _result(_provider("gib", ProviderStatus.HAS_DEBT, Decimal(500)))
    changes = detect_changes({"gib": None}, current)
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.NEW_DEBT
    assert changes[0].current_total == Decimal(500)


def test_debt_resolved_detected():
    previous = _provider("gib", ProviderStatus.HAS_DEBT, Decimal(500))
    current = _result(_provider("gib", ProviderStatus.NO_DEBT, Decimal(0)))
    changes = detect_changes({"gib": previous}, current)
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.DEBT_RESOLVED


def test_amount_changed_detected():
    previous = _provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(2580))
    current = _result(_provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(2660)))
    changes = detect_changes({"avrasya": previous}, current)
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.AMOUNT_CHANGED
    assert changes[0].previous_total == Decimal(2580)
    assert changes[0].current_total == Decimal(2660)


def test_unchanged_amount_gives_no_changes():
    previous = _provider("kgm", ProviderStatus.HAS_DEBT, Decimal(2660))
    current = _result(_provider("kgm", ProviderStatus.HAS_DEBT, Decimal(2660)))
    changes = detect_changes({"kgm": previous}, current)
    assert changes == []


def test_error_provider_is_never_treated_as_debt_resolved():
    """КЛЮЧЕВОЙ тест-кейс design report п.10: предыдущий successful result
    HAS_DEBT, текущий provider ERROR — НЕ должно генерировать
    "долг исчез"."""
    previous = _provider("kgm", ProviderStatus.HAS_DEBT, Decimal(2660))
    current = _result(_provider("kgm", ProviderStatus.ERROR, Decimal(0), error_type="transport_error"))
    changes = detect_changes({"kgm": previous}, current)
    assert changes == []


def test_error_provider_is_never_treated_as_new_debt():
    current = _result(_provider("kgm", ProviderStatus.ERROR, Decimal(0), error_type="transport_error"))
    changes = detect_changes({"kgm": None}, current)
    assert changes == []


def test_multiple_providers_compared_independently():
    previous = {
        "gib": _provider("gib", ProviderStatus.NO_DEBT, Decimal(0)),
        "avrasya": _provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(100)),
        "kgm": _provider("kgm", ProviderStatus.HAS_DEBT, Decimal(200)),
    }
    current = _result(
        _provider("gib", ProviderStatus.HAS_DEBT, Decimal(50)),  # новый долг
        _provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(100)),  # без изменений
        _provider("kgm", ProviderStatus.ERROR, Decimal(0), error_type="transport_error"),  # игнорируется
    )
    changes = detect_changes(previous, current)
    assert len(changes) == 1
    assert changes[0].provider == "gib"
    assert changes[0].change_type == ChangeType.NEW_DEBT


# ---- Интеграция: TurkeyMonitoringService.run_scheduled_batch ----

class _FakeCheckService:
    def __init__(self, results: list[UnifiedCheckResult]):
        self._results = list(results)

    async def check(self, plate: str, **kwargs) -> UnifiedCheckResult:
        return self._results.pop(0)


class _RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send(self, *, telegram_chat_id: int, text: str) -> None:
        self.sent.append((telegram_chat_id, text))


def _make_monitoring_service(results: list[UnifiedCheckResult]):
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    notifier = _RecordingNotifier()
    service = TurkeyMonitoringService(_FakeCheckService(results), runs, subscriptions, notifier)
    return service, runs, subscriptions, notifier


async def test_unchanged_snapshot_across_two_scheduled_runs_sends_no_notification():
    """Два ПОСЛЕДОВАТЕЛЬНЫХ unified-check (имитация 13:00 -> 21:00, см.
    check_now_and_notify_if_changed — та же логика, что и
    run_scheduled_batch на одну подписку, без ожидания реального
    next_check_at, см. test_turkey_monitoring_schedule.py про сам
    scheduling отдельно)."""
    no_debt = _result(
        _provider("gib", ProviderStatus.NO_DEBT, Decimal(0)),
        _provider("avrasya", ProviderStatus.NO_DEBT, Decimal(0)),
        _provider("kgm", ProviderStatus.NO_DEBT, Decimal(0)),
    )
    service, runs, subscriptions, notifier = _make_monitoring_service([no_debt, no_debt])
    subscription = subscriptions.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert notifier.sent == []
    assert runs.count_total() == 2


async def test_new_debt_across_scheduled_runs_sends_notification():
    no_debt = _result(_provider("gib", ProviderStatus.NO_DEBT, Decimal(0)))
    has_debt = _result(_provider("gib", ProviderStatus.HAS_DEBT, Decimal(237)))
    service, _runs, subscriptions, notifier = _make_monitoring_service([no_debt, has_debt])
    subscription = subscriptions.enable(telegram_user_id=1, telegram_chat_id=42, plate="A123AA123")

    await service.check_now_and_notify_if_changed(subscription, initiator="13:00")
    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert len(notifier.sent) == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == 42
    assert "Новая задолженность" in text


async def test_amount_change_across_scheduled_runs_sends_notification():
    """Базовый ("уже известный") результат сохраняется НАПРЯМУЮ через
    run_repository (имитирует "предыдущий успешный прогон уже есть в
    истории") — так тест проверяет именно AMOUNT_CHANGED в изоляции, не
    смешивая его с NEW_DEBT первого-в-жизни прогона."""
    baseline = _result(_provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(2580)))
    changed = _result(_provider("avrasya", ProviderStatus.HAS_DEBT, Decimal(2660)))
    service, runs, subscriptions, notifier = _make_monitoring_service([changed])
    subscription = subscriptions.enable(telegram_user_id=1, telegram_chat_id=42, plate="A123AA123")
    runs.save(baseline, telegram_user_id=1, telegram_chat_id=42, initiator="manual")

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert len(notifier.sent) == 1
    assert "Было" in notifier.sent[0][1]
    assert "Стало" in notifier.sent[0][1]


async def test_provider_error_after_has_debt_does_not_send_resolved_notification():
    baseline = _result(_provider("kgm", ProviderStatus.HAS_DEBT, Decimal(2660)))
    errored = _result(_provider("kgm", ProviderStatus.ERROR, Decimal(0), error_type="transport_error"))
    service, runs, subscriptions, notifier = _make_monitoring_service([errored])
    subscription = subscriptions.enable(telegram_user_id=1, telegram_chat_id=42, plate="A123AA123")
    runs.save(baseline, telegram_user_id=1, telegram_chat_id=42, initiator="manual")

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert notifier.sent == []


async def test_inactive_subscription_is_not_checked():
    service, runs, subscriptions, notifier = _make_monitoring_service([])
    subscriptions.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")
    subscriptions.disable(telegram_user_id=1, plate="A123AA123")

    await service.run_scheduled_batch("13:00")

    assert runs.count_total() == 0
    assert notifier.sent == []
