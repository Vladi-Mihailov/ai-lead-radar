"""Тесты baseline ChangeDetector'а ПО ПОДПИСЧИКУ (см. задачу "TR-0a: база
сравнения турецкого мониторинга по подписчику").

Production-находка: TurkeyMonitoringService брал baseline через
get_last_successful_provider_result(plate, provider) — по ВСЕМ runs этого
номера, без фильтра по владельцу. Любой сохранённый run (ручная проверка
менеджера, "🔄 Проверить авто с задолженностью", мониторинг ДРУГОГО
подписчика того же номера в том же батче) "съедал" изменение — владелец
затем видел "без изменений" и не получал уведомление о новом долге.

Здесь фиксируется новая семантика (TurkeyCheckRunRepository.
get_last_successful_provider_result_for_subscriber): baseline подписчика —
только runs, результат которых считается показанным ЕМУ САМОМУ (свой
мониторинг/retry и своя ручная "🔎 Проверить сейчас"). Никаких реальных
HTTP/CAPTCHA — только fake check_service/notifier, как в
test_turkey_bot_change_detection.py."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reader.turkey_bot.conversation import _INITIATOR_MANUAL
from reader.turkey_bot.debt_refresh_service import _INITIATOR_MANAGER_REFRESH
from reader.turkey_bot.monitoring import monitoring_service as monitoring_service_module
from reader.turkey_bot.monitoring.monitoring_service import TurkeyMonitoringService
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
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

_NOW = datetime(2026, 11, 4, 18, 0, 0, tzinfo=timezone.utc)
_PLATE = "34ABC123"

_OWNER_ID = 111
_OWNER_CHAT = 1110
_SECOND_OWNER_ID = 222
_SECOND_OWNER_CHAT = 2220
_MANAGER_ID = 999
_MANAGER_CHAT = 9990


def _provider(name, status, total=Decimal(0), *, error_type=None, checked_at=_NOW):
    return ProviderCheckResult(
        provider=name, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=error_type, checked_at=checked_at,
    )


def _result(*providers):
    return UnifiedCheckResult(
        plate=_PLATE, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


def _gib(status, total=Decimal(0), **kwargs):
    return _result(_provider("gib", status, total, **kwargs))


class _ScriptedCheckService:
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
    notifier = _RecordingNotifier()
    check_service = _ScriptedCheckService(results)
    service = TurkeyMonitoringService(check_service, runs, subscriptions, notifier)
    return service, runs, subscriptions, notifier, check_service


class _FixedDatetime(datetime):
    """Тот же приём, что и в test_turkey_bot_monitoring_retry.py — retry
    ставится от datetime.now() внутри monitoring_service.py."""

    _fixed: "datetime"

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime = _NOW) -> None:
    _FixedDatetime._fixed = fixed_now
    monkeypatch.setattr(monitoring_service_module, "datetime", _FixedDatetime)


# ---- TurkeyCheckRunRepository.get_last_successful_provider_result_for_subscriber ----

def test_own_monitoring_run_is_baseline_and_foreign_manual_run_is_ignored():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    # Более НОВЫЙ run того же номера — ручная проверка менеджера.
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_MANAGER_ID,
              telegram_chat_id=_MANAGER_CHAT, initiator=_INITIATOR_MANUAL)

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline is not None
    assert baseline.status == ProviderStatus.NO_DEBT


def test_manager_refresh_saved_under_owner_id_is_not_baseline():
    """manager_refresh сохраняется под telegram_user_id ВЛАДЕЛЬЦА (см.
    debt_refresh_service.py), но владелец этот результат не видел."""
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator=_INITIATOR_MANAGER_REFRESH)

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline.status == ProviderStatus.NO_DEBT


def test_own_manual_check_is_baseline():
    """Своя ручная "🔎 Проверить сейчас" — владелец видел результат на
    экране, текущая семантика сохраняется."""
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator=_INITIATOR_MANUAL)

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline.status == ProviderStatus.HAS_DEBT
    assert baseline.total_amount == Decimal(500)


def test_own_monitoring_retry_run_is_baseline():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(70)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00_retry")

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline.total_amount == Decimal(70)


def test_other_subscriber_monitoring_run_is_not_baseline():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_SECOND_OWNER_ID,
              telegram_chat_id=_SECOND_OWNER_CHAT, initiator="monitoring_21:00")

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline.status == ProviderStatus.NO_DEBT


def test_error_result_is_skipped_and_older_own_success_is_used():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(300)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.ERROR, error_type="captcha_rejected"), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00")

    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert baseline.status == ProviderStatus.HAS_DEBT
    assert baseline.total_amount == Decimal(300)


def test_only_foreign_runs_give_no_baseline():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_MANAGER_ID,
              telegram_chat_id=_MANAGER_CHAT, initiator=_INITIATOR_MANUAL)

    assert runs.get_last_successful_provider_result_for_subscriber(
        _PLATE, "gib", telegram_user_id=_OWNER_ID,
    ) is None


def test_initiator_prefix_match_is_exact():
    """"monitoring_" — буквальный префикс (LIKE с ESCAPE), а не шаблон, в
    котором "_" совпадает с любым символом."""
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator="monitoringX21:00")

    assert runs.get_last_successful_provider_result_for_subscriber(
        _PLATE, "gib", telegram_user_id=_OWNER_ID,
    ) is None


def test_other_provider_and_plate_are_not_mixed():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(
        _result(_provider("kgm", ProviderStatus.HAS_DEBT, Decimal(40))),
        telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00",
    )
    other_plate = UnifiedCheckResult(
        plate="06XYZ999", started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status((_provider("gib", ProviderStatus.HAS_DEBT, Decimal(9)),)),
        total_amount=Decimal(9), providers=(_provider("gib", ProviderStatus.HAS_DEBT, Decimal(9)),),
    )
    runs.save(other_plate, telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00")

    assert runs.get_last_successful_provider_result_for_subscriber(
        _PLATE, "gib", telegram_user_id=_OWNER_ID,
    ) is None
    assert runs.get_last_successful_provider_result_for_subscriber(
        _PLATE, "kgm", telegram_user_id=_OWNER_ID,
    ).total_amount == Decimal(40)


def test_legacy_plate_wide_method_is_unchanged():
    """Старый get_last_successful_provider_result() остаётся как был
    (по всему номеру) — меняется только то, что вызывает мониторинг."""
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_MANAGER_ID,
              telegram_chat_id=_MANAGER_CHAT, initiator=_INITIATOR_MANUAL)

    assert runs.get_last_successful_provider_result(_PLATE, "gib").status == ProviderStatus.HAS_DEBT


def test_baseline_lookup_does_not_write():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    before = runs.count_total()

    runs.get_last_successful_provider_result_for_subscriber(_PLATE, "gib", telegram_user_id=_OWNER_ID)

    assert runs.count_total() == before


# ---- TurkeyMonitoringService: уведомления больше не теряются ----

async def test_manager_manual_check_does_not_swallow_owner_new_debt_notification():
    service, runs, subscriptions, notifier, _ = _make_service([_gib(ProviderStatus.HAS_DEBT, Decimal(500))])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    # Менеджер вручную проверил тот же номер ДО планового прогона и уже увидел долг.
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_MANAGER_ID,
              telegram_chat_id=_MANAGER_CHAT, initiator=_INITIATOR_MANUAL)

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == _OWNER_CHAT
    assert "Новая задолженность" in notifier.sent[0][1]


async def test_manager_refresh_does_not_swallow_owner_new_debt_notification():
    service, runs, subscriptions, notifier, _ = _make_service([_gib(ProviderStatus.HAS_DEBT, Decimal(500))])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator=_INITIATOR_MANAGER_REFRESH)

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert [chat for chat, _ in notifier.sent] == [_OWNER_CHAT]


async def test_two_subscribers_of_same_plate_are_both_notified_in_one_batch():
    """КЛЮЧЕВОЙ регрессионный кейс: раньше run первого подписчика
    становился baseline второго в том же батче, и второй не получал
    уведомление о том же новом долге."""
    has_debt = _gib(ProviderStatus.HAS_DEBT, Decimal(500))
    service, runs, subscriptions, notifier, check_service = _make_service([has_debt, has_debt])
    subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    subscriptions.enable(telegram_user_id=_SECOND_OWNER_ID, telegram_chat_id=_SECOND_OWNER_CHAT, plate=_PLATE)
    for user_id, chat_id in ((_OWNER_ID, _OWNER_CHAT), (_SECOND_OWNER_ID, _SECOND_OWNER_CHAT)):
        runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=user_id, telegram_chat_id=chat_id,
                  initiator="monitoring_21:00")

    await service.run_scheduled_batch("21:00")

    assert len(check_service.calls) == 2
    assert sorted(chat for chat, _ in notifier.sent) == sorted([_OWNER_CHAT, _SECOND_OWNER_CHAT])
    assert all("Новая задолженность" in text for _, text in notifier.sent)


async def test_two_subscribers_unchanged_next_run_sends_no_duplicates():
    has_debt = _gib(ProviderStatus.HAS_DEBT, Decimal(500))
    service, runs, subscriptions, notifier, _ = _make_service([has_debt, has_debt, has_debt, has_debt])
    first = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    second = subscriptions.enable(
        telegram_user_id=_SECOND_OWNER_ID, telegram_chat_id=_SECOND_OWNER_CHAT, plate=_PLATE,
    )

    await service.check_now_and_notify_if_changed(first, initiator="21:00")
    await service.check_now_and_notify_if_changed(second, initiator="21:00")
    await service.check_now_and_notify_if_changed(first, initiator="21:00")
    await service.check_now_and_notify_if_changed(second, initiator="21:00")

    assert sorted(chat for chat, _ in notifier.sent) == sorted([_OWNER_CHAT, _SECOND_OWNER_CHAT])


async def test_debt_resolved_seen_only_by_other_subscriber_is_still_reported_to_owner():
    service, runs, subscriptions, notifier, _ = _make_service([_gib(ProviderStatus.NO_DEBT)])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_SECOND_OWNER_ID,
              telegram_chat_id=_SECOND_OWNER_CHAT, initiator="monitoring_21:00")

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert [chat for chat, _ in notifier.sent] == [_OWNER_CHAT]


async def test_own_manual_check_still_suppresses_next_scheduled_notification():
    """Q3 — собственная ручная проверка владельца по-прежнему считается
    "уже показанным" результатом."""
    service, runs, subscriptions, notifier, _ = _make_service([_gib(ProviderStatus.HAS_DEBT, Decimal(500))])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(_gib(ProviderStatus.NO_DEBT), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="monitoring_21:00")
    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_OWNER_ID,
              telegram_chat_id=_OWNER_CHAT, initiator=_INITIATOR_MANUAL)

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert notifier.sent == []


async def test_monitoring_still_saves_run_under_subscriber_with_monitoring_initiator():
    service, runs, subscriptions, _notifier, _ = _make_service([_gib(ProviderStatus.NO_DEBT)])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert runs.count_total() == 1
    assert runs.count_by_initiator("monitoring_21:00") == 1
    assert runs.get_latest_for_owner(plate=_PLATE, telegram_user_id=_OWNER_ID) is not None


async def test_retry_pass_uses_subscriber_baseline(monkeypatch):
    """Первый проход: gib упал на CAPTCHA -> retry через 5 минут. Между
    проходами менеджер вручную увидел новый долг gib. Retry должен
    сравнить с СОБСТВЕННЫМ baseline владельца и уведомить его."""
    _freeze_now(monkeypatch)
    first_pass = _result(
        _provider("gib", ProviderStatus.ERROR, error_type="captcha_unavailable"),
        _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", ProviderStatus.NO_DEBT),
    )
    retry_pass = _gib(ProviderStatus.HAS_DEBT, Decimal(500))
    service, runs, subscriptions, notifier, check_service = _make_service([first_pass, retry_pass])
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(
        _result(
            _provider("gib", ProviderStatus.NO_DEBT),
            _provider("avrasya", ProviderStatus.NO_DEBT),
            _provider("kgm", ProviderStatus.NO_DEBT),
        ),
        telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00",
    )

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")
    assert notifier.sent == []

    runs.save(_gib(ProviderStatus.HAS_DEBT, Decimal(500)), telegram_user_id=_MANAGER_ID,
              telegram_chat_id=_MANAGER_CHAT, initiator=_INITIATOR_MANUAL)

    await service.run_due_retries(_NOW + timedelta(minutes=5))

    assert check_service.calls[-1][1]["providers"] == ("gib",)
    assert [chat for chat, _ in notifier.sent] == [_OWNER_CHAT]
    assert "Новая задолженность" in notifier.sent[0][1]
