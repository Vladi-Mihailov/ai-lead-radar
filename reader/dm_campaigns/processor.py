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

from reader.dm_campaigns.context import DmContextBuilder
from reader.dm_campaigns.draft_models import normalize_draft
from reader.dm_campaigns.draft_prompt import build_user_text
from reader.dm_campaigns.draft_service import DmDraftService, DmDraftServiceError
from reader.dm_campaigns.models import source_chat_allowed
from reader.dm_campaigns.outreach_repository import MAX_ATTEMPTS, DmOutreach, DmOutreachRepository
from reader.dm_campaigns.recent_messages import RecentMessageRepository
from reader.dm_campaigns.repository import DmCampaignRepository

logger = logging.getLogger(__name__)

FILTER_CAMPAIGN_DISABLED = "campaign_disabled_before_generation"
FILTER_SOURCE_CHANGED = "source_chat_not_allowed_before_generation"
FILTER_AI_NOT_SUITABLE = "ai_not_suitable"

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

        try:
            context = self._context.build(row, campaign, now)
        except Exception as exc:
            logger.exception("dm_outreach id=%s: не удалось собрать контекст", row.id)
            self._outreach.mark_failed(row.id, now=self._clock(), error_kind="context_error", error=type(exc).__name__)
            return True
        context_json = context.to_json()
        meta = context.metadata

        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                self._service.generate(build_user_text(row, campaign, context)), timeout=self._timeout,
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
        latency = time.monotonic() - started

        decision = normalize_draft(
            output,
            follow_up_enabled=campaign.follow_up_enabled,
            valid_refs=context.refs,
            fresh_context_used=context.fresh_context_used,
            n_distinct_senders=meta.n_distinct_senders if meta else 0,
            allowed_resources=campaign.resources,
        )
        finished = self._clock()
        if decision.kind == "filtered":
            stored = context.to_json(ai_skip_reason=(output.skip_reason or "")[:300])
            self._outreach.mark_filtered(row.id, now=finished, reason=FILTER_AI_NOT_SUITABLE, context_json=stored)
            status = "filtered"
        elif decision.kind == "invalid":
            self._outreach.mark_failed(
                row.id, now=finished, error_kind="ai_invalid_output", error=decision.error, context_json=context_json,
            )
            status = "failed"
        else:
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
