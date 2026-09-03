"""SubscriptionService — application/service layer между Add Car conversation
(reader/public_bot/conversation.py) и уже существующим Fine Monitor.

Ничего не дублирует из reader/fines/*: task_repository и check_service —
ТЕ ЖЕ САМЫЕ объекты, что использует и операторский FineJob/FineCommand (см.
reader/public_bot/main.py — сборка). Никакой отдельной реализации
provider/check-логики здесь нет.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineMonitoringTask
from reader.fines.task_repository import FineMonitoringTaskRepository
from reader.public_bot.car_owner_sync import sync_user_and_car
from reader.public_bot.models import FineMonitoringSubscription
from reader.public_bot.subscription_repository import (
    DuplicateActiveSubscriptionError,
    FineSubscriptionRepository,
)
from reader.users.repository import UserRepository

_SOURCE = "geshtrafbot"


@dataclass(frozen=True)
class AddCarOutcome:
    """Результат Add Car flow (см. SubscriptionService.add_car) — ok=False
    только когда сама проверка штрафов завершилась ошибкой (тот же смысл,
    что и у reader/commands/fine.py::_ImmediateCheckOutcome), не когда
    штрафов просто не нашлось (тогда ok=True, new_fines_count=0)."""

    task: FineMonitoringTask
    subscription: FineMonitoringSubscription
    check_ok: bool
    new_fines_count: int


class SubscriptionService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        subscription_repository: FineSubscriptionRepository,
        user_repository: UserRepository,
        check_service: FineCheckService,
    ):
        self._task_repository = task_repository
        self._subscription_repository = subscription_repository
        self._user_repository = user_repository
        self._check_service = check_service

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
        # вызывается (см. Stage 2 report) — новый штраф уйдёт оператору на
        # ближайшем плановом запуске FineJob/ArchiveFineJob, дедуп при этом
        # не страдает: сам факт обнаружения уже сохранён в detected_fines.
        check_result = await self._check_service.check_task(task)

        return AddCarOutcome(
            task=task,
            subscription=subscription,
            check_ok=check_result.status == "ok",
            new_fines_count=len(check_result.new_fines) if check_result.status == "ok" else 0,
        )

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
        # или этим же клиентом) — см. design про "минимальную и чистую
        # scheduling-модель": НЕ reset_period() (не сбрасываем чужой период),
        # НЕ трогаем monitoring_scope (операторская остаётся операторской,
        # клиентская — клиентской), только продлеваем период, если новый
        # end_date позже уже сохранённого — никогда не сокращаем. Обычно у
        # номера ровно одна активная задача (то же допущение, что и в
        # reader/commands/fine.py) — берём первую.
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
    ) -> FineMonitoringSubscription:
        existing = self._subscription_repository.get_active_for_user_and_car(
            telegram_user_id, car_number, today=start_date,
        )
        if existing is not None:
            # Повторное "Добавить авто" тем же человеком для той же машины —
            # обновляем период существующей подписки, дубль не создаём.
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
