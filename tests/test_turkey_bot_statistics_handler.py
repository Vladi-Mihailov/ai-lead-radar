"""Тесты reader/turkey_bot/conversation.py::ConversationController.handle_statistics
(см. задачу "доработать 📊 Статистика Turkey bot") — сквозные, через
РЕАЛЬНЫЙ ConversationController (не изолированные texts.py/
statistics_service.py unit-тесты, см. tests/test_turkey_bot_texts_statistics.py
/tests/test_turkey_statistics_service.py) — покрывает orchestration
(get_debt_rows + get_known_profile + форматирование), authorization и,
КРИТИЧЕСКИ ВАЖНО, отсутствие любых provider/network вызовов."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot import texts
from reader.turkey_bot.conversation import ConversationController
from reader.turkey_bot.conversation_state_repository import (
    TurkeyConversationStateRepository,
)
from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService
from reader.turkey_bot.unified.models import (
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
    derive_overall_status,
    total_amount_for,
)
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235
_TZ = ZoneInfo("UTC")


class _Fixture:
    def __init__(self, *, trusted_operator_user_ids=frozenset({_TRUSTED_ID})):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        # check_service=None — намеренно: handle_statistics НЕ должен
        # обращаться к нему вообще (см. задачу "КРИТИЧЕСКИ ВАЖНО: НИКАКИХ
        # LIVE CHECK") — если бы он это сделал, тест упал бы с
        # AttributeError('NoneType' object has no attribute ...), а не
        # тихо прошёл.
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, check_service=None,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids), tz=_TZ,
        )

    def add_car(self, *, telegram_user_id: int, car_number: str) -> None:
        self.garage.add_car(telegram_user_id=telegram_user_id, car_number=car_number)

    def record_known(
        self, *, telegram_user_id: int, username: str | None = None,
        first_name: str | None = None, last_name: str | None = None,
    ) -> None:
        self.known_users.record_seen(
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
            first_name=first_name, last_name=last_name,
        )

    def save_run(
        self, *, telegram_user_id: int, plate: str, providers: tuple[ProviderCheckResult, ...],
        days_ago: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc)
        result = UnifiedCheckResult(
            plate=plate, started_at=now, finished_at=now,
            overall_status=derive_overall_status(providers), total_amount=total_amount_for(providers),
            providers=providers,
        )
        run_id = self.runs.save(
            result, telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, initiator="manual",
        )
        finished_at = (now - timedelta(days=days_ago)).isoformat()
        self.runs._conn.execute("UPDATE turkey_check_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
        self.runs._conn.commit()


def _provider(status: ProviderStatus, amount: Decimal, *, provider: str = "gib") -> ProviderCheckResult:
    return ProviderCheckResult(
        provider=provider, status=status, debt_count=1 if status == ProviderStatus.HAS_DEBT else 0,
        principal_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        penalty_amount=Decimal(0), total_amount=amount if status == ProviderStatus.HAS_DEBT else Decimal(0),
        items=(), error_type="transport_error" if status == ProviderStatus.ERROR else None,
        checked_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def fx():
    return _Fixture()


# ---- authorization ----


async def test_ordinary_user_cannot_open_statistics(fx):
    """См. handle_text: STATISTICS_LABEL требует self._is_trusted(...) —
    для обычного пользователя условие ложно, текст трактуется как
    попытка ввести номер (тот же безопасный fallback, что и у
    STOP_MONITORING_LABEL/SEARCH_LABEL, см. задачу п.12: "не менять
    существующие trusted checks") — никакой статистики/задолженности не
    раскрывается."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.record_known(telegram_user_id=777, username="alenaogir")
    fx.save_run(telegram_user_id=777, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(500)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)

    assert "Пользователи" not in reply.text
    assert "Задолженность" not in reply.text
    assert "@alenaogir" not in reply.text
    assert "M295YB196" not in reply.text
    assert not reply.extra_texts


async def test_trusted_user_opens_statistics(fx):
    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert texts.STATISTICS_LABEL in reply.text
    assert "👥 Пользователи" in reply.text
    assert "🚨 Задолженность по последней проверке" in reply.text


# ---- no network/provider calls (see class docstring re: check_service=None) ----


async def test_statistics_does_not_call_check_service(fx):
    """См. фикстуру _Fixture — check_service=None. Если бы handle_statistics
    когда-либо обращался к нему (GİB/Avrasya/KGM/unified check), этот вызов
    упал бы с AttributeError, а не молча прошёл."""
    fx.add_car(telegram_user_id=777, car_number="M295YB196")
    fx.save_run(telegram_user_id=777, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(500)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert "M295YB196" in "".join((reply.text, *reply.extra_texts))


# ---- username list removed ----


async def test_statistics_no_longer_lists_individual_usernames(fx):
    fx.record_known(telegram_user_id=111, username="alenaogir")
    fx.record_known(telegram_user_id=222, username="Mihailov_vm")

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "@alenaogir" not in full_text
    assert "@Mihailov_vm" not in full_text


# ---- owner display variants (см. задачу п.7) ----


async def test_debt_owner_shown_with_name_and_username(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, username="ivanov_i", first_name="Иван", last_name="Иванов")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(2740)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "Иван Иванов (@ivanov_i)" in full_text


async def test_debt_owner_shown_with_name_only(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, first_name="Иван", last_name="Иванов")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(750)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "🚗 M295YB196 — Иван Иванов — 750 ₺" in full_text


async def test_debt_owner_shown_with_username_only(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    fx.record_known(telegram_user_id=1, username="alenaogir")
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(1250)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "🚗 M295YB196 — @alenaogir — 1 250 ₺" in full_text


async def test_debt_owner_shown_as_dash_when_neither(fx):
    fx.add_car(telegram_user_id=1, car_number="M295YB196")
    # record_known НЕ вызывается вовсе.
    fx.save_run(telegram_user_id=1, plate="M295YB196", providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(100)),))

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    full_text = "\n".join((reply.text, *reply.extra_texts))
    assert "🚗 M295YB196 — — — 100 ₺" in full_text
    assert "None" not in full_text


# ---- Telegram message limit (см. задачу п.11) ----


async def test_large_debt_list_splits_into_multiple_messages_without_truncation():
    fx = _Fixture()
    for i in range(500):
        fx.add_car(telegram_user_id=1000 + i, car_number=f"P{i:04d}AA01")
        fx.record_known(telegram_user_id=1000 + i, username=f"user{i}")
        fx.save_run(
            telegram_user_id=1000 + i, plate=f"P{i:04d}AA01",
            providers=(_provider(ProviderStatus.HAS_DEBT, Decimal(100 + i)),),
        )

    reply = await fx.controller.handle_text(texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID)

    assert len(reply.extra_texts) > 1
    for message in (reply.text, *reply.extra_texts):
        assert len(message) <= 4096

    full_text = "\n".join((reply.text, *reply.extra_texts))
    for i in range(500):
        assert f"P{i:04d}AA01" in full_text  # никто не потерян/не обрезан
    assert "Автомобилей: 500" in reply.text
