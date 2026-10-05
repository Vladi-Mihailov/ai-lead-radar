"""DmDraftNotifier — фоновый цикл процесса inviter_admin_bot: новые
ЛС-черновики (status='draft', ещё не разосланные) приходят карточкой с
кнопками ✅/✏️/⏭ каждому trusted-оператору в личку с ботом.

Черновики создаёт Reader (другой процесс) — общий только users.db, поэтому
опрос БД, а не событие. Строка помечается operator_notified_at атомарно ДО
рассылки: повторно она не придёт (в том числе после перезапуска бота), а
сама очередь "🆕 Новые" её по-прежнему показывает. Слишком старые черновики
(старше max_age, например накопившиеся до появления этой функции) карточкой
не рассылаются — только видны в очереди.

Это уведомление оператору, а не отправка ЛС лиду: send() — сообщение бота
trusted-администратору."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone

from reader.dm_campaigns.outreach_repository import DmOutreachRepository
from reader.inviter_admin_bot.conversation import BotReply
from reader.inviter_admin_bot.dm_draft_controller import DmDraftController

logger = logging.getLogger(__name__)

_BATCH_LIMIT = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DmDraftNotifier:
    def __init__(
        self,
        outreach_repository: DmOutreachRepository,
        controller: DmDraftController,
        send: Callable[[int, BotReply], Awaitable[None]],
        operator_ids: Iterable[int],
        *,
        interval_seconds: float = 30.0,
        max_age: timedelta = timedelta(hours=6),
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._outreach = outreach_repository
        self._controller = controller
        self._send = send
        self._operators = tuple(sorted(set(operator_ids)))
        self._interval = interval_seconds
        self._max_age = max_age
        self._clock = clock

    async def run_forever(self) -> None:
        logger.info("dm drafts notifier started (interval=%ss, operators=%d)", self._interval, len(self._operators))
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dm drafts notifier: ошибка тика")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        now = self._clock()
        items = self._outreach.claim_unnotified_drafts(
            now=now, generated_after=now - self._max_age, limit=_BATCH_LIMIT,
        )
        for item in items:
            delivered = 0
            for operator_id in self._operators:
                reply = self._controller.notification_reply(item, telegram_user_id=operator_id)
                if reply is None:
                    continue
                try:
                    await self._send(operator_id, reply)
                    delivered += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "dm drafts notifier: карточку id=%s не удалось доставить оператору (%s)",
                        item.id, type(exc).__name__,
                    )
            logger.info("dm drafts notifier: карточка черновика id=%s доставлена операторам: %d", item.id, delivered)
        return len(items)
