"""UnifiedTurkeyCheckService — единственная точка входа для проверки
номера через ВСЕ три провайдера (GIB/Avrasya/KGM), используется ОДИНАКОВО
и для ручной "🔎 Проверить сейчас" (reader/turkey_bot/conversation.py),
и для планового мониторинга 13:00/21:00 (reader/turkey_bot/monitoring/
monitoring_service.py) — перенесено из reader/turkey_bot_test/unified/
check_service.py (см. задачу "Перенос Unified Turkey функционала в
production"): GİB/Avrasya/KGM providers НЕ дублируются — импортируются
из reader/turkey_bot/{gib,avrasya,kgm}/*, которые уже байт-в-байт
идентичны своим test-аналогам (см. READ-ONLY аудит).

Провайдеры полностью независимы (asyncio.gather(..., return_exceptions=True))
— ошибка/исключение одного НЕ мешает остальным.

CAPTCHA — см. captcha_resolver.py: САМ резолвер (OCR) НЕ меняется и НЕ
улучшается (см. задачу "Retry orchestration Unified Turkey checks" п.6:
"не реализовывать автоматическое многократное решение/обход CAPTCHA") —
это по-прежнему РОВНО одна попытка распознавания НА КАЖДУЮ отдельную
captcha-картинку. Что ДОБАВЛЕНО здесь — ОРКЕСТРАЦИЯ повторов: если ОДНА
попытка (получить challenge -> распознать -> submit) заканчивается
captcha_unavailable (OCR не смог прочитать) ИЛИ captcha_rejected (сервер
провайдера отклонил код), вызывающий код (conversation.py — 35 попыток
для ручной проверки, monitoring/monitoring_service.py — 25 попыток для
планового мониторинга) может попросить ЗАНОВО: НОВУЮ captcha-картинку
через provider.refresh_captcha() (та же существующая сессия/client, см.
GibProvider/AvrasyaProvider/KgmProvider — эти классы НЕ менялись) и НОВУЮ
попытку резолвера на эту новую картинку — то есть каждая попытка
по-прежнему проходит ТОЛЬКО через уже существующий разрешённый
resolver/input, просто может повториться до max_attempts раз. Успех
(HAS_DEBT/NO_DEBT/unexpected) или НЕ-captcha ошибка (transport_error/
rate_limited) немедленно прекращает retry этого провайдера — retry
существует ИСКЛЮЧИТЕЛЬНО для captcha_unavailable/captcha_rejected.
Каждый логический check (один вызов check()) по-прежнему пишет РОВНО
один ProviderCheckResult на провайдера — попытки НЕ создают
промежуточных строк в БД (это отвечает вызывающий код, не этот файл).

ОТЛИЧИЕ от test-версии — сохранение регрессии, найденной READ-ONLY
аудитом (см. задачу п.4): production ДО этого переноса переводил
location/violation_description GIB-штрафов на русский через
TurkeyFineTranslationService (reader/turkey_bot/conversation.py::
_translate_fines). Test unified-flow этот вызов никогда не делал.
Здесь — необязательный `gib_translator` (см. GibFineTranslatorLike
ниже, тот же Protocol-приём, что и FineTranslatorLike в
reader/turkey_bot/conversation.py) вызывается ТОЛЬКО для has_debt,
ТОЛЬКО что полученного (raw) GibSubmitOutcome.fines, ДО построения
DebtItem — fail-open (см. _check_gib): сбой перевода никогда не
превращает успешную проверку в ошибку, просто показывает турецкий
оригинал (тот же принцип, что и в production conversation.py)."""

from __future__ import annotations

import asyncio
import gc
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import httpx

from reader.turkey_bot.avrasya.models import AvrasyaDebtItem, AvrasyaSubmitOutcome
from reader.turkey_bot.avrasya.provider import AvrasyaProvider
from reader.turkey_bot.avrasya.session import (
    AvrasyaRateLimitedError,
    AvrasyaSession,
    AvrasyaTransportError,
)
from reader.turkey_bot.gib.models import GibFineRecord, GibSubmitOutcome
from reader.turkey_bot.gib.provider import GibProvider
from reader.turkey_bot.gib.session import GibSession, GibTransportError
from reader.turkey_bot.gib.translation import FineTranslationError
from reader.turkey_bot.kgm.models import KgmDebtItem, KgmSubmitOutcome
from reader.turkey_bot.kgm.provider import KgmProvider
from reader.turkey_bot.kgm.session import KgmSession, KgmTransportError
from reader.turkey_bot.unified.captcha_resolver import (
    CaptchaResolver,
    DefaultCaptchaResolver,
)
from reader.turkey_bot.unified.models import (
    DebtItem,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)

logger = logging.getLogger(__name__)

_ALL_PROVIDERS: tuple[str, ...] = ("gib", "avrasya", "kgm")

# Единственные два исхода, которые вообще запускают retry (см. модуль
# docstring) — НЕ transport_error/rate_limited/unexpected: те остаются
# терминальными с первой попытки, как и раньше (см. задачу: retry только
# "после captcha_unavailable / captcha_rejected").
_RETRYABLE_ERROR_TYPES = frozenset({"captcha_unavailable", "captcha_rejected"})

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_REQUEST_TIMEOUT_SECONDS = 30.0


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT})


def default_gib_check_factory() -> tuple[httpx.AsyncClient, GibProvider]:
    client = _build_client()
    return client, GibProvider(GibSession(client))


def default_avrasya_check_factory() -> tuple[httpx.AsyncClient, AvrasyaProvider]:
    client = _build_client()
    return client, AvrasyaProvider(AvrasyaSession(client))


def default_kgm_check_factory() -> tuple[httpx.AsyncClient, KgmProvider]:
    client = _build_client()
    return client, KgmProvider(KgmSession(client))


class _AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


GibCheckFactory = Callable[[], tuple[_AsyncCloseable, GibProvider]]
AvrasyaCheckFactory = Callable[[], tuple[_AsyncCloseable, AvrasyaProvider]]
KgmCheckFactory = Callable[[], tuple[_AsyncCloseable, KgmProvider]]


class GibFineTranslatorLike(Protocol):
    """Ровно то, что нужно отсюда от TurkeyFineTranslationService (см.
    reader/turkey_bot/gib/translation.py) — тот же Protocol-приём, что и
    FineTranslatorLike в reader/turkey_bot/conversation.py (сознательно
    НЕ импортируется оттуда: unified/check_service.py не должен зависеть
    от conversation.py, во избежание циклического импорта — conversation.py
    сам импортирует check_service.py)."""

    async def translate_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log_attempt(
    plate: str, provider: str, mode: str, attempt: int, max_attempts: int, result: str,
) -> None:
    """Observability на КАЖДУЮ попытку (см. задачу "Retry orchestration
    Unified Turkey checks" п.6) — ТОЛЬКО plate/provider/mode/attempt/
    max_attempts/result, НИКОГДА captcha-код, изображение или
    токены/секреты (их здесь физически нет ни в одном параметре)."""
    logger.info(
        "Turkey unified check attempt: plate=%s provider=%s mode=%s attempt=%d/%d result=%s",
        plate, provider, mode, attempt, max_attempts, result,
    )


def _log_finished(plate: str, provider: str, mode: str, attempts_used: int, final_status: str) -> None:
    logger.info(
        "Turkey unified check finished: plate=%s provider=%s mode=%s attempts_used=%d final_status=%s",
        plate, provider, mode, attempts_used, final_status,
    )


def _collect_after_check(plate: str) -> None:
    """См. задачу "reclaim Turkey OCR memory after checks" — offline A/B
    (см. отчёт) установил: ни onnxruntime, ни OpenCV сами по себе не
    удерживают память (изолированные тесты — рост RSS ~0 на 50 вызовов
    каждый), а полный RapidOCR pipeline (новые тензоры + result-объекты
    на каждый CAPTCHA-вызов) накапливает исключительно GC-collectible
    память — Python-циклы, до которых обычный generational GC доходит
    только на редких полных (gen2) проходах. Один explicit gc.collect()
    сразу после ОДНОГО логического check() надёжно возвращает RSS почти
    к baseline (см. отчёт: 822 MB -> 139 MB в offline-тесте). НЕ
    вызывается внутри retry-цикла/на каждую отдельную CAPTCHA-попытку —
    один логический check = один collect."""
    collected = gc.collect()
    logger.debug("Turkey unified check GC collected objects=%d (plate=%s)", collected, plate)


def _error_result(provider: str, error_type: str, *, checked_at: datetime) -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=ProviderStatus.ERROR, debt_count=0,
        principal_amount=None, penalty_amount=None, total_amount=Decimal(0),
        items=(), error_type=error_type, checked_at=checked_at,
    )


def _format_try_amount(amount: Decimal) -> str:
    """Тот же формат, что и reader/turkey_bot/texts.py::_format_try_amount
    (не импортирую оттуда напрямую, чтобы unified/ — business logic — не
    зависел от texts.py — presentation layer; тот же приём, что и
    reader/turkey_bot/monitoring/notification_texts.py::_format_try_amount:
    "алгоритм намеренно идентичен, единственный источник истины для
    формата сумм в проекте")."""
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}".replace(",", " ") + " ₺"
    formatted = f"{quantized:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} ₺"


def _gib_fine_description(fine: GibFineRecord) -> str | None:
    """Собирает ЧЕЛОВЕКОЧИТАЕМУЮ многострочную детализацию ОДНОГО штрафа
    для DebtItem.description (см. задачу "Улучшить формат unified Turkey
    check" — "использовать структурированные date/time/location/amount...
    если поля нет — ничего не выдумывать") — используются ТОЛЬКО реально
    присутствующие поля GibFineRecord, каждое — своя строка, ничего не
    показывается, если поле отсутствует. *_ru поля — КОГДА заполнены (см.
    reader/turkey_bot/gib/translation.py::translate_fines), иначе исходный
    турецкий текст (тот же fallback "location_ru or location", что и в
    reader/turkey_bot/texts.py::_format_fine_block — сохраняет production
    перевод, найденный READ-ONLY аудитом как regression относительно
    test unified-flow)."""
    lines: list[str] = []
    if fine.violation_date is not None:
        lines.append(fine.violation_date.strftime("%d.%m.%Y"))
    location = fine.location_ru or fine.location
    if location:
        lines.append(location)
    violation = fine.violation_description_ru or fine.violation_description or fine.description
    if violation:
        lines.append(violation)
    if fine.amount is not None:
        lines.append(f"Сумма: {_format_try_amount(fine.amount)}")
    if fine.late_fee is not None and fine.late_fee > 0:
        lines.append(f"Пеня: {_format_try_amount(fine.late_fee)}")
    if fine.discount is not None and fine.discount > 0:
        lines.append(f"Скидка: -{_format_try_amount(fine.discount)}")
    if fine.protocol_no:
        lines.append(f"№ {fine.protocol_no}")
    return "\n".join(lines) if lines else None


def _avrasya_item_description(item: AvrasyaDebtItem) -> str:
    """См. _gib_fine_description — те же принципы, но AvrasyaDebtItem
    структурно НЕ несёт ни даты, ни маршрута (см. reader/turkey_bot/
    avrasya/models.py::AvrasyaDebtItem — этих полей там физически нет,
    не только "не показаны вживую"), поэтому детализация ограничена тем,
    что реально есть: service_file_type + суммы."""
    lines = [item.service_file_type, f"Стоимость: {_format_try_amount(item.principal_amount)}"]
    if item.penalty_amount is not None and item.penalty_amount > 0:
        lines.append(f"Начислено: {_format_try_amount(item.penalty_amount)}")
    lines.append(f"К оплате: {_format_try_amount(item.total_amount)}")
    return "\n".join(lines)


def _kgm_item_description(item: KgmDebtItem) -> str:
    """Тот же набор полей/формат, что и уже существующий
    reader/turkey_bot/texts.py::_format_kgm_item_block (legacy
    provider-specific рендер) — здесь ПРОДУБЛИРОВАН (не импортирован) по
    той же причине, что и _format_try_amount выше: unified/ не должен
    зависеть от texts.py. date_time/entry_station/exit_station/
    penalty_free_deadline пропускаются, если их нет вовсе (см.
    reader/turkey_bot/kgm/models.py::KgmDebtItem — None означает "буквально
    пусто на сайте", не ошибку разбора)."""
    lines = [item.date_time.strftime("%d.%m.%Y %H:%M")]
    if item.entry_station and item.exit_station:
        lines.append(f"{item.entry_station} → {item.exit_station}")
    elif item.exit_station:
        lines.append(item.exit_station)
    elif item.entry_station:
        lines.append(item.entry_station)
    lines.append(f"Стоимость: {_format_try_amount(item.base_toll)}")
    lines.append(f"К оплате: {_format_try_amount(item.payable_amount)}")
    if item.penalty_free_deadline is not None:
        lines.append(f"Без штрафа до: {item.penalty_free_deadline.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def _gib_outcome_to_result(outcome: GibSubmitOutcome, *, checked_at: datetime) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="gib", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        # Та же формула, что и texts.py::format_has_debt_messages (сумма
        # amount + late_fee - discount по каждому штрафу) — не изобретаю
        # новую математику для той же величины.
        principal = Decimal(0)
        penalty = Decimal(0)
        total = Decimal(0)
        items = []
        for fine in outcome.fines:
            if fine.amount is not None:
                principal += fine.amount
                total += fine.amount
            if fine.late_fee is not None:
                penalty += fine.late_fee
                total += fine.late_fee
            if fine.discount is not None:
                total -= fine.discount
            items.append(DebtItem(
                reference=fine.protocol_no, amount=fine.amount or Decimal(0),
                description=_gib_fine_description(fine),
            ))
        return ProviderCheckResult(
            provider="gib", status=ProviderStatus.HAS_DEBT, debt_count=len(outcome.fines),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=tuple(items), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("gib", "captcha_rejected", checked_at=checked_at)
    return _error_result("gib", "unexpected", checked_at=checked_at)


def _avrasya_outcome_to_result(
    outcome: AvrasyaSubmitOutcome, *, checked_at: datetime,
) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="avrasya", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        # Та же формула, что и texts.py::format_avrasya_has_debt_message.
        principal = sum((item.principal_amount for item in outcome.debt_items), Decimal(0))
        penalty = sum(
            (item.penalty_amount for item in outcome.debt_items if item.penalty_amount is not None),
            Decimal(0),
        )
        total = sum((item.total_amount for item in outcome.debt_items), Decimal(0))
        # AvrasyaDebtItem не несёт стабильного id — reference всегда None,
        # item-level diff для Avrasya невозможен.
        items = tuple(
            DebtItem(reference=None, amount=item.total_amount, description=_avrasya_item_description(item))
            for item in outcome.debt_items
        )
        return ProviderCheckResult(
            provider="avrasya", status=ProviderStatus.HAS_DEBT, debt_count=len(outcome.debt_items),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=items, error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("avrasya", "captcha_rejected", checked_at=checked_at)
    return _error_result("avrasya", "unexpected", checked_at=checked_at)


def _kgm_outcome_to_result(outcome: KgmSubmitOutcome, *, checked_at: datetime) -> ProviderCheckResult:
    if outcome.kind == "no_debt":
        return ProviderCheckResult(
            provider="kgm", status=ProviderStatus.NO_DEBT, debt_count=0,
            principal_amount=Decimal(0), penalty_amount=Decimal(0), total_amount=Decimal(0),
            items=(), error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "has_debt":
        all_items = [item for operator in outcome.operators for item in operator.items]
        principal = sum((item.base_toll for item in all_items), Decimal(0))
        total = outcome.grand_total if outcome.grand_total is not None else Decimal(0)
        penalty = total - principal if total > principal else Decimal(0)
        # KgmDebtItem не несёт стабильного id — reference всегда None.
        items = tuple(
            DebtItem(reference=None, amount=item.payable_amount, description=_kgm_item_description(item))
            for operator in outcome.operators
            for item in operator.items
        )
        return ProviderCheckResult(
            provider="kgm", status=ProviderStatus.HAS_DEBT, debt_count=len(all_items),
            principal_amount=principal, penalty_amount=penalty, total_amount=total,
            items=items, error_type=None, checked_at=checked_at,
        )
    if outcome.kind == "rejected":
        return _error_result("kgm", "captcha_rejected", checked_at=checked_at)
    return _error_result("kgm", "unexpected", checked_at=checked_at)


class UnifiedTurkeyCheckService:
    def __init__(
        self,
        gib_check_factory: GibCheckFactory,
        avrasya_check_factory: AvrasyaCheckFactory,
        kgm_check_factory: KgmCheckFactory,
        *,
        captcha_resolver: CaptchaResolver | None = None,
        gib_translator: GibFineTranslatorLike | None = None,
    ):
        self._gib_check_factory = gib_check_factory
        self._avrasya_check_factory = avrasya_check_factory
        self._kgm_check_factory = kgm_check_factory
        self._captcha_resolver = captcha_resolver or DefaultCaptchaResolver()
        self._gib_translator = gib_translator
        # См. задачу "reduce Turkey OCR memory pressure" — READ-ONLY
        # диагностика production OOM установила: manual check, plановый
        # monitoring и manager mass refresh все делят ОДИН и тот же
        # UnifiedTurkeyCheckService instance (см. reader/turkey_bot/
        # main.py) БЕЗ какого-либо lock'а между ними — два разных
        # автомобиля могли одновременно гонять до 35 CAPTCHA-попыток ×
        # 3 провайдера каждый через общий process-wide RapidOCR singleton
        # (см. captcha_solver.py), что могло удваивать/утраивать peak
        # memory. asyncio.Lock (НЕ threading.Lock — весь стек асинхронный,
        # один event loop) сериализует ЦЕЛИКОМ один логический check()
        # (GİB+Avrasya+KGM от начала до конца) относительно ЛЮБОГО другого
        # check() ТОГО ЖЕ instance — существующий asyncio.gather ТРЁХ
        # провайдеров ОДНОГО check() ниже не меняется вовсе (см. докстрок
        # check()). Ни один caller не держит этот lock и рекурсивно не
        # вызывает check() изнутри себя (см. reader/turkey_bot/
        # conversation.py::_run_manual_check, debt_refresh_service.py::
        # refresh(), monitoring/monitoring_service.py — все просто
        # await self._check_service.check(...) один раз за итерацию, без
        # вложенности) — deadlock исключён по построению.
        self._check_lock = asyncio.Lock()

    async def check(
        self,
        plate: str,
        *,
        max_attempts: int = 1,
        mode: str = "manual",
        providers: tuple[str, ...] | None = None,
    ) -> UnifiedCheckResult:
        """max_attempts — сколько раз ПОВТОРИТЬ (получить новую captcha +
        новую попытку резолвера) один провайдер, если он упирается в
        captcha_unavailable/captcha_rejected (см. модуль docstring) —
        default=1 сохраняет старое поведение (ровно одна попытка, без
        retry) для любого вызывающего кода, который явно не запрашивает
        retry. `mode` — ТОЛЬКО для observability-логов ("manual"/
        "scheduled"/"scheduled_retry"), не меняет логику. `providers` —
        None означает ВСЕ три (обычный полный check); подмножество — для
        scheduled retry ЧЕРЕЗ 5 минут, который должен проверять ТОЛЬКО
        провайдеров, не завершившихся успешно в первом проходе (см.
        reader/turkey_bot/monitoring/monitoring_service.py) — успешные
        провайдеры повторно НЕ запрашиваются вообще, ни как HTTP-запрос,
        ни как отдельная строка в UnifiedCheckResult.providers.

        Весь метод целиком сериализован через self._check_lock (см.
        __init__ докстрок про "reduce Turkey OCR memory pressure") — в
        один момент времени в этом процессе может выполняться только ОДИН
        check() (одного автомобиля), но provider'ы ВНУТРИ этого одного
        check() по-прежнему запускаются параллельно через
        asyncio.gather() ниже, без изменений.

        См. задачу "reclaim Turkey OCR memory after checks" — finally
        гарантирует ОДИН explicit gc.collect() на success/provider-error/
        partial-result путях И при отмене (CancelledError, например через
        внешний asyncio.wait_for) — gc.collect() синхронный и ничего не
        await'ит, поэтому безопасен на любом из этих путей. Результат уже
        полностью построен и возвращён (return вычисляется ДО выполнения
        finally) — gc.collect() не может инвалидировать то, что уже
        возвращается вызывающему коду."""
        try:
            async with self._check_lock:
                started_at = _now()
                provider_names = providers if providers is not None else _ALL_PROVIDERS
                checkers = {
                    "gib": self._check_gib,
                    "avrasya": self._check_avrasya,
                    "kgm": self._check_kgm,
                }
                results = await asyncio.gather(
                    *(checkers[name](plate, max_attempts=max_attempts, mode=mode) for name in provider_names),
                    return_exceptions=True,
                )
                providers_result = tuple(
                    result if isinstance(result, ProviderCheckResult)
                    else _error_result(provider_name, "internal_error", checked_at=_now())
                    for provider_name, result in zip(provider_names, results)
                )
                for provider_name, result in zip(provider_names, results):
                    if isinstance(result, Exception):
                        logger.exception(
                            "Turkey unified check: непойманное исключение в провайдере %s (plate=%s)",
                            provider_name, plate, exc_info=result,
                        )
                finished_at = _now()
                overall_status = derive_overall_status(providers_result)
                return UnifiedCheckResult(
                    plate=plate, started_at=started_at, finished_at=finished_at,
                    overall_status=overall_status, total_amount=total_amount_for(providers_result),
                    providers=providers_result,
                )
        finally:
            _collect_after_check(plate)

    async def _translate_gib_fines(
        self, fines: tuple[GibFineRecord, ...],
    ) -> tuple[GibFineRecord, ...]:
        """Fail-open (см. reader/turkey_bot/conversation.py::
        _translate_fines — идентичный принцип): нет translator'а ИЛИ сбой
        перевода -> исходные (турецкие) fines без изменений, НИКОГДА не
        превращает успешную проверку в ошибку. Логируется только факт
        сбоя, НИКОГДА исходный/переведённый текст."""
        if self._gib_translator is None:
            return fines
        try:
            return await self._gib_translator.translate_fines(fines)
        except FineTranslationError:
            logger.warning(
                "Turkey unified GIB: перевод недоступен, показываю оригинальный турецкий текст",
            )
            return fines

    async def _check_gib(
        self, plate: str, *, max_attempts: int, mode: str,
    ) -> ProviderCheckResult:
        client, provider = self._gib_check_factory()
        try:
            last_error_type = "captcha_unavailable"
            for attempt in range(1, max_attempts + 1):
                try:
                    challenge = await provider.start() if attempt == 1 else await provider.refresh_captcha()
                except GibTransportError:
                    _log_attempt(plate, "gib", mode, attempt, max_attempts, "transport_error")
                    return _error_result("gib", "transport_error", checked_at=_now())

                captcha_code = await self._captcha_resolver.resolve(
                    provider="gib", image_png=challenge.image_png,
                )
                if not captcha_code:
                    last_error_type = "captcha_unavailable"
                    _log_attempt(plate, "gib", mode, attempt, max_attempts, last_error_type)
                    continue

                try:
                    outcome = await provider.submit(
                        plate=plate, image_id=challenge.image_id, captcha_code=captcha_code,
                    )
                except GibTransportError:
                    _log_attempt(plate, "gib", mode, attempt, max_attempts, "transport_error")
                    return _error_result("gib", "transport_error", checked_at=_now())

                if outcome.kind == "rejected":
                    last_error_type = "captcha_rejected"
                    _log_attempt(plate, "gib", mode, attempt, max_attempts, last_error_type)
                    continue

                _log_attempt(plate, "gib", mode, attempt, max_attempts, outcome.kind)
                _log_finished(plate, "gib", mode, attempt, outcome.kind)
                if outcome.kind == "has_debt":
                    translated_fines = await self._translate_gib_fines(outcome.fines)
                    outcome = replace(outcome, fines=translated_fines)
                return _gib_outcome_to_result(outcome, checked_at=_now())

            _log_finished(plate, "gib", mode, max_attempts, f"error({last_error_type})")
            return _error_result("gib", last_error_type, checked_at=_now())
        finally:
            await client.aclose()

    async def _check_avrasya(
        self, plate: str, *, max_attempts: int, mode: str,
    ) -> ProviderCheckResult:
        client, provider = self._avrasya_check_factory()
        try:
            last_error_type = "captcha_unavailable"
            for attempt in range(1, max_attempts + 1):
                try:
                    challenge = await provider.start() if attempt == 1 else await provider.refresh_captcha()
                except AvrasyaRateLimitedError:
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, "rate_limited")
                    return _error_result("avrasya", "rate_limited", checked_at=_now())
                except AvrasyaTransportError:
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, "transport_error")
                    return _error_result("avrasya", "transport_error", checked_at=_now())

                captcha_code = await self._captcha_resolver.resolve(
                    provider="avrasya", image_png=challenge.image_png,
                )
                if not captcha_code:
                    last_error_type = "captcha_unavailable"
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, last_error_type)
                    continue

                try:
                    outcome = await provider.submit(plate=plate, captcha_code=captcha_code)
                except AvrasyaRateLimitedError:
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, "rate_limited")
                    return _error_result("avrasya", "rate_limited", checked_at=_now())
                except AvrasyaTransportError:
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, "transport_error")
                    return _error_result("avrasya", "transport_error", checked_at=_now())

                if outcome.kind == "rejected":
                    last_error_type = "captcha_rejected"
                    _log_attempt(plate, "avrasya", mode, attempt, max_attempts, last_error_type)
                    continue

                _log_attempt(plate, "avrasya", mode, attempt, max_attempts, outcome.kind)
                _log_finished(plate, "avrasya", mode, attempt, outcome.kind)
                return _avrasya_outcome_to_result(outcome, checked_at=_now())

            _log_finished(plate, "avrasya", mode, max_attempts, f"error({last_error_type})")
            return _error_result("avrasya", last_error_type, checked_at=_now())
        finally:
            await client.aclose()

    async def _check_kgm(
        self, plate: str, *, max_attempts: int, mode: str,
    ) -> ProviderCheckResult:
        client, provider = self._kgm_check_factory()
        try:
            last_error_type = "captcha_unavailable"
            for attempt in range(1, max_attempts + 1):
                try:
                    challenge = await provider.start() if attempt == 1 else await provider.refresh_captcha()
                except KgmTransportError:
                    _log_attempt(plate, "kgm", mode, attempt, max_attempts, "transport_error")
                    return _error_result("kgm", "transport_error", checked_at=_now())

                captcha_code = await self._captcha_resolver.resolve(
                    provider="kgm", image_png=challenge.image_png,
                )
                if not captcha_code:
                    last_error_type = "captcha_unavailable"
                    _log_attempt(plate, "kgm", mode, attempt, max_attempts, last_error_type)
                    continue

                try:
                    outcome = await provider.submit(plate=plate, captcha_code=captcha_code)
                except KgmTransportError:
                    _log_attempt(plate, "kgm", mode, attempt, max_attempts, "transport_error")
                    return _error_result("kgm", "transport_error", checked_at=_now())

                if outcome.kind == "rejected":
                    last_error_type = "captcha_rejected"
                    _log_attempt(plate, "kgm", mode, attempt, max_attempts, last_error_type)
                    continue

                _log_attempt(plate, "kgm", mode, attempt, max_attempts, outcome.kind)
                _log_finished(plate, "kgm", mode, attempt, outcome.kind)
                return _kgm_outcome_to_result(outcome, checked_at=_now())

            _log_finished(plate, "kgm", mode, max_attempts, f"error({last_error_type})")
            return _error_result("kgm", last_error_type, checked_at=_now())
        finally:
            await client.aclose()
