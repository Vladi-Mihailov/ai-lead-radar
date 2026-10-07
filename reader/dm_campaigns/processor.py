"""DmDraftProcessor — фоновый цикл внутри процесса Reader (reader/main.py),
без отдельного сервиса/очереди: SQLite (dm_outreach) — источник истины.

На каждом тике:
1. drafting, зависший дольше drafting_recovery_seconds (процесс упал
   посреди генерации), -> pending_context;
2. раз в час — очистка буфера group_messages_recent старше retention;
3. кандидаты pending_context с draft_after_at <= now — СТРОГО по одному
   (OpenAI concurrency = 1): атомарный claim -> повторная проверка кампании
   (включена? источник всё ещё разрешён?) -> контекст -> OpenAI ->
   проверка ответа -> draft / filtered / failed.

Ничего не отправляет в Telegram — только пишет черновик в БД."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from reader.dm_campaigns.border import (
    BorderPolicy,
    cta_handles,
    footer_lines,
    georgia_trip,
    greeting_for,
    negative_signals,
    place_hint,
    requested_checkpoints,
)
from reader.dm_campaigns.context import DmContextBuilder
from reader.dm_campaigns.draft_models import (
    LEAD_INTENTS,
    DmDraftOutput,
    DmIntentOutput,
    normalize_draft,
    resolve_intent,
)
from reader.dm_campaigns.draft_prompt import (
    EXPERT_CAMPAIGNS,
    INTENT_CLASSIFIER_PROMPT,
    build_classification_text,
    build_user_text,
    reply_to_other,
    system_prompt_for,
)
from reader.dm_campaigns.draft_service import DmDraftService, DmDraftServiceError
from reader.dm_campaigns.models import source_chat_allowed
from reader.dm_campaigns.observer import ACTIVE_DRAFT_WINDOW, FILTER_ACTIVE_DRAFT
from reader.dm_campaigns.outreach_repository import MAX_ATTEMPTS, DmOutreach, DmOutreachRepository
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.relevance import (
    FILTER_CAMPAIGN_NOT_RELEVANT,
    ROUTABLE_CAMPAIGNS,
    campaign_relevant,
    side_topics,
)
from reader.dm_campaigns.repository import DmCampaignRepository
from reader.dm_campaigns.resources import allowed_resources
from reader.groups import REGION_UNKNOWN

logger = logging.getLogger(__name__)

FILTER_CAMPAIGN_DISABLED = "campaign_disabled_before_generation"
FILTER_SOURCE_CHANGED = "source_chat_not_allowed_before_generation"
FILTER_AI_NOT_SUITABLE = "ai_not_suitable"

# Исправимые причины отказа экспертного черновика -> подсказка для одной
# повторной генерации.
RETRY_HINTS = {
    "topic_leak_medical": "в ответе есть медицинская страховка, а USER о ней не спрашивал — убери её.",
    "too_long": "слишком длинно — максимум 3 коротких предложения.",
    "legalese": "юридические обороты — перескажи простыми словами.",
    "missing_resource": "упомяни по назначению каждый ресурс из ALLOWED PROMOTED RESOURCES.",
    "ad_only_answer": "сначала ответ по существу, ресурсы — после.",
    "expert_hedging_or_group_reference": "без оговорок и без ссылок на группу или её участников.",
    "unapproved_claim": "утверждение, которого нет в утверждённых фактах, — убери его.",
    "unnecessary_clarification": "не переспрашивай — ответь тем, что следует из фактов.",
}
# fuel / border_queue: та же одна исправляющая попытка — для оговорок,
# обещаний и ссылок на несуществующие сообщения.
DYNAMIC_RETRY_HINTS = {
    "dynamic_hedging": "без оговорок «точно подтвердить не могу» и без отсылок к официальным каналам; нет данных — "
                       "скажи прямо: «В свежих сообщениях нет данных о …».",
    "future_promise": "не обещай ничего сделать потом (посмотрю, проверю, уточню, напишите маршрут) — ответь сразу "
                      "тем, что есть.",
    "unknown_context_refs": "used_context_refs — только метки сообщений из контекста; утверждай только то, что есть "
                            "в этих сообщениях.",
    "topic_leak": "в ответе только темы, о которых USER не спрашивал (страховка, бензин, посылки, штрафы, документы, "
                  "номера, обмен валют, другие КПП) — ответь только на его вопрос о проезде.",
    "source_disclosure": "не раскрывай источник: без «по свежим сообщениям», «в группе пишут», «участники», «отзыв», "
                         "«нет данных» — отвечай от себя как сервис (нет негативных сигналов — «открыт, критичных "
                         "очередей сейчас нет»).",
    "contradicts_signals": "в BORDER SIGNALS есть негативные сигналы — отрази их и не пиши «очередей нет», "
                           "«спокойно», «свободно».",
    "overclaim": "без «точно», «гарантированно» — обычная уверенная формулировка.",
    "ad_only_answer": "сначала прямой ответ на вопрос по существу; ресурсы не упоминай — их добавит система.",
    "unsupported_generalization": "без предположений «обычно», «чаще всего», «как правило», «проблем не "
                                  "предвидится» — только подтверждённое, иначе безопасный совет без выдуманного факта.",
    "unsupported_detail": "в ответе места, трассы или цифры, которых нет в сообщениях контекста, — убери их; "
                          "оставь только подтверждённое.",
}

# ЛС-кампании с серверным оформлением ответа (приветствие; для fuel и
# border_queue — ещё подвал с ресурсами и запрет раскрывать источник).
REPLY_POLICY_CAMPAIGNS = frozenset({"insurance", "fuel", "border_queue"})

_BATCH_LIMIT = 5
_CLEANUP_INTERVAL_SECONDS = 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DmDraftProcessor:
    def __init__(
        self,
        outreach_repository: DmOutreachRepository,
        campaign_repository: DmCampaignRepository,
        recent_repository: RecentMessageRepository,
        context_builder: DmContextBuilder,
        service: DmDraftService,
        *,
        interval_seconds: float,
        drafting_recovery_seconds: float,
        retention_hours: float,
        generation_timeout_seconds: float = 90.0,
        group_resource_regions: dict[str, str] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._outreach = outreach_repository
        self._campaigns = campaign_repository
        self._recent = recent_repository
        self._context = context_builder
        self._service = service
        self._interval = interval_seconds
        self._recovery = timedelta(seconds=drafting_recovery_seconds)
        self._retention = timedelta(hours=retention_hours)
        self._timeout = generation_timeout_seconds
        # identifier группы (lower, без @) -> resource_region из groups.yaml.
        self._group_regions = {k.lstrip("@").lower(): v for k, v in (group_resource_regions or {}).items()}
        self._clock = clock
        self._monotonic = monotonic
        self._last_cleanup: float | None = None

    async def run_forever(self) -> None:
        """Никогда не роняет Reader: ошибка тика только логируется (иначе
        _run_concurrently в reader/main.py остановил бы весь процесс)."""
        logger.info("dm_outreach draft processor started (interval=%ss)", self._interval)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dm_outreach: ошибка тика обработчика черновиков")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        now = self._clock()
        recovered = self._outreach.recover_stale(stale_before=now - self._recovery, now=now)
        if recovered:
            logger.warning("dm_outreach: восстановлено зависших drafting -> pending_context: %d", recovered)
        self._maybe_cleanup(now)

        processed = 0
        for row in self._outreach.due_pending(now, limit=_BATCH_LIMIT):
            if await self._process(row):
                processed += 1
        return processed

    def _maybe_cleanup(self, now: datetime) -> None:
        tick = self._monotonic()
        if self._last_cleanup is not None and tick - self._last_cleanup < _CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = tick
        deleted = self._recent.delete_older_than(now - self._retention)
        if deleted:
            logger.info("dm_outreach: удалено из group_messages_recent: %d", deleted)

    async def _process(self, row: DmOutreach) -> bool:
        now = self._clock()
        if not self._outreach.claim(row.id, now):
            return False
        if row.attempts + 1 > MAX_ATTEMPTS:
            self._outreach.mark_failed(row.id, now=now, error_kind="too_many_attempts")
            self._log(row, "failed", note="too_many_attempts")
            return True

        campaign = self._campaigns.get_campaign(row.campaign_id)
        if campaign is None or not campaign.enabled:
            self._outreach.mark_filtered(row.id, now=now, reason=FILTER_CAMPAIGN_DISABLED)
            self._log(row, "filtered", note=FILTER_CAMPAIGN_DISABLED)
            return True
        if not source_chat_allowed(campaign, row.source_chat_identifier):
            self._outreach.mark_filtered(row.id, now=now, reason=FILTER_SOURCE_CHANGED)
            self._log(row, "filtered", note=FILTER_SOURCE_CHANGED)
            return True

        # Пока кандидат ждал контекст, тому же человеку мог появиться черновик
        # в этой кампании (другое сообщение той же дискуссии) — второй не нужен.
        if self._outreach.active_draft_exists(
            campaign_id=campaign.id, recipient_user_id=row.recipient_user_id,
            recipient_username=row.recipient_username, since=now - ACTIVE_DRAFT_WINDOW, exclude_id=row.id,
        ):
            self._outreach.mark_filtered(row.id, now=now, reason=FILTER_ACTIVE_DRAFT)
            self._log(row, "filtered", note=FILTER_ACTIVE_DRAFT)
            return True

        try:
            context = self._context.build(row, campaign, now)
        except Exception as exc:
            logger.exception("dm_outreach id=%s: не удалось собрать контекст", row.id)
            self._outreach.mark_failed(row.id, now=self._clock(), error_kind="context_error", error=type(exc).__name__)
            return True
        context_json = context.to_json()
        meta = context.metadata
        # Страновые ресурсы — только по resource_region группы и смыслу
        # вопроса (исходное сообщение + то, на что оно отвечает), см. resources.py.
        region = self._group_regions.get((row.source_chat_identifier or "").lstrip("@").lower(), REGION_UNKNOWN)
        intent_text = " ".join([row.source_text] + [i.text for i in context.discussion if i.ref.startswith("R")])
        resources = allowed_resources(
            campaign.resources, resource_region=region, intent_text=intent_text, campaign_key=campaign.key,
        )
        replies = [i.text for i in context.discussion if i.ref.startswith("R")]
        border_policy = None
        if campaign.key in REPLY_POLICY_CAMPAIGNS:
            # Приветствие — всем ЛС-кампаниям; border_queue — КПП вопроса и
            # негативные сигналы по свежим отчётам; fuel/border_queue —
            # обязательный подвал с ресурсами для поездки, связанной с Грузией.
            border = campaign.key == "border_queue"
            checkpoints = (requested_checkpoints(row.source_text, replies, row.source_chat_identifier)
                           if border else frozenset())
            georgia = georgia_trip(row.source_text, replies, row.source_chat_identifier, checkpoints)
            border_policy = BorderPolicy(
                kind=campaign.key, checkpoints=checkpoints,
                negative_signals=negative_signals(i.text for i in context.evidence) if border else (),
                greeting=greeting_for(row.source_text, continuation=bool(replies)),
                cta=(footer_lines(row.source_text, replies, georgia=georgia, campaign_resources=campaign.resources)
                     if campaign.key != "insurance" else ()),
                side_topics_asked=side_topics(row.source_text),
                context_text=" ".join(
                    [row.source_text] + [i.text for i in context.discussion] + [i.text for i in context.evidence]),
            )
            resources = tuple(dict.fromkeys(resources + cta_handles(border_policy)))

        started = time.monotonic()
        expert = campaign.key in EXPERT_CAMPAIGNS
        intent = None
        try:
            # Шаг 1 (ВСЕ ЛС-кампании): общая классификация роли сообщения —
            # без фактов и ресурсов кампании. Не лид — генерация не запускается.
            classified = await asyncio.wait_for(
                self._service.generate(
                    build_classification_text(row, context), instructions=INTENT_CLASSIFIER_PROMPT,
                    text_format=DmIntentOutput,
                ),
                timeout=self._timeout,
            )
            intent = resolve_intent(classified, reply_to_other=reply_to_other(context))
            intent_audit = {
                "model_intent": classified.intent, "final_intent": intent, "intent": intent,
                "has_own_problem": classified.has_own_problem, "intent_reason": (classified.reason or "")[:300],
            }
            context_json = context.to_json(**intent_audit)
            if intent not in LEAD_INTENTS:
                reason = f"not_lead_intent:{intent.lower()}"
                self._outreach.mark_filtered(row.id, now=self._clock(), reason=reason, context_json=context_json)
                self._log(row, "filtered", campaign_key=campaign.key, note=reason)
                return True
            # Лид, но тема не этой кампании (посылка в border_queue, продукты на
            # таможне…) — без генерации; детерминированно, без OpenAI. Тему
            # insurance уже проверил страховой префильтр observer.
            intent_audit["campaign_relevant"] = campaign.key not in ROUTABLE_CAMPAIGNS or campaign_relevant(
                campaign.key, row.source_text, replies, place_hint=place_hint(row.source_chat_identifier, replies),
            )
            if border_policy is not None:
                intent_audit["border"] = border_policy.audit()
            context_json = context.to_json(**intent_audit)
            if not intent_audit["campaign_relevant"]:
                self._outreach.mark_filtered(
                    row.id, now=self._clock(), reason=FILTER_CAMPAIGN_NOT_RELEVANT, context_json=context_json,
                )
                self._log(row, "filtered", campaign_key=campaign.key, note=FILTER_CAMPAIGN_NOT_RELEVANT)
                return True
            # Шаг 2: генерация ответа генератором ЭТОЙ кампании.
            output = await asyncio.wait_for(
                self._service.generate(
                    build_user_text(row, campaign, context, allowed_resources=resources, border_policy=border_policy),
                    instructions=system_prompt_for(campaign.key),
                    text_format=DmDraftOutput,
                ),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._outreach.mark_failed(row.id, now=self._clock(), error_kind="ai_timeout", context_json=context_json)
            self._log(row, "failed", note="ai_timeout")
            return True
        except DmDraftServiceError as exc:
            self._outreach.mark_failed(
                row.id, now=self._clock(), error_kind="ai_error", error=str(exc), context_json=context_json,
            )
            self._log(row, "failed", note="ai_error")
            return True
        except Exception as exc:
            logger.exception("dm_outreach id=%s: неожиданная ошибка генерации", row.id)
            self._outreach.mark_failed(
                row.id, now=self._clock(), error_kind="internal_error", error=type(exc).__name__,
                context_json=context_json,
            )
            return True

        def decide(out):
            return normalize_draft(
                out,
                follow_up_enabled=campaign.follow_up_enabled,
                valid_refs=context.refs,
                fresh_context_used=context.fresh_context_used,
                n_distinct_senders=meta.n_distinct_senders if meta else 0,
                allowed_resources=resources,
                expert=expert,
                intent_text=intent_text,
                intent=intent,
                side_topics_asked=side_topics(row.source_text) if campaign.key == "border_queue" else None,
                border_policy=border_policy,
            )

        decision = decide(output)
        # Одна исправляющая попытка, если сервер отклонил черновик по исправимой
        # причине (insurance: лишняя медстраховка, длина, канцелярит, нет
        # ресурса…; fuel/border_queue: оговорки, обещания, чужие метки) —
        # модели называется причина; проверка та же, не ослаблена.
        hints = RETRY_HINTS if expert else DYNAMIC_RETRY_HINTS
        hint = hints.get((decision.error or "").split(":")[0]) if decision.kind == "invalid" else None
        if hint:
            try:
                output = await asyncio.wait_for(
                    self._service.generate(
                        build_user_text(row, campaign, context, allowed_resources=resources, border_policy=border_policy)
                        + f"\n\nПРЕДЫДУЩИЙ ЧЕРНОВИК ОТКЛОНЁН ПРОВЕРКОЙ: {hint} Напиши заново, исправив это.",
                        instructions=system_prompt_for(campaign.key),
                        text_format=DmDraftOutput,
                    ),
                    timeout=self._timeout,
                )
                decision = decide(output)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # первая (отклонённая) попытка остаётся итогом
                logger.warning("dm_outreach id=%s: исправляющая попытка не удалась (%s)", row.id, type(exc).__name__)
        latency = time.monotonic() - started
        finished = self._clock()
        if decision.kind == "filtered":
            stored = context.to_json(ai_skip_reason=(output.skip_reason or "")[:300], **intent_audit)
            self._outreach.mark_filtered(
                row.id, now=finished, reason=decision.filter_reason or FILTER_AI_NOT_SUITABLE, context_json=stored,
            )
            status = "filtered"
        elif decision.kind == "invalid":
            self._outreach.mark_failed(
                row.id, now=finished, error_kind="ai_invalid_output", error=decision.error, context_json=context_json,
            )
            status = "failed"
        else:
            if decision.removed_side_topics:
                context_json = context.to_json(removed_side_topics=list(decision.removed_side_topics), **intent_audit)
            self._outreach.mark_draft(
                row.id, now=finished, context_json=context_json, primary_text=decision.primary_message,
                follow_up_text=decision.follow_up_message, evidence_strength=decision.evidence_strength,
                used_context_refs=list(decision.used_context_refs),
            )
            status = "draft"
        self._log(
            row, status, campaign_key=campaign.key, context_count=len(context.discussion),
            evidence_count=len(context.evidence), latency=latency,
            note=decision.error if decision.kind == "invalid" else None,
        )
        return True

    @staticmethod
    def _log(row: DmOutreach, status: str, *, campaign_key: str | None = None, context_count: int | None = None,
             evidence_count: int | None = None, latency: float | None = None, note: str | None = None) -> None:
        logger.info(
            "dm_outreach processed id=%s campaign=%s chat=%s message=%s status=%s context=%s evidence=%s "
            "latency=%s note=%s",
            row.id, campaign_key or row.campaign_id, row.source_chat_id, row.source_message_id, status,
            context_count, evidence_count, f"{latency:.2f}s" if latency is not None else None, note,
        )
