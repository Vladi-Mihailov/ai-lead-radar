"""SubscriptionService — application/service layer между Add Car conversation
(reader/public_bot/conversation.py) и уже существующим Fine Monitor.

Ничего не дублирует из reader/fines/*: task_repository и check_service —
ТЕ ЖЕ САМЫЕ объекты, что использует и операторский FineJob/FineCommand (см.
reader/public_bot/main.py — сборка). Никакой отдельной реализации
provider/check-логики здесь нет.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineMonitoringTask
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.car_owner_sync import sync_user_and_car
from reader.public_bot.models import FineMonitoringSubscription
from reader.public_bot.owner_resolution import (
    OwnerUsernameResolverLike,
    resolve_owner_username,
)
from reader.public_bot.subscription_repository import (
    DuplicateActiveSubscriptionError,
    DuplicatePendingClaimError,
    FineSubscriptionRepository,
    default_claim_token_expiry,
    generate_claim_token,
)
from reader.users.repository import UserRepository

_SOURCE = "geshtrafbot"


@dataclass(frozen=True)
class AddCarOutcome:
    """Результат self-service Add Car flow (см. SubscriptionService.add_car) —
    ok=False только когда сама проверка штрафов завершилась ошибкой (тот же
    смысл, что и у reader/commands/fine.py::_ImmediateCheckOutcome), не
    когда штрафов просто не нашлось (тогда ok=True, new_fines_count=0)."""

    task: FineMonitoringTask
    subscription: FineMonitoringSubscription
    check_ok: bool
    new_fines_count: int


@dataclass(frozen=True)
class DelegatedAddCarOutcome:
    """Результат trusted-operator delegated Add Car flow. pending_claim=True
    означает, что owner_username не удалось надёжно резолвить — subscription
    в статусе 'pending_claim' (telegram_user_id/telegram_chat_id ещё NULL),
    claim_link — deep-link, который trusted-оператор должен переслать
    реальному владельцу (None, если pending_claim=False — резолв удался,
    ссылка не нужна)."""

    task: FineMonitoringTask
    subscription: FineMonitoringSubscription
    pending_claim: bool
    check_ok: bool
    new_fines_count: int
    claim_link: str | None


@dataclass(frozen=True)
class ClaimOutcome:
    subscription: FineMonitoringSubscription


@dataclass(frozen=True)
class CheckNowOutcome:
    """Результат 🔎 Проверить сейчас — ТОТ ЖЕ FineCheckService/дедуп, что и
    у фонового мониторинга (см. SubscriptionService.check_now); никакой
    отдельной системы штрафов здесь нет."""

    car_number: str
    check_ok: bool
    new_fines_count: int


@dataclass(frozen=True)
class AddCarWithoutClientOutcome:
    """Результат trusted-operator Add Car БЕЗ клиента ("Отмена" на "👤
    Добавить Telegram клиента?", см. design report: "без фиктивного
    owner/subscription"). Никакой fine_monitoring_subscriptions строки НЕ
    создаётся — операторская видимость/действия идут через task-level
    admin API (см. list_all_active_tasks/check_now_task/
    stop_task_for_trusted_admin ниже), а уведомление о найденных штрафах —
    ИСКЛЮЧИТЕЛЬНО через уже существующий operator notification mechanism
    (FineNotificationCoordinator, который не зависит от subscriptions
    вовсе и срабатывает для ЛЮБОЙ задачи независимо от scope)."""

    task: FineMonitoringTask
    check_ok: bool
    new_fines_count: int


_CLAIM_TOKEN_TTL = timedelta(days=7)

# Подписки, с которыми пользователь ещё может что-то сделать через
# 🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report Stage 4,
# раздел "UI completion") — активные ИЛИ ожидающие claim, но не
# остановленные/истёкшие (те уже неактуальны для действия).
_ACTIONABLE_STATUSES = ("active", "pending_claim")


async def extend_client_bot_task_if_still_needed(
    task: FineMonitoringTask,
    today: date,
    *,
    task_repository: FineMonitoringTaskRepository,
    subscription_repository: FineSubscriptionRepository,
) -> FineMonitoringTask:
    """pre_complete_hook для client_bot-scope FineJob (см.
    reader/jobs/fine_job.py, design report Stage 4, раздел "Task
    lifecycle") — вызывается ТОЛЬКО когда FineJob уже решил, что
    today > task.end_date, ПЕРЕД тем как пометить задачу completed.

    Пересчитывает максимальный end_date среди ещё действующих (active ИЛИ
    pending_claim) подписок этой машины — если он всё ещё >= today,
    продлевает задачу (extend_period_if_shorter — никогда не сокращает)
    вместо завершения; иначе возвращает задачу без изменений, и FineJob
    завершает её как обычно ("если больше нет действующих client
    subscriptions и task не operator scope — его можно завершить").

    Каждый существующий путь создания/claim/продления подписки уже вызывает
    extend_period_if_shorter() сам по себе (см. _create_or_extend_task/
    add_delegated_car) — этот хук существует как defense-in-depth
    подстраховка, а не как единственный механизм продления."""
    if task.monitoring_scope != "client_bot":
        # Операторские задачи эту логику не используют вовсе (см. design
        # report: "operator task semantics не менять") — wiring
        # (reader/main.py) подключает этот хук только к client_bot-scope
        # FineJob-инстансу, но проверка здесь — на случай ошибки wiring.
        return task

    needed_until = subscription_repository.max_relevant_end_date_for_car(task.car_number, today=today)
    if needed_until is None or needed_until <= task.end_date:
        return task

    return task_repository.extend_period_if_shorter(task.id, needed_until)


class SubscriptionService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        subscription_repository: FineSubscriptionRepository,
        user_repository: UserRepository,
        check_service: FineCheckService,
        *,
        owner_resolver_client: OwnerUsernameResolverLike | None = None,
        bot_username: str = "GEShtrafbot",
    ):
        self._task_repository = task_repository
        self._subscription_repository = subscription_repository
        self._user_repository = user_repository
        self._check_service = check_service
        # None — как и everywhere в проекте (см. FineCommand.telegram_client) —
        # означает "живой резолв через Telegram не выполняется", резолв
        # тогда работает только по локальной БД (см. owner_resolution.py).
        self._owner_resolver_client = owner_resolver_client
        self._bot_username = bot_username

    # ---- self-service (обычный пользователь) — БЕЗ ИЗМЕНЕНИЙ с Stage 2 ----

    async def add_car(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str,
        first_name: str | None,
        last_name: str | None,
        car_number: str,
        period_days: int,
        today: date,
    ) -> AddCarOutcome:
        start_date = today
        end_date = today + timedelta(days=period_days)

        task = self._create_or_extend_task(
            car_number=car_number,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            start_date=start_date,
            end_date=end_date,
        )

        subscription = self._create_or_update_subscription(
            task_id=task.id,
            car_number=car_number,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            username=username,
            start_date=start_date,
            end_date=end_date,
        )

        # Тот же UserRepository, что использует и остальной Reader (см.
        # reader/public_bot/main.py) — operator-facing "Telegram: @username"
        # (fine list/fine check/уведомления) начинает показывать клиента
        # без единой правки в reader/commands/fine.py.
        sync_user_and_car(
            self._user_repository,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            car_number=car_number,
        )

        # Тот же FineCheckService, что и у FineJob/FineCommand — никакой
        # отдельной реализации проверки. Дедуп (detected_fines) отрабатывает
        # ровно так же, как и для операторского мониторинга. Доставка
        # найденного штрафа ОПЕРАТОРУ (flush_pending()) здесь намеренно НЕ
        # вызывается — новый штраф уйдёт оператору на ближайшем плановом
        # запуске FineJob/ArchiveFineJob, дедуп при этом не страдает: сам
        # факт обнаружения уже сохранён в detected_fines.
        check_result = await self._check_service.check_task(task)

        return AddCarOutcome(
            task=task,
            subscription=subscription,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
        )

    # ---- trusted-operator delegated flow ----

    async def add_delegated_car(
        self,
        *,
        created_by_telegram_user_id: int,
        created_by_telegram_chat_id: int,
        owner_username: str,
        car_number: str,
        period_days: int,
        today: date,
    ) -> DelegatedAddCarOutcome:
        """Trusted-оператор ставит автомобиль на мониторинг ДЛЯ ДРУГОГО
        человека (см. design report). Резолв владельца — ПЕРЕД созданием
        задачи/подписки (тот же порядок, что и в reader/commands/fine.py::
        _handle_add — резолв @username происходит до task_repository.
        create(), чтобы техническая ошибка резолва не оставляла частично
        созданное состояние); OwnerResolutionError пробрасывается
        вызывающему коду как есть — ничего не создаётся."""
        start_date = today
        end_date = today + timedelta(days=period_days)

        resolved = await resolve_owner_username(
            owner_username,
            user_repository=self._user_repository,
            telegram_client=self._owner_resolver_client,
        )

        # Задача мониторинга создаётся/продлевается ВСЕГДА, независимо от
        # того, удалось ли резолвить владельца — мониторинг не должен
        # ждать owner claim (см. design report: "monitoring task
        # запускается сразу, claim владельца не блокирует мониторинг").
        # created_by_user_id/telegram_chat_id задачи — trusted-оператор
        # (это ТОЛЬКО информационное поле, см. reader/fines/task_repository.py —
        # ownership клиента полностью определяется fine_monitoring_subscriptions,
        # см. design report: "Не использовать fine_monitoring_tasks.
        # created_by_user_id для client ownership").
        task = self._create_or_extend_task(
            car_number=car_number,
            telegram_user_id=created_by_telegram_user_id,
            telegram_chat_id=created_by_telegram_chat_id,
            start_date=start_date,
            end_date=end_date,
        )

        if resolved is not None:
            subscription = self._create_or_update_subscription(
                task_id=task.id,
                car_number=car_number,
                telegram_user_id=resolved.telegram_user_id,
                # Приватный чат бота с пользователем в Telegram — это
                # ВСЕГДА его собственный numeric id, независимо от того,
                # начинал ли он уже диалог с ботом (см. owner_resolution.py
                # про разницу между "резолвнуть id" и "смочь доставить").
                telegram_chat_id=resolved.telegram_user_id,
                username=resolved.username or owner_username,
                start_date=start_date,
                end_date=end_date,
                owner_username_hint=owner_username,
                created_by_telegram_user_id=created_by_telegram_user_id,
                created_by_telegram_chat_id=created_by_telegram_chat_id,
            )
            sync_user_and_car(
                self._user_repository,
                telegram_user_id=resolved.telegram_user_id,
                username=resolved.username or owner_username,
                first_name=resolved.first_name,
                last_name=resolved.last_name,
                car_number=car_number,
            )
            claim_link = None
            pending = False
        else:
            subscription = self._create_or_refresh_pending_claim(
                task_id=task.id,
                car_number=car_number,
                owner_username_hint=owner_username,
                created_by_telegram_user_id=created_by_telegram_user_id,
                created_by_telegram_chat_id=created_by_telegram_chat_id,
                start_date=start_date,
                end_date=end_date,
            )
            claim_link = self._build_claim_link(subscription.claim_token)
            pending = True

        check_result = await self._check_service.check_task(task)

        return DelegatedAddCarOutcome(
            task=task,
            subscription=subscription,
            pending_claim=pending,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
            claim_link=claim_link,
        )

    async def add_delegated_car_without_client(
        self,
        *,
        created_by_telegram_user_id: int,
        created_by_telegram_chat_id: int,
        car_number: str,
        period_days: int,
        today: date,
    ) -> AddCarWithoutClientOutcome:
        """Trusted-оператор ставит машину на мониторинг БЕЗ указания
        клиента (см. design: "👤 Добавить Telegram клиента?" → "Отмена" —
        username клиента НЕ обязателен для постановки на мониторинг). НЕ
        создаёт НИКАКОЙ fine_monitoring_subscriptions строки — "без
        фиктивного owner/subscription" (см. design report про пересмотр
        архитектуры: fine_monitoring_tasks = source of truth, subscriptions
        = ТОЛЬКО access/delivery для реальных клиентов). Уведомление о
        найденных штрафах идёт ИСКЛЮЧИТЕЛЬНО через уже существующий
        operator notification mechanism (FineNotificationCoordinator,
        который срабатывает для ЛЮБОЙ задачи независимо от scope/
        subscriptions) — та же видимость, что и у обычной операторской
        машины, поставленной через "fine add". Видимость/действия самого
        trusted-оператора над этой задачей — через task-level admin API
        (list_all_active_tasks/check_now_task/stop_task_for_trusted_admin
        ниже), не через "Мои авто" по подписке.

        Если позже потребуется привязать реального клиента — это ОТДЕЛЬНАЯ
        независимая подписка на ТУ ЖЕ задачу мониторинга через обычный
        add_delegated_car() (архитектура уже поддерживает несколько
        подписок на одну машину) — никакой связи/миграции не требуется."""
        start_date = today
        end_date = today + timedelta(days=period_days)

        task = self._create_or_extend_task(
            car_number=car_number,
            telegram_user_id=created_by_telegram_user_id,
            telegram_chat_id=created_by_telegram_chat_id,
            start_date=start_date,
            end_date=end_date,
        )

        check_result = await self._check_service.check_task(task)

        return AddCarWithoutClientOutcome(
            task=task,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
        )

    def claim(
        self,
        claim_token: str,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> ClaimOutcome | None:
        """Обрабатывает "/start claim_<token>" — связывает pending_claim с
        РЕАЛЬНЫМ отправителем этого /start (event.sender_id и данные
        события, никогда не что-то из самого токена, см.
        FineSubscriptionRepository.claim). None — токен не найден, уже
        использован или истёк; вызывающий код (conversation.py) должен
        показать понятную ошибку. Не запускает повторную immediate-
        проверку штрафов — мониторинг уже идёт с момента delegated add_car,
        claim влияет только на то, кому теперь можно доставить уведомление."""
        subscription = self._subscription_repository.claim(
            claim_token,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
            now=datetime.now(timezone.utc),
        )
        if subscription is None:
            return None

        sync_user_and_car(
            self._user_repository,
            telegram_user_id=telegram_user_id,
            username=telegram_username,
            first_name=first_name,
            last_name=last_name,
            car_number=subscription.car_number,
        )

        return ClaimOutcome(subscription=subscription)

    def _build_claim_link(self, claim_token: str | None) -> str:
        return f"https://t.me/{self._bot_username}?start=claim_{claim_token}"

    def _create_or_refresh_pending_claim(
        self,
        *,
        task_id: int,
        car_number: str,
        owner_username_hint: str,
        created_by_telegram_user_id: int,
        created_by_telegram_chat_id: int,
        start_date: date,
        end_date: date,
    ) -> FineMonitoringSubscription:
        existing = self._subscription_repository.get_pending_claim_for_task_and_hint(
            task_id, owner_username_hint,
        )
        claim_token = generate_claim_token()
        claim_token_expires_at = datetime.now(timezone.utc) + _CLAIM_TOKEN_TTL

        if existing is not None:
            # Повторное "Добавить авто" тем же (или другим) trusted-
            # оператором на уже приглашённого, но ещё не claimed владельца —
            # продлеваем период и выпускаем свежий токен (старая ссылка
            # погашается автоматически: claim_token в БД перезаписывается,
            # прежнее значение больше нигде не совпадёт).
            return self._subscription_repository.refresh_pending_claim(
                existing.id, start_date=start_date, end_date=end_date,
                claim_token=claim_token, claim_token_expires_at=claim_token_expires_at,
            )

        try:
            return self._subscription_repository.create_pending_claim(
                monitoring_task_id=task_id, car_number=car_number,
                owner_username_hint=owner_username_hint,
                created_by_telegram_user_id=created_by_telegram_user_id,
                created_by_telegram_chat_id=created_by_telegram_chat_id,
                start_date=start_date, end_date=end_date,
                claim_token=claim_token, claim_token_expires_at=claim_token_expires_at,
            )
        except DuplicatePendingClaimError:
            # Гонка между get_pending_claim_for_task_and_hint() и create_
            # pending_claim() — тот же приём, что и в
            # _create_or_update_subscription/FineCheckService.
            refreshed = self._subscription_repository.get_pending_claim_for_task_and_hint(
                task_id, owner_username_hint,
            )
            if refreshed is None:
                raise
            return self._subscription_repository.refresh_pending_claim(
                refreshed.id, start_date=start_date, end_date=end_date,
                claim_token=claim_token, claim_token_expires_at=claim_token_expires_at,
            )

    # ---- общее ----

    def _create_or_extend_task(
        self,
        *,
        car_number: str,
        telegram_user_id: int,
        telegram_chat_id: int,
        start_date: date,
        end_date: date,
    ) -> FineMonitoringTask:
        existing = self._task_repository.get_active_by_car_number(car_number)
        if not existing:
            return self._task_repository.create(
                car_number=car_number,
                label=None,
                start_date=start_date,
                end_date=end_date,
                telegram_chat_id=telegram_chat_id,
                created_by_user_id=telegram_user_id,
                monitoring_scope="client_bot",
            )

        # Задача уже существует (операторская ИЛИ заведённая ранее другим
        # или этим же клиентом/trusted-оператором) — см. design про
        # "минимальную и чистую scheduling-модель": НЕ reset_period() (не
        # сбрасываем чужой период), НЕ трогаем monitoring_scope (операторская
        # остаётся операторской, клиентская — клиентской), только
        # продлеваем период, если новый end_date позже уже сохранённого —
        # никогда не сокращаем. Обычно у номера ровно одна активная
        # задача (то же допущение, что и в reader/commands/fine.py) —
        # берём первую. Один и тот же путь для self-service и delegated —
        # task creation/extension не зависит от того, кто в итоге окажется
        # владельцем-подписчиком.
        return self._task_repository.extend_period_if_shorter(existing[0].id, end_date)

    def _create_or_update_subscription(
        self,
        *,
        task_id: int,
        car_number: str,
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str,
        start_date: date,
        end_date: date,
        owner_username_hint: str | None = None,
        created_by_telegram_user_id: int | None = None,
        created_by_telegram_chat_id: int | None = None,
    ) -> FineMonitoringSubscription:
        existing = self._subscription_repository.get_active_for_user_and_car(
            telegram_user_id, car_number, today=start_date,
        )
        if existing is not None:
            # Повторное "Добавить авто" на ту же машину для того же
            # владельца — обновляем период существующей подписки, дубль не
            # создаём. created_by_*/owner_username_hint НЕ переписываются
            # повторным продлением (см. update_period) — атрибуция
            # "кто и для кого" фиксируется один раз, при первом создании.
            return self._subscription_repository.update_period(
                existing.id, start_date=start_date, end_date=end_date,
            )

        try:
            return self._subscription_repository.create(
                monitoring_task_id=task_id,
                car_number=car_number,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_username=username,
                start_date=start_date,
                end_date=end_date,
                source=_SOURCE,
                owner_username_hint=owner_username_hint,
                created_by_telegram_user_id=created_by_telegram_user_id,
                created_by_telegram_chat_id=created_by_telegram_chat_id,
            )
        except DuplicateActiveSubscriptionError:
            # Гонка между get_active_for_user_and_car() и create() (тот же
            # приём, что и FineCheckService/IntegrityError на detected_fines,
            # см. reader/fines/check_service.py) — кто-то другой (повторное
            # нажатие/два параллельных апдейта) уже создал активную подписку
            # для этой же пары — обновляем её, а не падаем.
            refreshed = self._subscription_repository.get_active_for_user_and_car(
                telegram_user_id, car_number, today=start_date,
            )
            if refreshed is None:
                raise
            return self._subscription_repository.update_period(
                refreshed.id, start_date=start_date, end_date=end_date,
            )

    def list_my_cars(self, telegram_user_id: int) -> list[FineMonitoringSubscription]:
        """ВСЕ подписки этого telegram_user_id, любого статуса — "Мои авто"
        (см. reader/public_bot/texts.py::format_my_cars). Никогда не
        возвращает чужие подписки: фильтр по telegram_user_id — на уровне
        SQL (см. FineSubscriptionRepository.list_by_user), не постфильтром
        в Python."""
        return self._subscription_repository.list_by_user(telegram_user_id)

    def list_managed_cars(self, created_by_telegram_user_id: int) -> list[FineMonitoringSubscription]:
        """Delegated-подписки, заведённые ЭТИМ trusted-оператором для
        других людей — отдельно от list_my_cars (см. design report: trusted-
        режим не даёт доступа ко ВСЕМ подпискам системы, только к тем, что
        оператор создал сам)."""
        return self._subscription_repository.list_managed_by_creator(created_by_telegram_user_id)

    def stop_subscription(self, subscription_id: int, *, telegram_user_id: int) -> bool:
        """Останавливает подписку, если telegram_user_id — её владелец
        ИЛИ trusted-оператор, создавший её как delegated (см.
        FineSubscriptionRepository.stop_by_owner_or_creator)."""
        return self._subscription_repository.stop_by_owner_or_creator(
            subscription_id, telegram_user_id=telegram_user_id,
        )

    # ---- 🔎 Проверить сейчас / ⛔ Остановить мониторинг (см. design report
    # Stage 4, раздел "UI completion") ----

    def list_actionable_subscriptions(
        self, telegram_user_id: int, *, today: date,
    ) -> list[FineMonitoringSubscription]:
        """Подписки, с которыми этот пользователь может действовать через
        🔎/⛔ — свои (owner) плюс delegated, которые он создал (creator),
        в статусе active/pending_claim и ещё не истёкшие. Дедуп по id — та
        же строка МОЖЕТ оказаться и "своей", и "созданной им" одновременно
        (trusted-оператор поставил машину на мониторинг для самого себя)."""
        own = self._subscription_repository.list_by_user(telegram_user_id)
        managed = self._subscription_repository.list_managed_by_creator(telegram_user_id)

        seen_ids = {s.id for s in own}
        combined = own + [s for s in managed if s.id not in seen_ids]

        return [
            s for s in combined
            if s.status in _ACTIONABLE_STATUSES and s.end_date >= today
        ]

    def get_actionable_subscription(
        self, subscription_id: int, *, telegram_user_id: int,
    ) -> FineMonitoringSubscription | None:
        """None, если подписка не существует ИЛИ telegram_user_id не её
        владелец и не создавший её trusted-оператор — единственная
        server-side проверка владения перед 🔎/⛔ (см. design report:
        "никаких действий с чужими subscriptions по callback payload").
        callback_data несёт только subscription_id (публичный, не секрет) —
        доказательством авторизации служит ИСКЛЮЧИТЕЛЬНО этот запрос к БД,
        а не сам факт, что id пришёл в правильном формате."""
        subscription = self._subscription_repository.get(subscription_id)
        if subscription is None:
            return None
        if telegram_user_id in (subscription.telegram_user_id, subscription.created_by_telegram_user_id):
            return subscription
        return None

    async def check_now(
        self, subscription_id: int, *, telegram_user_id: int,
    ) -> CheckNowOutcome | None:
        """None — подписка не найдена/не принадлежит/не создана этим
        пользователем (см. get_actionable_subscription) — вызывающий код
        должен показать "недоступно", ничего не проверяя.

        Иначе — ТОТ ЖЕ FineCheckService.check_task(), что и у фонового
        FineJob/ClientFineJob и у self-service/delegated add_car — никакой
        отдельной реализации проверки/обхода дедупа (см. явное требование
        задачи). flush_pending() здесь намеренно НЕ вызывается — та же
        причина, что и в add_car()/add_delegated_car(): NotificationFlushJob
        (см. reader/jobs/notification_flush_job.py) в main-процессе
        подхватит результат в течение ~30 секунд, без риска double-send
        между процессами."""
        subscription = self.get_actionable_subscription(subscription_id, telegram_user_id=telegram_user_id)
        if subscription is None:
            return None

        task = self._task_repository.get(subscription.monitoring_task_id)
        if task is None:
            return None

        check_result = await self._check_service.check_task(task)

        return CheckNowOutcome(
            car_number=subscription.car_number,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
        )

    # ---- trusted-operator task-level admin (см. design report: пересмотр
    # архитектуры — fine_monitoring_tasks = source of truth автомобилей/
    # monitoring jobs, subscriptions = ТОЛЬКО access/delivery для реальных
    # клиентов). Авторизация ("is_trusted") — ИСКЛЮЧИТЕЛЬНО ответственность
    # вызывающего кода (ConversationController._is_trusted(), сверяет
    # numeric event.sender_id с config public_bot.trusted_operator_user_ids
    # заново на КАЖДОМ вызове) — ни один метод здесь сам эту проверку не
    # делает и не имеет доступа к конфигу trusted-списка. task_id,
    # приходящий из callback_data, — публичный идентификатор, НЕ
    # доказательство авторизации сам по себе (тот же принцип, что и у
    # subscription_id для 🔎/⛔ выше) — get_active_task_for_trusted_admin()
    # обязателен перед любым действием. ----

    def list_all_active_tasks(self) -> list[FineMonitoringTask]:
        """ВСЕ активные задачи мониторинга — операторские И клиентские,
        с subscription и без неё (см. design report) — полный аналог уже
        существующего reader/commands/fine.py::_handle_list
        (FineMonitoringTaskRepository.list_active(), тот же метод, никакой
        второй реализации). reader/commands/fine.py/FineJob/monitoring
        scopes/dedup этим не затрагиваются — только чтение."""
        return self._task_repository.list_active()

    def get_active_task_for_trusted_admin(self, task_id: int) -> FineMonitoringTask | None:
        """None — задача не существует ИЛИ уже не 'active' (завершена/
        остановлена) — единственная server-side проверка СУЩЕСТВОВАНИЯ и
        АКТУАЛЬНОСТИ задачи перед task-level 🔎/⛔ (см. design report:
        "task_id считать только идентификатором, не authorization").
        Проверку самого is_trusted() этот метод НЕ делает — она уже должна
        быть сделана вызывающим кодом ДО обращения сюда (см. докстрок
        класса выше)."""
        task = self._task_repository.get(task_id)
        if task is None or task.status != "active":
            return None
        return task

    def count_active_or_pending_subscribers_for_task(self, task_id: int) -> int:
        """Сколько ещё actionable (active/pending_claim) client-подписок у
        этой задачи — используется ПЕРЕД показом подтверждения ⛔, чтобы
        честно предупредить trusted-оператора, что остановка затронет
        клиента(ов), а не только его самого (см. design report)."""
        return self._subscription_repository.count_active_or_pending_for_task(task_id)

    async def check_now_task(self, task_id: int) -> CheckNowOutcome | None:
        """Task-level 🔎 Проверить сейчас для trusted-оператора — БЕЗ
        привязки к subscription вовсе (см. design report: "наличие
        fine_monitoring_subscription для trusted operator НЕ требуется").
        None — задача не существует/не активна (см.
        get_active_task_for_trusted_admin). Иначе — ТОТ ЖЕ
        FineCheckService.check_task()/дедуп, что и везде — никакой
        отдельной реализации проверки."""
        task = self.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return None

        check_result = await self._check_service.check_task(task)

        return CheckNowOutcome(
            car_number=task.car_number,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
        )

    def stop_task_for_trusted_admin(self, task_id: int) -> bool:
        """Task-level ⛔ Остановить мониторинг для trusted-оператора —
        останавливает саму задачу (тот же task_repository.set_status(...,
        "stopped"), что и у reader/commands/fine.py::_handle_stop — не
        новая семантика lifecycle), а НЕ отдельную subscription.

        Одновременно останавливает ВСЕ ещё actionable (active/
        pending_claim) client-подписки этой задачи (см.
        FineSubscriptionRepository.stop_all_for_task) — см. design report:
        "не оставляй после этого misleading active subscriptions" — клиент
        не должен продолжать видеть "✅ Активен" в "Мои авто" для задачи,
        которую только что остановил trusted-оператор. Переиспользует уже
        существующий статус 'stopped' (тот же путь отображения, что и при
        обычном user-initiated Stop) — новой колонки/статуса не требуется.

        False — задача не существует/уже не активна (см.
        get_active_task_for_trusted_admin) — вызывающий код не должен
        показывать "остановлено" в этом случае."""
        task = self.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return False

        self._task_repository.set_status(task_id, "stopped")
        self._subscription_repository.stop_all_for_task(task_id)
        return True
