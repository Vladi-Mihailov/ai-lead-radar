"""Тесты UnifiedTurkeyCheckService (см. reader/turkey_bot/unified/
check_service.py) — GIB/Avrasya/KGM полностью независимы, ERROR одного
провайдера не должен ронять остальные и не эквивалентен NO_DEBT (см.
design report "Перестроить UX Turkey test bot" п.4/п.5/п.7). Никаких
реальных HTTP-запросов — только fake providers/captcha resolver."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from reader.turkey_bot.avrasya.models import (
    AvrasyaCaptchaChallenge,
    AvrasyaSubmitOutcome,
)
from reader.turkey_bot.avrasya.session import (
    AvrasyaTransportError,
)
from reader.turkey_bot.gib.models import (
    CaptchaChallenge,
    GibFineRecord,
    GibSubmitOutcome,
)
from reader.turkey_bot.gib.session import GibTransportError
from reader.turkey_bot.kgm.models import (
    KgmCaptchaChallenge,
    KgmDebtItem,
    KgmOperatorResult,
    KgmSubmitOutcome,
)
from reader.turkey_bot.kgm.session import KgmTransportError
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import OverallStatus, ProviderStatus

pytestmark = pytest.mark.asyncio


class _FakeCloseable:
    async def aclose(self) -> None:
        pass


class _FakeGibProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        return CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        return self._outcome


class _FakeAvrasyaProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

    async def submit(self, *, plate, captcha_code):
        return self._outcome


class _FakeKgmProvider:
    def __init__(self, *, start_exc=None, outcome=None):
        self._start_exc = start_exc
        self._outcome = outcome

    async def start(self):
        if self._start_exc:
            raise self._start_exc
        return KgmCaptchaChallenge(image_png=b"KGM-PNG")

    async def submit(self, *, plate, captcha_code):
        return self._outcome


class _FakeCaptchaResolver:
    """Возвращает фиксированный код для любого провайдера — никакого OCR."""

    async def resolve(self, *, provider, image_png):
        return "ABCDE"


def _make_service(*, gib_provider=None, avrasya_provider=None, kgm_provider=None):
    gib_provider = gib_provider or _FakeGibProvider(
        outcome=GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None),
    )
    avrasya_provider = avrasya_provider or _FakeAvrasyaProvider(
        outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
    )
    kgm_provider = kgm_provider or _FakeKgmProvider(
        outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ),
    )
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib_provider),
        lambda: (_FakeCloseable(), avrasya_provider),
        lambda: (_FakeCloseable(), kgm_provider),
        captcha_resolver=_FakeCaptchaResolver(),
    )


async def test_all_no_debt_gives_overall_no_debt():
    service = _make_service()
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.NO_DEBT
    assert result.total_amount == Decimal(0)
    assert all(p.status == ProviderStatus.NO_DEBT for p in result.providers)


async def test_gib_has_debt():
    gib_provider = _FakeGibProvider(outcome=GibSubmitOutcome(
        kind="has_debt", messages=(), raw_data=None,
        fines=(GibFineRecord(
            protocol_no="P1", plate="A123AA123", amount=Decimal(237), description="d",
            violation_date=None, authority=None, late_fee=None, discount=None,
        ),),
    ))
    service = _make_service(gib_provider=gib_provider)
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.HAS_DEBT
    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.HAS_DEBT
    assert gib.total_amount == Decimal(237)
    assert result.total_amount == Decimal(237)


async def test_avrasya_has_debt():
    from reader.turkey_bot.avrasya.models import AvrasyaDebtItem

    avrasya_provider = _FakeAvrasyaProvider(outcome=AvrasyaSubmitOutcome(
        kind="has_debt", status_code=200, messages=(), raw_data=None,
        debt_items=(AvrasyaDebtItem(
            principal_amount=Decimal(2580), total_amount=Decimal(2580), service_file_type="toll",
        ),),
    ))
    service = _make_service(avrasya_provider=avrasya_provider)
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.HAS_DEBT
    avrasya = result.provider_result("avrasya")
    assert avrasya.status == ProviderStatus.HAS_DEBT
    assert avrasya.total_amount == Decimal(2580)


async def test_kgm_has_debt():
    kgm_op = KgmOperatorResult(
        operator_key="kgm", operator_name="KGM",
        items=(KgmDebtItem(
            operator="kgm", date_time=datetime(2026, 9, 15, 9, 39, 48),  # noqa: DTZ001
            entry_station="HENDEK", exit_station="TOPAĞAÇ SGS", vehicle_class="1",
            base_toll=Decimal(2660), payable_amount=Decimal(2660),
            penalty_free_deadline=date(2026, 9, 30),
        ),),
        subtotal=Decimal(2660),
    )
    kgm_provider = _FakeKgmProvider(outcome=KgmSubmitOutcome(
        kind="has_debt", operators=(kgm_op,),
        kgm_total=Decimal(2660), yid_total=Decimal(0), grand_total=Decimal(2660),
    ))
    service = _make_service(kgm_provider=kgm_provider)
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.HAS_DEBT
    kgm = result.provider_result("kgm")
    assert kgm.status == ProviderStatus.HAS_DEBT
    assert kgm.total_amount == Decimal(2660)


async def test_one_provider_error_gives_partial_not_no_debt():
    gib_provider = _FakeGibProvider(start_exc=GibTransportError("boom"))
    service = _make_service(gib_provider=gib_provider)
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.PARTIAL
    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "transport_error"
    # PARTIAL никогда не должен молча стать NO_DEBT/HAS_DEBT.
    assert result.overall_status != OverallStatus.NO_DEBT


async def test_all_providers_error_gives_overall_error():
    service = _make_service(
        gib_provider=_FakeGibProvider(start_exc=GibTransportError("boom")),
        avrasya_provider=_FakeAvrasyaProvider(start_exc=AvrasyaTransportError("boom")),
        kgm_provider=_FakeKgmProvider(start_exc=KgmTransportError("boom")),
    )
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.ERROR
    assert result.total_amount == Decimal(0)
    assert all(p.status == ProviderStatus.ERROR for p in result.providers)


async def test_captcha_unavailable_is_error_not_no_debt():
    class _AlwaysFailingResolver:
        async def resolve(self, *, provider, image_png):
            return None

    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), _FakeGibProvider(outcome=GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None))),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None))),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0)))),
        captcha_resolver=_AlwaysFailingResolver(),
    )
    result = await service.check("A123AA123")

    assert result.overall_status == OverallStatus.ERROR
    assert all(p.error_type == "captcha_unavailable" for p in result.providers)


async def test_rejected_captcha_is_error_not_no_debt():
    gib_provider = _FakeGibProvider(outcome=GibSubmitOutcome(kind="rejected", messages=(), raw_data=None))
    service = _make_service(gib_provider=gib_provider)
    result = await service.check("A123AA123")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "captcha_rejected"


# ---- GIB Russian translation preservation (см. задачу "Перенос Unified
# Turkey функционала в production" п.4: READ-ONLY аудит обнаружил, что
# production ДО этого переноса переводил location/violation_description
# через TurkeyFineTranslationService, а test unified-flow эту фичу
# потерял — здесь она восстановлена через необязательный gib_translator,
# см. reader/turkey_bot/unified/check_service.py::_translate_gib_fines) ----

class _FakeTranslator:
    """FineTranslatorLike-фейк (см. reader/turkey_bot/gib/translation.py::
    TurkeyFineTranslationService.translate_fines) — без единого реального
    OpenAI-вызова."""

    def __init__(self, *, raises: Exception | None = None):
        self._raises = raises
        self.calls: list[tuple] = []

    async def translate_fines(self, fines):
        self.calls.append(fines)
        if self._raises is not None:
            raise self._raises
        return tuple(
            GibFineRecord(
                protocol_no=f.protocol_no, plate=f.plate, amount=f.amount, description=f.description,
                violation_date=f.violation_date, authority=f.authority, late_fee=f.late_fee,
                discount=f.discount, location=f.location, violation_description=f.violation_description,
                location_ru="Стамбул", violation_description_ru="Превышение скорости",
            )
            for f in fines
        )


def _gib_has_debt_outcome() -> GibSubmitOutcome:
    return GibSubmitOutcome(
        kind="has_debt", messages=(), raw_data=None,
        fines=(GibFineRecord(
            protocol_no="P1", plate="A123AA123", amount=Decimal(237), description="d",
            violation_date=None, authority=None, late_fee=None, discount=None,
            location="İstanbul", violation_description="Hız sınırı ihlali",
        ),),
    )


async def test_gib_translator_populates_russian_debt_item_description():
    translator = _FakeTranslator()
    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), _FakeGibProvider(outcome=_gib_has_debt_outcome())),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(
            outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        )),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=_FakeCaptchaResolver(),
        gib_translator=translator,
    )

    result = await service.check("A123AA123")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.HAS_DEBT
    assert len(gib.items) == 1
    assert "Стамбул" in gib.items[0].description
    assert "Превышение скорости" in gib.items[0].description
    assert len(translator.calls) == 1


async def test_no_translator_falls_back_to_original_turkish_text():
    """gib_translator=None (default) — та же реализация, что и раньше
    (см. задачу: не превращать это в ошибку/пустой текст)."""
    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), _FakeGibProvider(outcome=_gib_has_debt_outcome())),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(
            outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        )),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=_FakeCaptchaResolver(),
    )

    result = await service.check("A123AA123")

    gib = result.provider_result("gib")
    assert "İstanbul" in gib.items[0].description
    assert "Hız sınırı ihlali" in gib.items[0].description


async def test_translation_failure_is_fail_open_shows_original_text():
    """Явное требование задачи (то же, что и у production conversation.py::
    _translate_fines): сбой перевода НИКОГДА не должен превращать успешную
    проверку в ошибку — check() обязан вернуть HAS_DEBT с исходным
    (турецким) текстом, не ERROR/исключение."""
    from reader.turkey_bot.gib.translation import FineTranslationError

    translator = _FakeTranslator(raises=FineTranslationError("network down"))
    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), _FakeGibProvider(outcome=_gib_has_debt_outcome())),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(
            outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        )),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=_FakeCaptchaResolver(),
        gib_translator=translator,
    )

    result = await service.check("A123AA123")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.HAS_DEBT
    assert "İstanbul" in gib.items[0].description


async def test_translator_only_called_for_has_debt_not_no_debt():
    """Тот же принцип, что и у production _translate_fines — нечего
    переводить для no_debt (fines всегда пустой)."""
    translator = _FakeTranslator()
    service = _make_service()
    service._gib_translator = translator

    await service.check("A123AA123")

    assert translator.calls == []


# ---- MANUAL retry orchestration (см. задачу "Retry orchestration
# Unified Turkey checks" п.1/п.7) — до 35 попыток на провайдера при
# captcha_unavailable/captcha_rejected, НОВАЯ captcha через
# provider.refresh_captcha() между попытками, успех прекращает retry
# немедленно, успешный провайдер не повторяется. Никаких реальных HTTP/
# CAPTCHA — только детерминированные фейки, отслеживающие число вызовов. ----

_GIB_NO_DEBT = GibSubmitOutcome(kind="no_debt", messages=(), raw_data=None)
_GIB_REJECTED = GibSubmitOutcome(kind="rejected", messages=(), raw_data=None)


class _SequencedGibProvider:
    """submit() возвращает следующий outcome из заранее заданной
    последовательности при каждом вызове — start_calls/refresh_calls/
    submit_calls считают реальные вызовы (см. задачу п.7: "success attempt
    N -> ровно N calls")."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.start_calls = 0
        self.refresh_calls = 0
        self.submit_calls = 0

    async def start(self):
        self.start_calls += 1
        return CaptchaChallenge(image_id=f"cid-{self.start_calls}", image_png=b"GIB-PNG")

    async def refresh_captcha(self):
        self.refresh_calls += 1
        return CaptchaChallenge(image_id=f"cid-r{self.refresh_calls}", image_png=b"GIB-PNG")

    async def submit(self, *, plate, image_id, captcha_code):
        self.submit_calls += 1
        return self._outcomes.pop(0)


class _SequencedResolver:
    """Возвращает следующий код (или None -> captcha_unavailable) из
    заранее заданной последовательности при каждом вызове resolve() —
    ТОЛЬКО для `only_provider` (по умолчанию "gib"): avrasya/kgm в этих
    тестах используют default-фейки, которые ТОЖЕ дёргают
    captcha_resolver (один общий resolver на весь UnifiedTurkeyCheckService,
    см. check_service.py) — без этого фильтра их вызовы съедали бы
    элементы последовательности, предназначенной только для GIB."""

    def __init__(self, codes, *, only_provider="gib"):
        self._codes = list(codes)
        self._only_provider = only_provider
        self.calls = 0

    async def resolve(self, *, provider, image_png):
        if provider != self._only_provider:
            return "OTHERCODE"
        self.calls += 1
        return self._codes.pop(0)


def _service_with_gib(provider, resolver=None) -> UnifiedTurkeyCheckService:
    return UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), provider),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(
            outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        )),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=resolver or _FakeCaptchaResolver(),
    )


async def test_manual_success_on_first_attempt_makes_exactly_one_call():
    provider = _SequencedGibProvider([_GIB_NO_DEBT])
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert provider.start_calls == 1
    assert provider.refresh_calls == 0
    assert provider.submit_calls == 1


async def test_manual_success_on_attempt_seven_makes_exactly_seven_calls():
    provider = _SequencedGibProvider([_GIB_REJECTED] * 6 + [_GIB_NO_DEBT])
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert provider.submit_calls == 7
    assert provider.start_calls == 1
    assert provider.refresh_calls == 6


async def test_manual_success_on_attempt_thirty_five_makes_exactly_35_calls():
    provider = _SequencedGibProvider([_GIB_REJECTED] * 34 + [_GIB_NO_DEBT])
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert provider.submit_calls == 35


async def test_manual_all_35_attempts_fail_gives_error_after_exactly_35():
    provider = _SequencedGibProvider([_GIB_REJECTED] * 35)
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "captcha_rejected"
    assert provider.submit_calls == 35
    # 36-я попытка НЕ должна была произойти.
    assert provider.refresh_calls == 34


# ---- SCHEDULED boundary (max_attempts=25, см. задачу п.2/п.7) — тот же
# механизм retry, что и manual, только с другим лимитом попыток. Оркестрация
# +5-минутного повторного прохода — отдельно, см.
# tests/test_turkey_bot_monitoring_retry.py. ----

async def test_scheduled_success_on_first_attempt_makes_exactly_one_call():
    provider = _SequencedGibProvider([_GIB_NO_DEBT])
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=25, mode="scheduled")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert provider.submit_calls == 1


async def test_scheduled_success_on_attempt_25_makes_exactly_25_calls():
    provider = _SequencedGibProvider([_GIB_REJECTED] * 24 + [_GIB_NO_DEBT])
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=25, mode="scheduled")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert provider.submit_calls == 25


async def test_scheduled_all_25_attempts_fail_gives_error():
    provider = _SequencedGibProvider([_GIB_REJECTED] * 25)
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=25, mode="scheduled")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "captcha_rejected"
    assert provider.submit_calls == 25


async def test_manual_captcha_unavailable_retries_and_succeeds():
    """Тот же лимит/логика, но через captcha_unavailable (резолвер не
    вернул код) вместо captcha_rejected (сервер отклонил код)."""
    resolver = _SequencedResolver([None, None, None, "CODE1"])
    provider = _SequencedGibProvider([_GIB_NO_DEBT])
    service = _service_with_gib(provider, resolver)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert resolver.calls == 4
    assert provider.submit_calls == 1  # submit только на успешной попытке
    assert provider.start_calls == 1
    assert provider.refresh_calls == 3


async def test_manual_all_captcha_unavailable_gives_error_after_35():
    resolver = _SequencedResolver([None] * 35)
    provider = _SequencedGibProvider([])
    service = _service_with_gib(provider, resolver)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "captcha_unavailable"
    assert resolver.calls == 35
    assert provider.submit_calls == 0


async def test_manual_successful_provider_is_not_retried_when_another_provider_needs_retries():
    """Явное требование задачи п.5/п.7: "не повторять уже успешно
    завершённый provider" — GIB успевает с первой попытки, пока Avrasya
    несколько раз проваливается; GIB не должен получить ни одного лишнего
    вызова."""
    gib_provider = _SequencedGibProvider([_GIB_NO_DEBT])

    class _FailingThenSucceedingAvrasyaProvider:
        def __init__(self):
            self.start_calls = 0
            self.refresh_calls = 0
            self.submit_calls = 0
            self._outcomes = [
                AvrasyaSubmitOutcome(kind="rejected", status_code=200, messages=(), raw_data=None),
                AvrasyaSubmitOutcome(kind="rejected", status_code=200, messages=(), raw_data=None),
                AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
            ]

        async def start(self):
            self.start_calls += 1
            return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

        async def refresh_captcha(self):
            self.refresh_calls += 1
            return AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG")

        async def submit(self, *, plate, captcha_code):
            self.submit_calls += 1
            return self._outcomes.pop(0)

    avrasya_provider = _FailingThenSucceedingAvrasyaProvider()
    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), gib_provider),
        lambda: (_FakeCloseable(), avrasya_provider),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=_FakeCaptchaResolver(),
    )

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    assert result.provider_result("gib").status == ProviderStatus.NO_DEBT
    assert result.provider_result("avrasya").status == ProviderStatus.NO_DEBT
    assert gib_provider.submit_calls == 1  # GIB НЕ повторялся из-за Avrasya
    assert avrasya_provider.submit_calls == 3


async def test_transport_error_is_never_retried_even_with_high_max_attempts():
    """Явное требование задачи (не менять область retry) — retry
    существует ТОЛЬКО для captcha_unavailable/captcha_rejected,
    transport_error остаётся терминальным с первой попытки."""
    provider = _SequencedGibProvider([])
    provider.start = _raise(GibTransportError("boom"))  # type: ignore[method-assign]
    service = _service_with_gib(provider)

    result = await service.check("A123AA123", max_attempts=35, mode="manual")

    gib = result.provider_result("gib")
    assert gib.status == ProviderStatus.ERROR
    assert gib.error_type == "transport_error"


def _raise(exc):
    async def _inner():
        raise exc
    return _inner


async def test_rendered_unified_result_contains_translated_text():
    """Полный regression-тест сквозь texts.py::format_unified_check_result
    (см. задачу п.4: "unified production flow по-прежнему отдаёт
    пользователю переведённые данные") — не только DebtItem.description,
    но и итоговый текст, который реально увидит пользователь."""
    from reader.turkey_bot import texts

    translator = _FakeTranslator()
    service = UnifiedTurkeyCheckService(
        lambda: (_FakeCloseable(), _FakeGibProvider(outcome=_gib_has_debt_outcome())),
        lambda: (_FakeCloseable(), _FakeAvrasyaProvider(
            outcome=AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        )),
        lambda: (_FakeCloseable(), _FakeKgmProvider(outcome=KgmSubmitOutcome(
            kind="no_debt", kgm_total=Decimal(0), yid_total=Decimal(0), grand_total=Decimal(0),
        ))),
        captcha_resolver=_FakeCaptchaResolver(),
        gib_translator=translator,
    )

    result = await service.check("A123AA123")
    rendered = texts.format_unified_check_result(result)

    assert "Стамбул" in rendered
    assert "Превышение скорости" in rendered
    assert "İstanbul" not in rendered
    assert "Hız sınırı ihlali" not in rendered
