"""@ProtocolTRbot: "🛡 ОСАГО Турция" main-menu handoff to @OSAGOTRbot (see
task "add Turkey OSAGO handoff to ProtocolTRbot"). Mirrors
tests/test_public_bot_help_osago.py's OSAGO section for @ProtocolGEbot --
only a transition (cta_buttons with one inline URL button), no insurance
logic in this bot at all.

NOT to be confused with the pre-existing, unrelated _debt_cta_buttons()'s
"🚗 ОСАГО Турции" (reader/turkey_bot/conversation.py) -- a commercial CTA
shown under a debt-check result, linking to a human operator. That feature
is untouched by this change; see test_turkey_bot_conversation.py /
test_turkey_bot_handlers.py for its own tests."""

import pytest

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
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

pytestmark = pytest.mark.asyncio

_TRUSTED_ID = 5712994689
_ORDINARY_ID = 685137235


class _Fixture:
    def __init__(self, *, trusted_operator_user_ids=frozenset({_TRUSTED_ID})):
        self.states = TurkeyConversationStateRepository(":memory:")
        self.garage = TurkeyUserCarsRepository(":memory:")
        self.runs = TurkeyCheckRunRepository(":memory:")
        self.subscriptions = TurkeyMonitoringSubscriptionRepository(":memory:")
        self.known_users = TurkeyBotKnownUsersRepository(":memory:")
        self.statistics = TurkeyStatisticsService(self.known_users, self.runs, self.subscriptions)
        self.controller = ConversationController(
            self.states, self.garage, self.runs, self.subscriptions, self.statistics, check_service=None,
            trusted_operator_user_ids=frozenset(trusted_operator_user_ids),
        )


@pytest.fixture
def fx():
    return _Fixture()


# ---- exact constants ----


def test_osago_tr_label_text_url_are_exact():
    assert texts.OSAGO_TR_LABEL == "🛡 ОСАГО Турция"
    assert texts.OSAGO_TR_LINK_BUTTON_LABEL == "🚗 Оформить ОСАГО Турция"
    assert texts.OSAGO_TR_BOT_URL == "https://t.me/OSAGOTRbot?start=turkey_bot"
    assert "start=turkey_bot" in texts.OSAGO_TR_BOT_URL
    assert texts.OSAGO_TR_LINK_TEXT == (
        "🛡 ОСАГО Турция\n\nОформление страховки происходит в нашем страховом боте.\n\nНажмите кнопку ниже:"
    )


def test_help_menu_text_includes_the_osago_bullet():
    assert "• 🛡 ОСАГО Турция — оформить страховку для поездки по Турции." in texts.HELP_MENU_TEXT


# ---- selecting the button ----


async def test_selecting_osago_returns_single_inline_deep_link(fx):
    reply = await fx.controller.handle_text(
        texts.OSAGO_TR_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )

    assert reply.text == texts.OSAGO_TR_LINK_TEXT
    assert reply.cta_buttons == ((texts.OSAGO_TR_LINK_BUTTON_LABEL, texts.OSAGO_TR_BOT_URL),)
    assert reply.show_main_menu is False
    assert reply.show_georgian_bot_link is False


async def test_osago_works_for_trusted_operator_too(fx):
    reply = await fx.controller.handle_text(
        texts.OSAGO_TR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )

    assert reply.cta_buttons == ((texts.OSAGO_TR_LINK_BUTTON_LABEL, texts.OSAGO_TR_BOT_URL),)


# ---- clears active conversation state ----


async def test_osago_clears_an_active_add_car_state(fx):
    from reader.turkey_bot.conversation import _STEP_AWAITING_NEW_CAR_PLATE

    fx.states.set(chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID, step=_STEP_AWAITING_NEW_CAR_PLATE)

    reply = await fx.controller.handle_text(
        texts.OSAGO_TR_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )

    assert reply.text == texts.OSAGO_TR_LINK_TEXT
    assert fx.states.get(_ORDINARY_ID) is None
    # Not swallowed as a plate -- no car was added.
    assert fx.garage.list_cars(_ORDINARY_ID) == []


async def test_osago_works_while_trusted_search_state_is_active(fx):
    from reader.turkey_bot.conversation import _STEP_AWAITING_SEARCH_QUERY

    fx.states.set(chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, step=_STEP_AWAITING_SEARCH_QUERY)

    reply = await fx.controller.handle_text(
        texts.OSAGO_TR_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )

    assert reply.cta_buttons == ((texts.OSAGO_TR_LINK_BUTTON_LABEL, texts.OSAGO_TR_BOT_URL),)
    assert fx.states.get(_TRUSTED_ID) is None


# ---- existing buttons remain unchanged ----


async def test_existing_menu_buttons_are_unaffected(fx):
    georgian = await fx.controller.handle_text(
        texts.GEORGIAN_BOT_LINK_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert georgian.text == texts.GEORGIAN_BOT_LINK_TEXT
    assert georgian.show_georgian_bot_link is True

    help_reply = await fx.controller.handle_text(
        texts.HELP_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert help_reply.text == texts.HELP_MENU_TEXT
    assert help_reply.help_keyboard == "menu"

    add_car = await fx.controller.handle_text(
        texts.ADD_CAR_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )
    assert add_car.text == texts.ASK_PLATE_FOR_NEW_CAR_TEXT

    start = await fx.controller.handle_text("/start", chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID)
    assert start.text == texts.WELCOME_TEXT and start.show_main_menu is True


async def test_trusted_search_and_statistics_are_unaffected(fx):
    search = await fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )
    assert search.search_prompt is True

    stats = await fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID,
    )
    assert stats is not None


async def test_ordinary_user_does_not_get_trusted_only_buttons(fx):
    fallback_search = await fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_ORDINARY_ID, telegram_user_id=_ORDINARY_ID,
    )
    # SEARCH_LABEL for a non-trusted user falls through like any other text
    # (same as before this change -- not a plate recognizable as a car, but
    # routing-wise it is NOT handle_search_start).
    assert fallback_search is not None
