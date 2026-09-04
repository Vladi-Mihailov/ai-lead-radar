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


_CLAIM_TOKEN_TTL = timedelta(days=7)


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
