"""
Тесты reader/turkey_bot_test/* — изолированный экспериментальный clone
production Turkey-бота (см. design report "изолированный experimental
clone Turkey bot"). Реальная сеть/Telegram/CAPTCHA здесь не используются —
только (1) структурные проверки полной изоляции runtime от production
(token/session/DB/классы) и (2) минимальные happy-path тесты GIB/Avrasya/
KGM через ConversationController клона, доказывающие, что клон
функционально идентичен и работает end-to-end независимо от production
reader/turkey_bot/*.

CAPTCHA — по-прежнему ТОЛЬКО human-in-the-loop (фейковый provider здесь
просто возвращает заранее заданный результат, ничего не решает и не
обходит), тот же принцип, что и в tests/test_turkey_bot_conversation.py.
"""

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.settings import ConfigError, load_settings  # noqa: E402

# ---- Production reader/turkey_bot/* (для сравнения — НЕ используется для
# запуска чего-либо, только чтобы доказать, что клон — ОТДЕЛЬНЫЕ модули/
# классы/пути, а не переиспользование production через alias/re-export). ----
import reader.turkey_bot.main as prod_main  # noqa: E402
from reader.turkey_bot.live_session_registry import (  # noqa: E402
    LiveGibSessionRegistry as ProdLiveGibSessionRegistry,
)

# ---- Test-clone reader/turkey_bot_test/* ----
import reader.turkey_bot_test.main as test_main  # noqa: E402
from reader.turkey_bot_test.avrasya.live_session_registry import (  # noqa: E402
    LiveAvrasyaSessionRegistry,
)
from reader.turkey_bot_test.avrasya.models import (  # noqa: E402
    AvrasyaCaptchaChallenge,
    AvrasyaSubmitOutcome,
)
from reader.turkey_bot_test.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot_test.conversation import ConversationController  # noqa: E402
from reader.turkey_bot_test.conversation_state_repository import (  # noqa: E402
    TurkeyConversationStateRepository,
)
from reader.turkey_bot_test.gib.models import CaptchaChallenge, GibSubmitOutcome  # noqa: E402
from reader.turkey_bot_test.kgm.live_session_registry import (  # noqa: E402
    LiveKgmSessionRegistry,
)
from reader.turkey_bot_test.kgm.models import (  # noqa: E402
    KgmCaptchaChallenge,
    KgmDebtItem,
    KgmOperatorResult,
    KgmSubmitOutcome,
)
from reader.turkey_bot_test.known_users_repository import (  # noqa: E402
    TurkeyBotKnownUsersRepository,
)
from reader.turkey_bot_test.live_session_registry import (  # noqa: E402
    LiveGibSessionRegistry,
)
from reader.turkey_bot_test.statistics_service import TurkeyStatisticsService  # noqa: E402
from reader.turkey_bot_test.toll_check_repository import (  # noqa: E402
    TurkeyTollCheckRepository,
)
from reader.turkey_bot_test.user_cars_repository import (  # noqa: E402
    TurkeyUserCarsRepository,
)

_CHAT_ID = 111
_USER_ID = 222


# ---- 1. Полная изоляция runtime от production (token/session/DB/классы) ----


def test_test_clone_uses_its_own_token_env_variable(monkeypatch):
    """См. design report п.3: TURKEYBOT_TEST_TOKEN, НЕ TURKEYBOT_TOKEN."""
    monkeypatch.delenv("TURKEYBOT_TOKEN", raising=False)
    monkeypatch.setenv("TURKEYBOT_TEST_TOKEN", "test-token-value")

    assert test_main.read_bot_token() == "test-token-value"


def test_test_clone_never_falls_back_to_production_token(monkeypatch):
    """Явное требование задачи: "Test bot НИКОГДА не должен fallback'иться
    на production TURKEYBOT_TOKEN" — даже когда production TURKEYBOT_TOKEN
    реально задан в окружении, отсутствие TURKEYBOT_TEST_TOKEN должно
    приводить к fail-fast ошибке, а не к тихому использованию production
    токена."""
    monkeypatch.setenv("TURKEYBOT_TOKEN", "production-token-should-not-be-used")
    monkeypatch.delenv("TURKEYBOT_TEST_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        test_main.read_bot_token()


def test_test_clone_db_path_differs_from_production_db_path():
    """См. design report п.5 — ОТДЕЛЬНЫЙ SQLite-файл, НЕ
    settings.app.users_db_file (production, общий с Георгия-ботом)."""
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")

    assert test_main._DB_PATH != settings.app.users_db_file
    assert test_main._DB_PATH.name == "turkey_bot_test.db"


def test_test_clone_session_path_differs_from_production_session_path():
    """См. design report п.4 — ОТДЕЛЬНЫЙ .session-файл."""
    assert test_main._SESSION_PATH != prod_main._SESSION_PATH
    assert test_main._SESSION_PATH.name == "turkey_bot_test"
    assert prod_main._SESSION_PATH.name == "turkeybot"


def test_production_and_test_entrypoints_are_independent_modules():
    """Клон — НАСТОЯЩАЯ отдельная копия (см. design report: "НЕ
    делай большой refactor... безопасная изоляция test clone"), а не
    re-export/alias production-модуля — доказывается тем, что это ДВА
    РАЗНЫХ объекта модуля и ДВА РАЗНЫХ класса реестра, а не один и тот же
    импортированный дважды."""
    assert test_main is not prod_main
    assert test_main.__file__ != prod_main.__file__
    assert LiveGibSessionRegistry is not ProdLiveGibSessionRegistry


def test_test_clone_repositories_do_not_touch_production_repository_state(tmp_path):
    """Прямое доказательство изоляции данных (см. design report п.5: "Test
    bot НЕ должен писать экспериментальные данные в production
    data/users.db") — запись через test-репозиторий на СВОЙ файл никак не
    отражается в ОТДЕЛЬНОМ файле, имитирующем production."""
    prod_like_db = tmp_path / "prod_like.db"
    test_like_db = tmp_path / "test_like.db"

    prod_repo = TurkeyCheckRepository(prod_like_db)
    test_repo = TurkeyCheckRepository(test_like_db)

    test_repo.record_result(
        telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate="34ABC123",
        captcha_attempts=1, status="no_debt", gib_message_text=None, raw_response=None,
    )

    assert test_repo.count_total() == 1
    assert prod_repo.count_total() == 0

    prod_repo.close()
    test_repo.close()


# ---- 2. Happy-path через ConversationController клона (GIB/Avrasya/KGM) ----


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeGibProvider:
    def __init__(self, *, start_challenge, submit_results):
        self._start_challenge = start_challenge
        self._submit_results = list(submit_results)
        self.submit_calls: list[dict] = []

    async def start(self):
        return self._start_challenge

    async def refresh_captcha(self):
        return self._start_challenge

    async def submit(self, *, plate, image_id, captcha_code):
        self.submit_calls.append({"plate": plate, "image_id": image_id, "captcha_code": captcha_code})
        return self._submit_results.pop(0)


class _FakeAvrasyaProvider:
    def __init__(self, *, start_challenge, submit_results):
        self._start_challenge = start_challenge
        self._submit_results = list(submit_results)

    async def start(self):
        return self._start_challenge

    async def refresh_captcha(self):
        return self._start_challenge

    async def submit(self, *, plate, captcha_code):
        return self._submit_results.pop(0)


class _FakeKgmProvider:
    def __init__(self, *, start_challenge, submit_results):
        self._start_challenge = start_challenge
        self._submit_results = list(submit_results)

    async def start(self):
        return self._start_challenge

    async def refresh_captcha(self):
        return self._start_challenge

    async def submit(self, *, plate, captcha_code):
        return self._submit_results.pop(0)


def _make_test_clone_controller(*, check_factory=None, avrasya_check_factory=None, kgm_check_factory=None):
    states = TurkeyConversationStateRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    registry = LiveGibSessionRegistry()
    garage = TurkeyUserCarsRepository(":memory:")
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    statistics = TurkeyStatisticsService(known_users, checks)
    avrasya_registry = LiveAvrasyaSessionRegistry()
    toll_checks = TurkeyTollCheckRepository(":memory:")
    kgm_registry = LiveKgmSessionRegistry()

    kwargs = {}
    if check_factory is not None:
        kwargs["check_factory"] = check_factory
    if avrasya_check_factory is not None:
        kwargs["avrasya_check_factory"] = avrasya_check_factory
    if kgm_check_factory is not None:
        kwargs["kgm_check_factory"] = kgm_check_factory

    return ConversationController(
        states, checks, registry, garage, statistics, avrasya_registry, toll_checks, kgm_registry,
        **kwargs,
    )


async def test_gib_happy_path_works_through_test_clone_registry():
    provider = _FakeGibProvider(
        start_challenge=CaptchaChallenge(image_id="cid-1", image_png=b"GIB-PNG"),
        submit_results=[
            GibSubmitOutcome(kind="no_debt", messages=(), raw_data={}, fines=()),
        ],
    )

    def factory():
        return _FakeClient(), provider

    controller = _make_test_clone_controller(check_factory=factory)

    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.photo_png == b"GIB-PNG"

    reply = await controller.handle_text("g8fyx", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "34ABC123" in reply.text


async def test_avrasya_happy_path_works_through_test_clone_registry():
    provider = _FakeAvrasyaProvider(
        start_challenge=AvrasyaCaptchaChallenge(image_png=b"AVRASYA-PNG"),
        submit_results=[
            AvrasyaSubmitOutcome(kind="no_debt", status_code=404, messages=(), raw_data=None),
        ],
    )

    def avrasya_factory():
        return _FakeClient(), provider

    controller = _make_test_clone_controller(avrasya_check_factory=avrasya_factory)

    await controller.handle_toll_provider_callback("avrasya", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("A123AA123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.photo_png == b"AVRASYA-PNG"

    reply = await controller.handle_text("123456", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "A123AA123" in reply.text


async def test_kgm_happy_path_works_through_test_clone_registry():
    kgm_op = KgmOperatorResult(
        operator_key="kgm", operator_name="KGM",
        items=(
            KgmDebtItem(
                operator="kgm", date_time=datetime(2026, 9, 15, 9, 39, 48),  # noqa: DTZ001
                entry_station="HENDEK", exit_station="TOPAĞAÇ SGS", vehicle_class="1",
                base_toll=Decimal("40.00"), payable_amount=Decimal("40.00"),
                penalty_free_deadline=date(2026, 9, 30),
            ),
        ),
        subtotal=Decimal("40.00"),
    )
    outcome = KgmSubmitOutcome(
        kind="has_debt", operators=(kgm_op,),
        kgm_total=Decimal("40.00"), yid_total=Decimal(0), grand_total=Decimal("40.00"),
    )
    provider = _FakeKgmProvider(
        start_challenge=KgmCaptchaChallenge(image_png=b"KGM-PNG"),
        submit_results=[outcome],
    )

    def kgm_factory():
        return _FakeClient(), provider

    controller = _make_test_clone_controller(kgm_check_factory=kgm_factory)

    await controller.handle_toll_provider_callback("kgm", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    reply = await controller.handle_text("34ABC123", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert reply.photo_png == b"KGM-PNG"

    reply = await controller.handle_text("15 53893", chat_id=_CHAT_ID, telegram_user_id=_USER_ID)
    assert "HENDEK" in reply.text
    assert reply.cta_buttons is not None
