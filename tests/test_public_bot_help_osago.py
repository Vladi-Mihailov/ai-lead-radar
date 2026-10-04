"""@ProtocolGEbot: "ℹ️ Справка" и "🛡 ОСАГО Грузия" в главном меню.

"🛡 ОСАГО Грузия" — только переход (inline URL-кнопка) в @OSAGO24GEbot с
deep-link payload georgia_bot; никакой логики страхования в этом боте нет.
Обе кнопки — обычные пункты меню: распознаются как текст и, как и прочие
пункты меню, имеют приоритет над текущим шагом диалога."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from test_public_bot_conversation import _TRUSTED_ID, _Fixture
from test_public_bot_handlers import (
    _button_labels,
    _button_urls,
    _FakeEvent,
    _FakeReply,
)

from reader.public_bot import texts
from reader.public_bot.conversation import (
    STEP_AWAITING_CAR_NUMBER,
    STEP_AWAITING_SEARCH_QUERY,
    STEP_PROTOCOL_CHECK_METHOD,
)
from reader.public_bot.handlers import _send_reply
from reader.public_bot.keyboards import main_menu_keyboard

_NEW_LABELS = [texts.HELP_LABEL, texts.OSAGO_LABEL]


@pytest.fixture
def fx(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


@pytest.fixture
def trusted_fx(tmp_path):
    fixture = _Fixture(tmp_path, trusted_operator_user_ids={_TRUSTED_ID})
    yield fixture
    fixture.close()


def _rows(keyboard) -> list[list[str]]:
    return [[b.button.text for b in row] for row in keyboard]


# ---- menu ----


@pytest.mark.parametrize("is_trusted", [False, True], ids=["ordinary", "trusted"])
def test_menu_has_help_and_osago_as_last_row(is_trusted):
    rows = _rows(main_menu_keyboard(is_trusted=is_trusted))
    assert rows[-1] == _NEW_LABELS
    # Существующие кнопки на месте (по одному разу), новые не дублируются.
    labels = [label for row in rows for label in row]
    for label in (texts.ADD_CAR_LABEL, texts.MY_CARS_LABEL, texts.CHECK_NOW_LABEL,
                  texts.TURKEY_BOT_LINK_LABEL, *_NEW_LABELS):
        assert labels.count(label) == 1
    assert texts.PROTOCOL_CHECK_LABEL not in labels  # hidden from the menu
    assert (texts.STATISTICS_LABEL in labels) is is_trusted
    assert (texts.SEARCH_LABEL in labels) is is_trusted


def test_new_labels_are_exact_and_distinct_from_existing_ones():
    assert texts.HELP_LABEL == "ℹ️ Справка"
    assert texts.OSAGO_LABEL == "🛡 ОСАГО Грузия"
    existing = {texts.ADD_CAR_LABEL, texts.MY_CARS_LABEL, texts.CHECK_NOW_LABEL, texts.STOP_LABEL,
                texts.TURKEY_BOT_LINK_LABEL, texts.STATISTICS_LABEL, texts.SEARCH_LABEL,
                texts.PROTOCOL_CHECK_LABEL}
    assert not existing & set(_NEW_LABELS)


# ---- help ----


async def test_help_shows_help_text_with_main_menu(fx):
    reply = await fx.controller.handle_text(texts.HELP_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    assert reply.text == texts.HELP_TEXT
    assert reply.show_main_menu is True
    assert reply.cta_buttons is None


def test_help_text_covers_every_section():
    assert texts.HELP_TEXT.startswith("ℹ️ Справка\n\nВ боте можно:")
    for section in (texts.ADD_CAR_LABEL, texts.MY_CARS_LABEL, texts.CHECK_NOW_LABEL,
                    texts.TURKEY_BOT_LINK_LABEL, texts.OSAGO_LABEL, "@ProtocolTRbot"):
        assert section in texts.HELP_TEXT
    assert texts.PROTOCOL_CHECK_LABEL not in texts.HELP_TEXT  # hidden feature: not advertised
    assert texts.HELP_TEXT == (
        "ℹ️ Справка\n\n"
        "В боте можно:\n\n"
        "• ➕ Добавить авто — сохранить автомобиль и получать уведомления о новых штрафах.\n"
        "• 📋 Мои авто — посмотреть сохранённые автомобили, включить или выключить мониторинг.\n"
        "• 🔎 Проверить сейчас — проверить штрафы прямо сейчас.\n"
        "• 🇹🇷 Штрафы Турции — штрафы и платные дороги Турции в нашем боте @ProtocolTRbot.\n"
        "• 🛡 ОСАГО Грузия — оформить страховку для поездки по Грузии."
    )


async def test_help_works_for_trusted_operator(trusted_fx):
    reply = await trusted_fx.controller.handle_text(
        texts.HELP_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert reply.text == texts.HELP_TEXT
    assert reply.show_main_menu is True


async def test_help_reply_renders_with_main_menu_keyboard():
    reply = _FakeReply(text=texts.HELP_TEXT, show_main_menu=True)
    event = _FakeEvent()

    await _send_reply(event, reply)

    (call,) = event.calls
    assert call["text"] == texts.HELP_TEXT
    assert [label for row in _rows(call["buttons"]) for label in row][-2:] == _NEW_LABELS


# ---- OSAGO deep link ----


async def test_osago_returns_single_inline_deep_link(fx):
    reply = await fx.controller.handle_text(texts.OSAGO_LABEL, chat_id=1, telegram_user_id=1, username="alice")

    assert reply.text == texts.OSAGO_LINK_TEXT
    assert reply.cta_buttons == [[(texts.OSAGO_LINK_BUTTON_LABEL, "https://t.me/OSAGO24GEbot?start=georgia_bot")]]
    assert reply.show_main_menu is False
    assert reply.show_turkey_bot_link is False


async def test_osago_works_for_trusted_operator(trusted_fx):
    reply = await trusted_fx.controller.handle_text(
        texts.OSAGO_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert reply.cta_buttons == [[(texts.OSAGO_LINK_BUTTON_LABEL, texts.OSAGO_BOT_URL)]]


@pytest.mark.parametrize("prefer_edit", [False, True])
async def test_osago_reply_renders_as_inline_url_button(prefer_edit):
    reply = _FakeReply(text=texts.OSAGO_LINK_TEXT)
    reply.cta_buttons = [[(texts.OSAGO_LINK_BUTTON_LABEL, texts.OSAGO_BOT_URL)]]
    event = _FakeEvent()

    await _send_reply(event, reply, prefer_edit=prefer_edit)

    (call,) = event.calls
    assert call["text"] == texts.OSAGO_LINK_TEXT
    assert _button_labels(call["buttons"]) == [texts.OSAGO_LINK_BUTTON_LABEL]
    assert _button_urls(call["buttons"]) == {"https://t.me/OSAGO24GEbot?start=georgia_bot"}


# ---- menu labels win over an active conversation step ----


@pytest.mark.parametrize("label", _NEW_LABELS)
@pytest.mark.parametrize("step", [STEP_AWAITING_CAR_NUMBER, STEP_PROTOCOL_CHECK_METHOD])
async def test_new_labels_win_over_active_step_and_clear_it(fx, label, step):
    fx.conversation_state_repository.set(chat_id=1, telegram_user_id=1, step=step)

    reply = await fx.controller.handle_text(label, chat_id=1, telegram_user_id=1, username="alice")

    assert reply.text in (texts.HELP_TEXT, texts.OSAGO_LINK_TEXT)
    assert fx.conversation_state_repository.get(1) is None
    # Не принято за госномер: ни одной новой подписки.
    assert fx.subscription_repository.list_by_user(1) == []


@pytest.mark.parametrize("label", _NEW_LABELS)
async def test_new_labels_win_over_trusted_search_step(trusted_fx, label):
    trusted_fx.conversation_state_repository.set(
        chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, step=STEP_AWAITING_SEARCH_QUERY,
    )

    reply = await trusted_fx.controller.handle_text(
        label, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )

    assert reply.text in (texts.HELP_TEXT, texts.OSAGO_LINK_TEXT)
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID) is None


async def test_existing_menu_buttons_are_unchanged(fx):
    """Существующие пункты меню отвечают как раньше."""
    turkey = await fx.controller.handle_text(texts.TURKEY_BOT_LINK_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert turkey.text == texts.TURKEY_BOT_LINK_TEXT and turkey.show_turkey_bot_link is True

    add_car = await fx.controller.handle_text(texts.ADD_CAR_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert add_car.text == texts.CAR_NUMBER_PROMPT
    assert fx.conversation_state_repository.get(1).step == STEP_AWAITING_CAR_NUMBER

    protocol = await fx.controller.handle_text(texts.PROTOCOL_CHECK_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert protocol.text == texts.PROTOCOL_CHECK_INTRO_TEXT
    assert fx.conversation_state_repository.get(1).step == STEP_PROTOCOL_CHECK_METHOD

    my_cars = await fx.controller.handle_text(texts.MY_CARS_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert my_cars.text == texts.NO_CARS_TEXT

    check_now = await fx.controller.handle_text(texts.CHECK_NOW_LABEL, chat_id=1, telegram_user_id=1, username=None)
    assert check_now.text == texts.NO_ACTIONABLE_CARS_TEXT

    # trusted-only labels stay a safe fallback for an ordinary user
    for label in (texts.STATISTICS_LABEL, texts.SEARCH_LABEL):
        fallback = await fx.controller.handle_text(label, chat_id=1, telegram_user_id=1, username=None)
        assert fallback.text == texts.MAIN_MENU_TEXT and fallback.show_main_menu is True

    start = await fx.controller.handle_text("/start", chat_id=1, telegram_user_id=1, username=None)
    assert start.text == texts.MAIN_MENU_TEXT and start.show_main_menu is True


async def test_trusted_statistics_and_search_are_unchanged(trusted_fx):
    stats = await trusted_fx.controller.handle_text(
        texts.STATISTICS_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert "📊 Статистика бота" in stats.text

    search = await trusted_fx.controller.handle_text(
        texts.SEARCH_LABEL, chat_id=_TRUSTED_ID, telegram_user_id=_TRUSTED_ID, username=None,
    )
    assert search.text == texts.SEARCH_ENTRY_TEXT and search.search_prompt is True
    assert trusted_fx.conversation_state_repository.get(_TRUSTED_ID).step == STEP_AWAITING_SEARCH_QUERY


@pytest.mark.parametrize("is_trusted", [False, True], ids=["ordinary", "trusted"])
async def test_start_reply_renders_the_menu_with_both_new_buttons(is_trusted):
    """/start keeps its semantics (MAIN_MENU_TEXT + show_main_menu); the
    rendered reply keyboard now ends with the two new buttons."""
    event = _FakeEvent()
    await _send_reply(event, _FakeReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True), is_trusted=is_trusted)

    (call,) = event.calls
    assert _rows(call["buttons"])[-1] == _NEW_LABELS


def test_osago_button_label_and_url_are_exact():
    assert texts.OSAGO_LINK_BUTTON_LABEL == "🚗 Оформить ОСАГО Грузия"
    assert texts.OSAGO_BOT_URL == "https://t.me/OSAGO24GEbot?start=georgia_bot"
    assert texts.OSAGO_LINK_TEXT == (
        "🛡 ОСАГО Грузия\n\nОформление страховки происходит в нашем страховом боте.\n\nНажмите кнопку ниже:"
    )
