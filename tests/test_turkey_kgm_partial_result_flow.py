"""
Частичный ответ KGM (kind == "partial", часть операторов не проверена)
по всей цепочке Turkey bot: unified-результат, итоговый статус и сумма,
сохранение и выборки run_repository, мониторинг и уведомления, список
долгов и тексты.

Главное требование: непроверенный оператор НЕ может ни обнулить
известную задолженность, ни вызвать уведомление о её погашении/изменении,
а итог проверки — PARTIAL, не HAS_DEBT/NO_DEBT. Разобранные секции при
этом сохраняются и показываются. Никаких реальных HTTP/CAPTCHA.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from reader.turkey_bot.avrasya.models import AvrasyaCaptchaChallenge, AvrasyaSubmitOutcome
from reader.turkey_bot.debt_refresh_service import TurkeyDebtRefreshService
from reader.turkey_bot.gib.models import CaptchaChallenge, GibSubmitOutcome
from reader.turkey_bot.kgm.models import (
    KgmCaptchaChallenge,
    KgmDebtItem,
    KgmDiagnostics,
    KgmOperatorResult,
    KgmSubmitOutcome,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.monitoring_service import TurkeyMonitoringService
from reader.turkey_bot.monitoring.subscription_repository import TurkeyMonitoringSubscriptionRepository
from reader.turkey_bot.statistics_service import TurkeyProviderFreshness, TurkeyStatisticsService
from reader.turkey_bot.texts import (
    _format_debt_provider_state,
    _format_try_amount,
    format_debt_refresh_summary,
    format_unified_check_result,
)
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService, _kgm_outcome_to_result
from reader.turkey_bot.unified.models import (
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    partial_error_type,
    total_amount_for,
    unchecked_sections,
    unchecked_sections_from_error_type,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

_NOW = datetime(2026, 11, 12, 6, 30, 0, tzinfo=timezone.utc)
_PLATE = "TEST0001"
_OWNER_ID = 111
_OWNER_CHAT = 1110
_PARTIAL_ERROR_TYPE = "partial_unchecked:aydin_denizli"


def _kgm_item(amount: str) -> KgmDebtItem:
    return KgmDebtItem(
        operator="kgm", date_time=datetime(2026, 11, 10, 9, 39, 48), entry_station="GEBZE",
        exit_station="İZMİT", vehicle_class="1", base_toll=Decimal(amount), payable_amount=Decimal(amount),
        penalty_free_deadline=None,
    )


def _partial_outcome(*amounts: str) -> KgmSubmitOutcome:
    items = tuple(_kgm_item(amount) for amount in amounts)
    total = sum((item.payable_amount for item in items), Decimal(0))
    operators = (KgmOperatorResult(operator_key="kgm", operator_name="KGM", items=items, subtotal=total),) if items else ()
    return KgmSubmitOutcome(
        kind="partial", operators=operators, kgm_total=total, yid_total=Decimal(0), grand_total=total,
        message="unchecked operator sections: aydin_denizli: 'site text'",
        diagnostics=KgmDiagnostics(
            category="result_structure", code="section_label_unrecognized",
            detail="aydin_denizli chars=9 digits=0 keywords=- remainder=no",
        ),
        unchecked_operators=("aydin_denizli",),
    )


def _provider(name: str, status: ProviderStatus, total: Decimal = Decimal(0)) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=name, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=total, penalty_amount=Decimal(0), total_amount=total,
        items=(), error_type=None, checked_at=_NOW,
    )


def _kgm_partial(*amounts: str) -> ProviderCheckResult:
    return _kgm_outcome_to_result(_partial_outcome(*amounts), checked_at=_NOW)


def _unified(*providers: ProviderCheckResult) -> UnifiedCheckResult:
    return UnifiedCheckResult(
        plate=_PLATE, started_at=_NOW, finished_at=_NOW,
        overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
        providers=providers,
    )


def _complete_run(kgm_debt: Decimal) -> UnifiedCheckResult:
    kgm_status = ProviderStatus.HAS_DEBT if kgm_debt > 0 else ProviderStatus.NO_DEBT
    return _unified(
        _provider("gib", ProviderStatus.NO_DEBT), _provider("avrasya", ProviderStatus.NO_DEBT),
        _provider("kgm", kgm_status, kgm_debt),
    )


def _partial_run(*amounts: str) -> UnifiedCheckResult:
    return _unified(
        _provider("gib", ProviderStatus.NO_DEBT), _provider("avrasya", ProviderStatus.NO_DEBT), _kgm_partial(*amounts),
    )


# ---- unified/models.py: кодирование непроверенных секций ----

def test_partial_error_type_round_trips_and_rejects_bad_keys():
    assert partial_error_type(["aydin_denizli"]) == _PARTIAL_ERROR_TYPE
    assert unchecked_sections_from_error_type("partial_unchecked:kgm,aydin_denizli") == ("kgm", "aydin_denizli")
    for error_type in (None, "", "unexpected", "transport_error", "partial_unchecked:"):
        assert unchecked_sections_from_error_type(error_type) == ()
    for bad in ((), ("Aydin Denizli",), ("x;y",)):
        with pytest.raises(ValueError):
            partial_error_type(bad)


def test_unchecked_sections_only_for_error_results():
    assert unchecked_sections(_kgm_partial("40")) == ("aydin_denizli",)
    assert unchecked_sections(_provider("kgm", ProviderStatus.HAS_DEBT, Decimal(80))) == ()


# ---- unified/check_service.py ----

def test_partial_outcome_becomes_error_result_with_checked_debts():
    result = _kgm_partial("40.00", "40.00")

    assert result.status == ProviderStatus.ERROR
    assert result.error_type == _PARTIAL_ERROR_TYPE
    assert result.debt_count == 2
    assert result.total_amount == Decimal("80.00")
    assert (result.principal_amount, result.penalty_amount) == (None, None)
    assert [item.amount for item in result.items] == [Decimal("40.00"), Decimal("40.00")]
    assert all(item.description for item in result.items)


def test_overall_status_is_partial_never_has_debt_or_no_debt():
    assert derive_overall_status(_partial_run("40").providers) == OverallStatus.PARTIAL
    assert derive_overall_status(_partial_run().providers) == OverallStatus.PARTIAL
    # Run без единого полного ответа остаётся ERROR (см. derive_overall_status).
    assert derive_overall_status((_kgm_partial("40"),)) == OverallStatus.ERROR


def test_partial_kgm_is_not_counted_in_the_overall_total():
    run = _unified(_provider("gib", ProviderStatus.HAS_DEBT, Decimal(150)), _kgm_partial("40", "40"))

    assert run.total_amount == Decimal(150)


class _Closeable:
    async def aclose(self) -> None:
        pass


class _Gib:
    async def start(self):
        return CaptchaChallenge(image_id="cid", image_png=b"GIB")

    async def submit(self, *, plate, image_id, captcha_code):
        return GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)


class _Avrasya:
    async def start(self):
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA")

    async def submit(self, *, plate, captcha_code):
        return AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None)


class _Kgm:
    def __init__(self, outcome: KgmSubmitOutcome):
        self._outcome = outcome
        self.submits = 0

    async def start(self):
        return KgmCaptchaChallenge(image_png=b"KGM")

    async def refresh_captcha(self):
        return KgmCaptchaChallenge(image_png=b"KGM")

    async def submit(self, *, plate, captcha_code):
        self.submits += 1
        return self._outcome


class _Resolver:
    async def resolve(self, *, provider, image_png):
        return "ABCDE"


async def test_unified_check_returns_partial_and_does_not_retry_kgm():
    kgm = _Kgm(_partial_outcome("40", "40"))
    service = UnifiedTurkeyCheckService(
        lambda: (_Closeable(), _Gib()), lambda: (_Closeable(), _Avrasya()), lambda: (_Closeable(), kgm),
        captcha_resolver=_Resolver(),
    )

    result = await service.check(_PLATE, max_attempts=5, mode="manual")

    assert result.overall_status == OverallStatus.PARTIAL
    assert result.total_amount == Decimal(0)
    kgm_result = result.provider_result("kgm")
    assert kgm_result.status == ProviderStatus.ERROR
    assert kgm_result.error_type == _PARTIAL_ERROR_TYPE
    assert kgm_result.total_amount == Decimal(80)
    assert kgm.submits == 1  # partial — не CAPTCHA-ошибка, повторной попытки нет


# ---- run_repository.py ----

def test_partial_kgm_round_trips_and_is_never_the_last_successful_result():
    runs = TurkeyCheckRunRepository(":memory:")
    runs.save(_complete_run(Decimal(3788)), telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT,
              initiator="manual")
    partial = _partial_run("40.00", "40.00")
    run_id = runs.save(partial, telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, initiator="manual")

    stored = runs.get_run(run_id)
    assert stored.overall_status == OverallStatus.PARTIAL
    stored_kgm = stored.provider_result("kgm")
    assert stored_kgm.error_type == _PARTIAL_ERROR_TYPE
    assert stored_kgm.items == partial.provider_result("kgm").items
    assert stored_kgm.total_amount == Decimal("80.00")
    assert unchecked_sections(stored_kgm) == ("aydin_denizli",)

    last_success = runs.get_last_successful_provider_result(_PLATE, "kgm")
    assert (last_success.status, last_success.total_amount) == (ProviderStatus.HAS_DEBT, Decimal(3788))
    last_attempt, last_ok = runs.get_latest_provider_results_for_plate(_PLATE, "kgm")
    assert last_attempt.result.error_type == _PARTIAL_ERROR_TYPE
    assert last_ok.result.total_amount == Decimal(3788)
    assert runs.get_last_successful_provider_result_for_subscriber(
        _PLATE, "kgm", telegram_user_id=_OWNER_ID,
    ).total_amount == Decimal(3788)


# ---- мониторинг и уведомления ----

class _ScriptedCheckService:
    def __init__(self, results):
        self._results = list(results)

    async def check(self, plate, **kwargs):
        return self._results.pop(0)


class _RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send(self, *, telegram_chat_id, text):
        self.sent.append((telegram_chat_id, text))


def _monitoring(results, baseline: UnifiedCheckResult):
    runs = TurkeyCheckRunRepository(":memory:")
    subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
    notifier = _RecordingNotifier()
    service = TurkeyMonitoringService(_ScriptedCheckService(results), runs, subscriptions, notifier)
    subscription = subscriptions.enable(telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, plate=_PLATE)
    runs.save(baseline, telegram_user_id=_OWNER_ID, telegram_chat_id=_OWNER_CHAT, initiator="monitoring_21:00")
    return service, runs, notifier, subscription


@pytest.mark.parametrize("found", [(), ("40",)], ids=["nothing-found", "less-than-known"])
async def test_partial_after_known_debt_never_notifies_resolution_or_change(found):
    service, runs, notifier, subscription = _monitoring(
        [_partial_run(*found), _complete_run(Decimal(3788))], baseline=_complete_run(Decimal(3788)),
    )

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")

    assert notifier.sent == []
    assert service._pending_retries == []
    baseline = runs.get_last_successful_provider_result_for_subscriber(_PLATE, "kgm", telegram_user_id=_OWNER_ID)
    assert (baseline.status, baseline.total_amount) == (ProviderStatus.HAS_DEBT, Decimal(3788))

    # Следующая полная проверка с тем же долгом сравнивается с прежним полным
    # результатом — "без изменений", ни "погашено", ни "новая задолженность".
    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")
    assert notifier.sent == []


async def test_partial_with_debt_in_checked_sections_is_not_reported_as_new_debt():
    """Консервативно: частичный ответ вообще не участвует в сравнении,
    уведомление придёт только после полной проверки."""
    service, _runs, notifier, subscription = _monitoring(
        [_partial_run("40", "40"), _complete_run(Decimal(80))], baseline=_complete_run(Decimal(0)),
    )

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")
    assert notifier.sent == []

    await service.check_now_and_notify_if_changed(subscription, initiator="21:00")
    assert len(notifier.sent) == 1
    assert "Новая задолженность" in notifier.sent[0][1]


# ---- список долгов (statistics_service.py + texts.py) ----

def _save_run(runs, result: UnifiedCheckResult, *, days_ago: int) -> None:
    run_id = runs.save(result, telegram_user_id=1, telegram_chat_id=1, initiator="manual")
    finished_at = (_NOW - timedelta(days=days_ago)).isoformat()
    runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
    runs._conn.commit()


def _statistics():
    runs = TurkeyCheckRunRepository(":memory:")
    service = TurkeyStatisticsService(
        TurkeyBotKnownUsersRepository(":memory:"), runs, TurkeyMonitoringSubscriptionRepository(":memory:"),
    )
    garage = TurkeyUserCarsRepository(":memory:")
    return service, runs, garage.add_car(telegram_user_id=1, car_number=_PLATE)


def test_debt_list_keeps_last_complete_kgm_debt_after_partial_check():
    service, runs, car = _statistics()
    _save_run(runs, _complete_run(Decimal(3788)), days_ago=6)
    _save_run(runs, _partial_run("40"), days_ago=1)

    rows = service.get_debt_rows([car])

    assert len(rows) == 1
    kgm = {state.provider: state for state in rows[0].provider_states}["kgm"]
    assert kgm.freshness == TurkeyProviderFreshness.STALE
    assert kgm.amount == Decimal(3788)
    assert kgm.last_attempt_error_type == _PARTIAL_ERROR_TYPE
    line = _format_debt_provider_state(kgm)
    assert line.startswith(f"⚠️ KGM: {_format_try_amount(Decimal(3788))} — последняя известная")
    assert "проверено частично, не проверено: Aydın-Denizli Otoyolu" in line
    assert "не ответил" not in line


def test_partial_check_alone_is_not_reliable_data():
    service, runs, car = _statistics()
    _save_run(runs, _partial_run(), days_ago=1)

    assert service.get_debt_rows([car]) == []
    assert service.count_plates_without_reliable_data([car]) == 1
    kgm = {state.provider: state for state in service._plate_provider_states(_PLATE)}["kgm"]
    assert kgm.freshness == TurkeyProviderFreshness.NO_DATA
    assert _format_debt_provider_state(kgm) == (
        f"❔ KGM: нет достоверных данных ({(_NOW - timedelta(days=1)).strftime('%d.%m.%Y')} "
        "проверено частично, не проверено: Aydın-Denizli Otoyolu)"
    )


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_manager_refresh_counts_partial_kgm_as_incomplete_and_keeps_known_debt():
    runs = TurkeyCheckRunRepository(":memory:")
    garage = TurkeyUserCarsRepository(":memory:")
    statistics = TurkeyStatisticsService(
        TurkeyBotKnownUsersRepository(":memory:"), runs, TurkeyMonitoringSubscriptionRepository(":memory:"),
    )
    garage.add_car(telegram_user_id=1, car_number=_PLATE)
    runs.save(_complete_run(Decimal(3788)), telegram_user_id=1, telegram_chat_id=1, initiator="manual")
    check_service = UnifiedTurkeyCheckService(
        lambda: (_Closeable(), _Gib()), lambda: (_Closeable(), _Avrasya()),
        lambda: (_Closeable(), _Kgm(_partial_outcome("40"))), captcha_resolver=_Resolver(),
    )
    refresh = TurkeyDebtRefreshService(garage, runs, check_service, statistics, sleep=_no_sleep)

    outcome = await refresh.refresh()

    assert (outcome.successful_plates, outcome.partial_plates, outcome.error_plates) == (0, 1, 0)
    assert outcome.partial_failed_providers == (("kgm",),)
    assert f"KGM — последняя успешная проверка {_NOW.strftime('%d.%m.%Y')}" in format_debt_refresh_summary(outcome)
    rows = refresh.list_candidates()
    assert len(rows) == 1
    kgm = {state.provider: state for state in rows[0].provider_states}["kgm"]
    assert (kgm.freshness, kgm.amount) == (TurkeyProviderFreshness.STALE, Decimal(3788))


# ---- texts.py: ответ пользователю ----

def test_unified_text_shows_checked_debts_and_unchecked_section():
    text = format_unified_check_result(_partial_run("40", "40"))

    assert "Итоговая сумма может быть неполной" in text
    assert (
        f"🛣 KGM (Платные дороги, включая тунели) — ⚠️ проверено частично: по проверенным дорогам "
        f"{_format_try_amount(Decimal(80))}; не проверено: Aydın-Denizli Otoyolu"
    ) in text
    assert "🛣 KGM (Платные дороги, включая тунели) (проверено частично, не проверено: Aydın-Denizli Otoyolu)" in text


def test_unified_text_never_says_kgm_has_no_debt_when_a_section_is_unchecked():
    text = format_unified_check_result(_partial_run())

    assert "по проверенным дорогам задолженностей нет; не проверено: Aydın-Denizli Otoyolu" in text
    assert "🛣 KGM (Платные дороги, включая тунели) — задолженностей нет" not in text
    assert "Итого:" not in text


def test_unchecked_kgm_operator_section_is_named_in_full():
    outcome = KgmSubmitOutcome(
        kind="partial", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        diagnostics=KgmDiagnostics(category="result_structure", code="section_label_unrecognized"),
        unchecked_operators=("kgm",),
    )
    run = _unified(_provider("gib", ProviderStatus.NO_DEBT), _kgm_outcome_to_result(outcome, checked_at=_NOW))

    text = format_unified_check_result(run)

    assert "не проверено: Karayolları Genel Müdürlüğü" in text
    assert "не проверено: KGM" not in text
