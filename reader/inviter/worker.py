import asyncio
import logging

from reader.inviter.repository import InviteCampaignRepository, TelegramAccountRepository
from reader.inviter.service import InviterService

logger = logging.getLogger(__name__)


class InviterWorker:
    """Постоянный фоновый режим инвайтера (см. python -m reader.inviter.main
    --worker) — вместо разовой дневной пачки (run(execute=True), которая
    отправляет каждым аккаунтом волну до daily_limit за один проход)
    равномерно распределяет приглашения во времени, ни одной новой
    бизнес-логики приглашения не заводя: вся отправка/учёт/FloodWait/
    daily_limit — по-прежнему InviterService (см. run_one_worker_attempt).

    За один тик (см. run_one_tick) обрабатывается РОВНО одна пара
    (кампания, аккаунт) — следующая по кругу (round-robin) среди всех
    enabled кампаний x enabled аккаунтов, в стабильном порядке (id). Это
    даёт сразу два свойства без необходимости хранить расписание где-либо:

    1. Разные аккаунты никогда не приглашают одновременно — тик,
       обработавший Account1, и тик, который дойдёт до Account2, разделены
       как минимум poll_interval_seconds (см. run_forever) — именно этим
       достигается "разнесение по времени" из задачи (10:00 Account1,
       10:10 Account2, 10:20 Account3, ... — конкретные минуты нигде не
       хардкодятся, они складываются из poll_interval_seconds x позиция в
       очереди, и админ настраивает paus poll_interval_seconds исходя из
       желаемого интервала и количества аккаунтов).
    2. Настоящий часовой лимит (invitations_per_account_per_hour) — НЕ
       эта раскладка по времени, а строгая проверка внутри
       InviterService.run_one_worker_attempt() по скользящему окну факти-
       ческой истории приглашений (см. UserCampaignInviteRepository.
       count_recent_sent) — даже если раскладка по каким-то причинам
       ускорится (например, часть аккаунтов отключена и очередь стала
       короче), лимит всё равно не будет превышен.

    Если для пары в этот тик ничего не отправлено (аккаунт заблокирован,
    daily_limit/часовой лимит исчерпаны, кандидатов нет, сессии нет и
    т.п.) — run_one_worker_attempt() просто возвращает None, и worker
    молча переходит к следующему тику без каких-либо попыток "догнать"
    пропущенное (см. задачу: никакого catch-up после простоя/FloodWait).

    TelegramClient не держится между тиками: каждый вызов
    run_one_worker_attempt() -> InviterService._execute_account()
    подключается и отключается сам (см. её докстрок) — тот же lifecycle,
    что и у обычного run(execute=True), просто чаще и меньшими порциями."""

    def __init__(
        self,
        service: InviterService,
        campaign_repository: InviteCampaignRepository,
        account_repository: TelegramAccountRepository,
        *,
        invitations_per_account_per_hour: int,
        poll_interval_seconds: float,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._service = service
        self._campaign_repository = campaign_repository
        self._account_repository = account_repository
        self._invitations_per_account_per_hour = invitations_per_account_per_hour
        self._poll_interval_seconds = poll_interval_seconds
        self._shutdown_event = shutdown_event
        self._rotation_index = 0

    async def run_forever(self) -> None:
        """Работает до shutdown_event.set() (см. reader/inviter/main.py —
        SIGTERM/SIGINT ставят его, а не отменяют текущую задачу) —
        проверяется ТОЛЬКО между тиками, поэтому уже начатая попытка
        приглашения (run_one_tick -> run_one_worker_attempt) всегда
        успевает завершиться штатно, включая disconnect() клиента,
        прежде чем цикл остановится (graceful shutdown, см. задачу)."""
        while not self._shutdown_event.is_set():
            await self.run_one_tick()
            await self._sleep_until_next_tick()

    async def _sleep_until_next_tick(self) -> None:
        """asyncio.wait_for(shutdown_event.wait(), ...) вместо
        asyncio.sleep() — если сигнал остановки пришёл ВО ВРЕМЯ ожидания
        между тиками, worker просыпается немедленно, а не ждёт полный
        poll_interval_seconds впустую."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(), timeout=self._poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass

    async def run_one_tick(self) -> None:
        """Один тик — не более одной пары (кампания, аккаунт), следующей
        по кругу. Список enabled-пар перечитывается заново на каждом тике
        (а не кэшируется) — новый/отключённый аккаунт или кампания
        подхватываются со следующего же тика, без перезапуска процесса."""
        pairs = self._enabled_pairs()
        if not pairs:
            logger.info(
                "[WORKER] Нет enabled кампаний/аккаунтов — ждём следующего цикла."
            )
            return

        index = self._rotation_index % len(pairs)
        self._rotation_index += 1
        campaign, account = pairs[index]

        await self._service.run_one_worker_attempt(
            campaign, account, hourly_limit=self._invitations_per_account_per_hour,
        )

    def _enabled_pairs(self):
        campaigns = [c for c in self._campaign_repository.list() if c.enabled]
        accounts = [a for a in self._account_repository.list() if a.enabled]
        return [(campaign, account) for campaign in campaigns for account in accounts]
