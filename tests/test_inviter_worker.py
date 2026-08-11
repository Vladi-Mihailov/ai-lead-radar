"""
Тесты reader/inviter/worker.py (InviterWorker) — постоянный фоновый режим
инвайтера. Никакой бизнес-логики приглашения здесь нет (она остаётся в
InviterService, см. test_inviter_candidates.py::test_worker_attempt_*) —
только раскладка по кругу (round-robin) и graceful shutdown, поэтому
InviterService подменяется простым фейком, который лишь запоминает вызовы.
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
)
from reader.inviter.worker import InviterWorker  # noqa: E402


class _FakeWorkerService:
    """Ровно то, что нужно InviterWorker от InviterService —
    run_one_worker_attempt(campaign, account, hourly_limit=...) — не
    отправляет ничего реально, только запоминает, с какими аргументами
    его вызвали (в порядке вызовов, см. test_run_one_tick_*)."""

    def __init__(self, *, delay: float = 0.0):
        self.calls: list[tuple[str, str, int]] = []
        self._delay = delay

    async def run_one_worker_attempt(self, campaign, account, *, hourly_limit):
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((campaign.name, account.name, hourly_limit))
        return None


def _make_repos(tmp_path):
    db_path = tmp_path / "inviter.db"
    return (
        InviteCampaignRepository(db_path),
        TelegramAccountRepository(db_path),
    )


def _make_worker(service, campaign_repository, account_repository, *, hourly_limit=2, poll_interval=600):
    return InviterWorker(
        service, campaign_repository, account_repository,
        invitations_per_account_per_hour=hourly_limit,
        poll_interval_seconds=poll_interval,
        shutdown_event=asyncio.Event(),
    )


# ---- run_one_tick() — round-robin по кругу ----


def test_run_one_tick_cycles_through_accounts_round_robin(tmp_path):
    """3 enabled-аккаунта, 1 кампания — четыре тика подряд должны пройти
    Account1, Account2, Account3, Account1 (по кругу, ORDER BY id, см.
    TelegramAccountRepository.list()), по одному приглашению-попытке за
    тик — не burst из всех аккаунтов сразу (см. задачу)."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        for name in ("Account1", "Account2", "Account3"):
            account_repository.create(
                name=name, phone="+995500000001", session_name=name.lower(),
                session_path=f"{name.lower()}.session",
            )

        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository)

        for _ in range(4):
            asyncio.run(worker.run_one_tick())

        assert [account for _, account, _ in service.calls] == [
            "Account1", "Account2", "Account3", "Account1",
        ]
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_one_tick_processes_campaign_account_cross_product(tmp_path):
    """2 кампании x 2 аккаунта — должны обрабатываться все 4 пары по
    кругу (campaign1,acc1), (campaign1,acc2), (campaign2,acc1),
    (campaign2,acc2) — а не только по одной кампании за раз."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="CampaignA", keyword="осаго", target_chat="@a")
        campaign_repository.create(name="CampaignB", keyword="каско", target_chat="@b")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1", session_path="a1.session",
        )
        account_repository.create(
            name="Account2", phone="+995500000002", session_name="a2", session_path="a2.session",
        )

        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository)

        for _ in range(4):
            asyncio.run(worker.run_one_tick())

        assert [(c, a) for c, a, _ in service.calls] == [
            ("CampaignA", "Account1"),
            ("CampaignA", "Account2"),
            ("CampaignB", "Account1"),
            ("CampaignB", "Account2"),
        ]
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_one_tick_skips_disabled_accounts_and_campaigns(tmp_path):
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Enabled", keyword="осаго", target_chat="@t", enabled=True)
        campaign_repository.create(name="Disabled", keyword="осаго", target_chat="@t2", enabled=False)
        account_repository.create(
            name="EnabledAcc", phone="+995500000001", session_name="a1",
            session_path="a1.session", enabled=True,
        )
        account_repository.create(
            name="DisabledAcc", phone="+995500000002", session_name="a2",
            session_path="a2.session", enabled=False,
        )

        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository)

        asyncio.run(worker.run_one_tick())
        asyncio.run(worker.run_one_tick())

        # Только одна валидная пара (Enabled, EnabledAcc) — повторяется,
        # ничего с disabled-стороной никогда не вызывается.
        assert [(c, a) for c, a, _ in service.calls] == [
            ("Enabled", "EnabledAcc"), ("Enabled", "EnabledAcc"),
        ]
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_one_tick_passes_configured_hourly_limit(tmp_path):
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1", session_path="a1.session",
        )

        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository, hourly_limit=5)

        asyncio.run(worker.run_one_tick())

        assert service.calls == [("Campaign", "Account1", 5)]
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_one_tick_does_nothing_when_no_enabled_pairs(tmp_path):
    """Ни одного enabled-аккаунта/кампании — тик должен пройти без
    исключений, просто ничего не вызвав (см. задачу: "при отсутствии
    подходящих кандидатов worker просто ждёт следующего цикла")."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository)

        asyncio.run(worker.run_one_tick())

        assert service.calls == []
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_one_tick_picks_up_newly_enabled_account_without_restart(tmp_path):
    """Список enabled-пар перечитывается на каждом тике — новый аккаунт,
    добавленный/включённый между тиками, участвует в раскладке уже со
    следующего тика, без перезапуска worker."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1", session_path="a1.session",
        )

        service = _FakeWorkerService()
        worker = _make_worker(service, campaign_repository, account_repository)

        asyncio.run(worker.run_one_tick())
        account_repository.create(
            name="Account2", phone="+995500000002", session_name="a2", session_path="a2.session",
        )
        asyncio.run(worker.run_one_tick())
        asyncio.run(worker.run_one_tick())

        assert [account for _, account, _ in service.calls] == [
            "Account1", "Account2", "Account1",
        ]
    finally:
        campaign_repository.close()
        account_repository.close()


def test_worker_spaces_same_account_attempts_by_poll_interval_times_pair_count(tmp_path):
    """Требование задачи: при N enabled-парах и фиксированном
    poll_interval_seconds конкретный аккаунт должен получать попытку
    примерно раз в N*poll_interval_seconds (round-robin по кругу), а НЕ на
    каждом тике — эмпирическая проверка реального run_forever() (не
    только порядка вызовов за один тик, см.
    test_run_one_tick_cycles_through_accounts_round_robin)."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        for name in ("Account1", "Account2", "Account3"):
            account_repository.create(
                name=name, phone="+995500000001", session_name=name.lower(),
                session_path=f"{name.lower()}.session",
            )

        poll_interval = 0.05
        timestamps: dict[str, list[float]] = {}

        class _TimestampingService:
            async def run_one_worker_attempt(self, campaign, account, *, hourly_limit):
                timestamps.setdefault(account.name, []).append(
                    asyncio.get_running_loop().time()
                )
                return None

        shutdown_event = asyncio.Event()
        worker = InviterWorker(
            _TimestampingService(), campaign_repository, account_repository,
            invitations_per_account_per_hour=2,
            poll_interval_seconds=poll_interval,
            shutdown_event=shutdown_event,
        )

        async def scenario():
            task = asyncio.create_task(worker.run_forever())
            # Достаточно времени примерно на 3 полных круга (9 тиков, по
            # 3 пары каждый) — с запасом с обеих сторон.
            await asyncio.sleep(poll_interval * 9.5)
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())

        account1_times = timestamps["Account1"]
        assert len(account1_times) >= 2
        deltas = [b - a for a, b in zip(account1_times, account1_times[1:])]
        for delta in deltas:
            # При 3 enabled-парах ожидается интервал ~3*poll_interval между
            # повторными попытками ОДНОГО и того же аккаунта — если бы
            # round-robin не работал (аккаунт проверялся на каждом тике),
            # delta была бы ~poll_interval. Порог x2 — с запасом на джиттер
            # планировщика, но чётко отличает "раз в круг" от "на каждом тике".
            assert delta > poll_interval * 2, (
                f"аккаунт проверялся слишком часто: delta={delta}, "
                f"poll_interval={poll_interval}"
            )
    finally:
        campaign_repository.close()
        account_repository.close()


def test_new_worker_instance_after_restart_attempts_only_one_pair_not_a_burst(tmp_path):
    """Симулирует restart процесса: rotation_index существует только в
    памяти (InviterWorker.__init__), не персистентен — после "падения" и
    пересоздания worker'а (новый Python-объект, та же БД) первый тик
    должен обработать РОВНО одну пару, а не попытаться наверстать
    пропущенные во время простоя циклы (см. задачу: никакого catch-up
    после restart/простоя)."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        for name in ("Account1", "Account2", "Account3"):
            account_repository.create(
                name=name, phone="+995500000001", session_name=name.lower(),
                session_path=f"{name.lower()}.session",
            )

        service_before_crash = _FakeWorkerService()
        worker_before_crash = _make_worker(service_before_crash, campaign_repository, account_repository)
        asyncio.run(worker_before_crash.run_one_tick())
        asyncio.run(worker_before_crash.run_one_tick())
        # "Простой" — реальный процесс в это время просто не существовал бы,
        # никакого таймера/очереди отложенных тиков нигде не ведётся.

        # "Restart" — совершенно новый InviterWorker (rotation_index снова
        # 0) и новый fake-сервис, но та же БД (campaign_repository/
        # account_repository не пересоздаются, как и настоящая
        # user_campaign_invites на диске после реального restart).
        service_after_restart = _FakeWorkerService()
        worker_after_restart = _make_worker(service_after_restart, campaign_repository, account_repository)

        asyncio.run(worker_after_restart.run_one_tick())

        assert len(service_after_restart.calls) == 1
    finally:
        campaign_repository.close()
        account_repository.close()


# ---- run_forever() — graceful shutdown ----


def test_run_forever_stops_promptly_when_shutdown_requested_during_wait(tmp_path):
    """shutdown_event.set() во время ожидания между тиками (а не во время
    самой попытки) — run_forever() должен вернуться почти сразу, а не
    ждать полный (нарочно большой) poll_interval_seconds."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1", session_path="a1.session",
        )

        service = _FakeWorkerService()
        shutdown_event = asyncio.Event()
        worker = InviterWorker(
            service, campaign_repository, account_repository,
            invitations_per_account_per_hour=2,
            poll_interval_seconds=3600,  # нарочно большой — тест не должен его дожидаться
            shutdown_event=shutdown_event,
        )

        async def scenario():
            task = asyncio.create_task(worker.run_forever())
            await asyncio.sleep(0)  # даём успеть выполнить первый тик и уйти в ожидание
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())

        assert len(service.calls) == 1
    finally:
        campaign_repository.close()
        account_repository.close()


def test_run_forever_finishes_in_flight_tick_before_stopping(tmp_path):
    """shutdown_event выставлен ПОКА текущий тик ещё выполняется
    (run_one_worker_attempt "в процессе") — этот тик должен завершиться
    штатно (graceful — см. задачу: "закончить текущую безопасную
    операцию"), и только потом worker останавливается, не начиная новый тик."""
    campaign_repository, account_repository = _make_repos(tmp_path)
    try:
        campaign_repository.create(name="Campaign", keyword="осаго", target_chat="@t")
        account_repository.create(
            name="Account1", phone="+995500000001", session_name="a1", session_path="a1.session",
        )

        service = _FakeWorkerService(delay=0.05)
        shutdown_event = asyncio.Event()
        worker = InviterWorker(
            service, campaign_repository, account_repository,
            invitations_per_account_per_hour=2,
            poll_interval_seconds=3600,
            shutdown_event=shutdown_event,
        )

        async def scenario():
            task = asyncio.create_task(worker.run_forever())
            await asyncio.sleep(0.01)  # тик уже начался (внутри asyncio.sleep(0.05))
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())

        # Ровно один тик — начатый до сигнала остановки, но успевший
        # дописать call в fake-сервис (т.е. точно завершившийся, а не
        # прерванный на середине).
        assert len(service.calls) == 1
    finally:
        campaign_repository.close()
        account_repository.close()
