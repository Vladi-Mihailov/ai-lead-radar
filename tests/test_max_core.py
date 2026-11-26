"""Ядро MAX-ботов (reader/max_bot/): HTTP-клиент Bot API на mock-ответах,
разбор апдейтов, клавиатуры, перевод разметки, дедуп повторной доставки
webhook, настройки из окружения и привязка MAX <-> Telegram по коду.

Сеть не используется: httpx.MockTransport и aiohttp TestServer."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from reader.max_bot import keyboards  # noqa: E402
from reader.max_bot.api import MaxApi, MaxApiError, MaxRateLimitedError  # noqa: E402
from reader.max_bot.formatting import split_text, telethon_md_to_html  # noqa: E402
from reader.max_bot.linking import CODE_TTL, MAX_ATTEMPTS_PER_HOUR, MaxTelegramLinkService, format_code  # noqa: E402
from reader.max_bot.runtime import SECRET_HEADER, MaxDispatcher, MaxResponder, Reply, build_webhook_app  # noqa: E402
from reader.max_bot.settings import MaxConfigError, load_max_bot_config  # noqa: E402
from reader.max_bot.storage import MaxBotStorage  # noqa: E402
from reader.max_bot.telegram_link import handle_max_link_confirm, handle_max_link_text  # noqa: E402
from reader.max_bot.updates import MaxUser, parse_update  # noqa: E402

_TOKEN = "test-token-not-real"
_USER = 7001


async def _no_sleep(_seconds):
    pass


def _api(handler) -> MaxApi:
    client = httpx.AsyncClient(base_url="https://platform-api2.max.ru", transport=httpx.MockTransport(handler))
    return MaxApi(_TOKEN, client=client, sleep=_no_sleep)


def _message_update(text, *, mid="mid-1", user_id=_USER, chat_type="dialog"):
    return {
        "update_type": "message_created", "timestamp": 1764000000000,
        "message": {
            "sender": {"user_id": user_id, "first_name": "Анна", "username": "anna"},
            "recipient": {"chat_id": 555, "chat_type": chat_type},
            "body": {"mid": mid, "text": text},
        },
    }


def _callback_update(payload, *, callback_id="cb-1", user_id=_USER):
    return {
        "update_type": "message_callback", "timestamp": 1764000000001,
        "callback": {"callback_id": callback_id, "payload": payload, "user": {"user_id": user_id}},
        "message": {"recipient": {"chat_id": 555, "chat_type": "dialog"}, "body": {"mid": "mid-kb"}},
    }


# ---- HTTP-клиент ----


async def test_token_goes_only_to_the_authorization_header_and_text_is_sent_as_html():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"body": {"mid": "m-77"}}})

    api = _api(handler)
    keyboard = keyboards.attachments([[keyboards.callback("OK", "ge:menu")]])
    mid = await api.send_message(user_id=_USER, text="<b>x</b>", attachments=keyboard)

    assert mid == "m-77"
    assert seen["auth"] == _TOKEN
    assert _TOKEN not in seen["url"] and "access_token" not in seen["url"]
    assert "user_id=7001" in seen["url"]
    assert seen["body"]["format"] == "html"
    assert seen["body"]["attachments"][0]["type"] == "inline_keyboard"
    assert _TOKEN not in repr(api)


async def test_rate_limit_is_retried_and_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"code": "too.many.requests", "message": "slow down"})
        return httpx.Response(200, json={"message": {"body": {"mid": "ok"}}})

    assert await _api(handler).send_message(user_id=_USER, text="hi") == "ok"
    assert len(calls) == 3


async def test_persistent_rate_limit_raises_without_endless_retries():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"code": "too.many.requests"})

    with pytest.raises(MaxRateLimitedError):
        await _api(handler).send_message(user_id=_USER, text="hi")
    assert len(calls) == 3


async def test_server_error_is_not_retried_because_sending_is_not_idempotent():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(MaxApiError) as raised:
        await _api(handler).send_message(user_id=_USER, text="hi")
    assert raised.value.status == 500
    assert len(calls) == 1


async def test_success_false_in_a_200_answer_is_an_error():
    def handler(request):
        return httpx.Response(200, json={"success": False, "code": "not.found", "message": "Resource not found"})

    with pytest.raises(MaxApiError) as raised:
        await _api(handler).edit_message("mid-1", text="x")
    assert raised.value.code == "not.found"
    assert raised.value.recipient_unavailable


async def test_blocked_user_and_transport_errors_are_reported():
    def blocked(request):
        return httpx.Response(403, json={"code": "chat.denied", "message": "denied"})

    with pytest.raises(MaxApiError) as raised:
        await _api(blocked).send_message(user_id=_USER, text="hi")
    assert raised.value.recipient_unavailable

    def broken(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(MaxApiError) as raised:
        await _api(broken).send_message(user_id=_USER, text="hi")
    assert raised.value.status is None and raised.value.code == "transport"


async def test_too_long_text_is_rejected_before_any_request():
    def handler(request):
        raise AssertionError("запроса быть не должно")

    with pytest.raises(ValueError):
        await _api(handler).send_message(user_id=_USER, text="x" * 4001)


async def test_callback_answer_edit_and_polling_shapes():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/updates":
            return httpx.Response(200, json={"updates": [{"update_type": "bot_started"}], "marker": 42})
        return httpx.Response(200, json={"success": True})

    api = _api(handler)
    await api.answer_callback("cb-9", message={"text": "new", "attachments": []})
    await api.edit_message("mid-5", text="edited", attachments=[])
    updates, marker = await api.get_updates(marker=41, timeout=1)

    answer, edit, poll = requests
    assert answer.url.params["callback_id"] == "cb-9"
    assert json.loads(answer.content)["message"]["text"] == "new"
    assert edit.method == "PUT" and edit.url.params["message_id"] == "mid-5"
    assert json.loads(edit.content)["attachments"] == []
    assert poll.url.params["marker"] == "41"
    assert marker == 42 and updates == [{"update_type": "bot_started"}]


# ---- апдейты ----


def test_parse_message_callback_and_start_updates():
    message = parse_update(_message_update("AA111BB"))
    assert message.kind == "message" and message.user.user_id == _USER and message.text == "AA111BB"
    assert message.dedup_key == "m:mid-1" and message.is_private

    callback = parse_update(_callback_update("ge:cars"))
    assert callback.kind == "callback" and callback.payload == "ge:cars"
    assert callback.callback_id == "cb-1" and callback.message_id == "mid-kb"
    assert callback.dedup_key == "c:cb-1"

    started = parse_update({"update_type": "bot_started", "timestamp": 5, "chat_id": 1, "user": {"user_id": 3},
                            "payload": "promo"})
    assert started.kind == "started" and started.payload == "promo" and started.dedup_key == "bot_started:3:5"


def test_group_messages_are_not_private_and_garbage_is_ignored():
    assert not parse_update(_message_update("hi", chat_type="chat")).is_private
    assert parse_update({"foo": 1}) is None
    assert parse_update({"update_type": "user_added", "timestamp": 1}).kind == "other"


# ---- клавиатуры и разметка ----


def test_keyboard_limits_are_enforced():
    attachment = keyboards.to_attachment([[keyboards.callback("A", "x"), keyboards.link("B", "https://t.me/x")]])
    assert attachment["payload"]["buttons"][0][1] == {"type": "link", "text": "B", "url": "https://t.me/x"}
    with pytest.raises(ValueError):
        keyboards.to_attachment([[keyboards.callback("A", "x" * 1025)]])
    with pytest.raises(ValueError):
        keyboards.to_attachment([[keyboards.link(str(i), "https://t.me/x") for i in range(4)]])
    assert keyboards.to_attachment(None) is None


def test_telethon_markdown_becomes_escaped_html():
    html = telethon_md_to_html("**AB**123 <script> & [оплата](https://t.me/x?a=1&b=2) __курсив__ `код`")
    assert html == (
        '<b>AB</b>123 &lt;script&gt; &amp; <a href="https://t.me/x?a=1&amp;b=2">оплата</a> '
        '<i>курсив</i> <code>код</code>'
    )


def test_defused_name_stays_literal():
    # escape_telethon_markdown вставляет zero-width space внутрь "**".
    assert "<b>" not in telethon_md_to_html("Иван *​*Петров")


def test_long_text_is_split_on_line_boundaries():
    text = "\n".join(f"строка {i}" for i in range(1000))
    parts = split_text(text, limit=500)
    assert all(len(part) <= 500 for part in parts)
    assert "\n".join(parts) == text


# ---- дедуп и диспетчер ----


class _RecordingHandler:
    def __init__(self):
        self.events = []

    async def handle(self, event, responder):
        self.events.append(event)


class _NullResponder:
    async def notify(self, event, text):
        pass


async def test_redelivered_update_is_processed_once(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    handler = _RecordingHandler()
    dispatcher = MaxDispatcher(storage, handler, _NullResponder())

    assert dispatcher.submit(_message_update("hi", mid="same"))
    assert dispatcher.submit(_message_update("hi", mid="same"))
    assert dispatcher._queue.qsize() == 1

    # Повтор после рестарта процесса — тот же ключ уже в БД.
    restarted = MaxDispatcher(MaxBotStorage(tmp_path / "users.db", bot="ge"), handler, _NullResponder())
    restarted.submit(_message_update("hi", mid="same"))
    assert restarted._queue.qsize() == 0

    # Другой бот (Турция) с тем же mid — свой дедуп.
    other = MaxDispatcher(MaxBotStorage(tmp_path / "users.db", bot="tr"), handler, _NullResponder())
    other.submit(_message_update("hi", mid="same"))
    assert other._queue.qsize() == 1


async def test_dispatcher_ignores_groups_records_users_and_marks_blocked(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    handler = _RecordingHandler()
    dispatcher = MaxDispatcher(storage, handler, _NullResponder())

    await dispatcher.process(parse_update(_message_update("hi", chat_type="channel")))
    assert handler.events == [] and storage.get_user(_USER) is None

    await dispatcher.process(parse_update(_message_update("hi")))
    assert len(handler.events) == 1
    assert storage.get_user(_USER).first_name == "Анна"

    await dispatcher.process(parse_update({"update_type": "bot_stopped", "timestamp": 9, "user": {"user_id": _USER}}))
    assert storage.get_user(_USER).blocked_at is not None
    assert len(handler.events) == 1

    await dispatcher.process(parse_update(_message_update("again", mid="m2")))
    assert storage.get_user(_USER).blocked_at is None


async def test_handler_error_is_reported_to_the_user_and_does_not_crash(tmp_path):
    class _Broken:
        async def handle(self, event, responder):
            raise RuntimeError("boom")

    class _Responder:
        def __init__(self):
            self.notified = []

        async def notify(self, event, text):
            self.notified.append(text)

    responder = _Responder()
    dispatcher = MaxDispatcher(MaxBotStorage(tmp_path / "users.db", bot="ge"), _Broken(), responder)
    await dispatcher.process(parse_update(_message_update("hi")))
    assert responder.notified and "Не удалось" in responder.notified[0]


def test_old_update_keys_are_purged(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    old = datetime.now(timezone.utc) - timedelta(days=4)
    storage.claim_update("old", now=old)
    storage.claim_update("fresh")
    assert storage.purge_processed_updates() == 1
    assert not storage.claim_update("fresh")
    assert storage.claim_update("old")


# ---- webhook ----


async def test_webhook_checks_secret_and_accepts_duplicates_with_200(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    dispatcher = MaxDispatcher(storage, _RecordingHandler(), _NullResponder())
    app = build_webhook_app(dispatcher, secret="s3cret_value", path="/max/ge/webhook")
    async with TestClient(TestServer(app)) as client:
        denied = await client.post("/max/ge/webhook", json=_message_update("hi"))
        assert denied.status == 401
        wrong = await client.post("/max/ge/webhook", json=_message_update("hi"), headers={SECRET_HEADER: "nope"})
        assert wrong.status == 401
        assert dispatcher._queue.qsize() == 0

        headers = {SECRET_HEADER: "s3cret_value"}
        first = await client.post("/max/ge/webhook", json=_message_update("hi"), headers=headers)
        again = await client.post("/max/ge/webhook", json=_message_update("hi"), headers=headers)
        assert first.status == 200 and again.status == 200
        assert dispatcher._queue.qsize() == 1

        bad = await client.post("/max/ge/webhook", data="not json", headers=headers)
        assert bad.status == 400


async def test_webhook_returns_503_when_queue_is_full_and_does_not_claim(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    dispatcher = MaxDispatcher(storage, _RecordingHandler(), _NullResponder(), queue_size=1)
    app = build_webhook_app(dispatcher, secret="s3cret_value", path="/hook")
    headers = {SECRET_HEADER: "s3cret_value"}
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/hook", json=_message_update("a", mid="1"), headers=headers)).status == 200
        assert (await client.post("/hook", json=_message_update("b", mid="2"), headers=headers)).status == 503
    # Апдейт, на который ответили 503, MAX пришлёт снова — он не помечен принятым.
    assert storage.claim_update("m:2")


# ---- ответы ----


class _FakeApi:
    def __init__(self, *, fail_edit=False):
        self.calls = []
        self.fail_edit = fail_edit

    async def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        return f"mid-{len(self.calls)}"

    async def answer_callback(self, callback_id, **kwargs):
        self.calls.append(("answer", callback_id, kwargs))

    async def edit_message(self, message_id, **kwargs):
        if self.fail_edit:
            raise MaxApiError(method="PUT", path="/messages", status=400, code="bad", message="x")
        self.calls.append(("edit", message_id, kwargs))


async def test_callback_reply_edits_in_place_and_long_reply_is_split():
    api = _FakeApi()
    responder = MaxResponder(api)
    event = parse_update(_callback_update("ge:cars"))

    await responder.answer(event, Reply(text="**Мои авто**", keyboard=[[keyboards.callback("A", "ge:menu")]]))
    kind, callback_id, kwargs = api.calls[0]
    assert kind == "answer" and callback_id == "cb-1"
    assert kwargs["message"]["text"] == "<b>Мои авто</b>"
    assert kwargs["message"]["attachments"][0]["type"] == "inline_keyboard"

    api.calls.clear()
    long_text = "\n".join("x" * 100 for _ in range(60))
    keyboard = [[keyboards.callback("A", "ge:menu")]]
    await responder.answer(event, Reply(text=long_text, keyboard=keyboard, markdown=False))
    assert api.calls[0][0] == "answer" and api.calls[0][2]["message"]["attachments"] == []
    assert api.calls[-1][0] == "send" and api.calls[-1][1]["attachments"]


async def test_slow_work_shows_progress_then_result_with_fallback_to_new_message():
    api = _FakeApi()
    responder = MaxResponder(api)
    event = parse_update(_message_update("AA111BB"))

    async def work():
        return Reply(text="готово", markdown=False)

    await responder.run_slow(event, "⏳", work)
    assert [call[0] for call in api.calls] == ["send", "edit"]
    assert api.calls[1][1] == "mid-1"

    failing = _FakeApi(fail_edit=True)
    await MaxResponder(failing).run_slow(event, "⏳", work)
    assert [call[0] for call in failing.calls] == ["send", "send"]


# ---- настройки ----


def test_settings_require_token_and_valid_webhook_secret():
    with pytest.raises(MaxConfigError):
        load_max_bot_config("ge", {})
    with pytest.raises(MaxConfigError):
        load_max_bot_config("ge", {"MAX_GE_BOT_TOKEN": "t", "MAX_GE_WEBHOOK_SECRET": "bad secret!"})
    with pytest.raises(MaxConfigError):
        load_max_bot_config("tr", {"MAX_TR_BOT_TOKEN": "t", "MAX_TR_MANAGER_USER_IDS": "12,abc"})

    config = load_max_bot_config("tr", {
        "MAX_TR_BOT_TOKEN": "secret-token", "MAX_TR_WEBHOOK_SECRET": "abc_DEF-123", "MAX_TR_MANAGER_USER_IDS": "5, 6",
    })
    assert config.webhook_port == 8782 and config.webhook_path == "/max/tr/webhook"
    assert config.manager_user_ids == frozenset({5, 6})
    assert config.api_base_url == "https://platform-api2.max.ru"
    assert "secret-token" not in repr(config) and "abc_DEF-123" not in repr(config)

    polling = load_max_bot_config("ge", {"MAX_GE_BOT_TOKEN": "t", "MAX_GE_MODE": "polling"})
    assert polling.mode == "polling" and polling.webhook_secret is None


# ---- привязка MAX <-> Telegram ----


def test_link_code_is_one_time_hashed_and_bound_to_the_bot(tmp_path):
    ge = MaxTelegramLinkService(MaxBotStorage(tmp_path / "users.db", bot="ge"))
    tr = MaxTelegramLinkService(MaxBotStorage(tmp_path / "users.db", bot="tr"))
    code = ge.issue_code(_USER)

    stored = ge._conn.execute("SELECT code_hash FROM max_link_codes").fetchall()
    assert code not in str(stored)

    assert tr.confirm(code, telegram_user_id=111).status == "wrong_bot"
    assert ge.confirm(format_code(code).lower(), telegram_user_id=111).status == "linked"
    assert ge.linked_telegram_user(_USER) == 111
    assert ge.confirm(code, telegram_user_id=222).status == "used"
    assert ge.linked_telegram_user(_USER) == 111


def test_link_code_expires_new_code_cancels_old_and_attempts_are_limited(tmp_path):
    service = MaxTelegramLinkService(MaxBotStorage(tmp_path / "users.db", bot="ge"))
    issued_at = datetime.now(timezone.utc) - CODE_TTL - timedelta(minutes=1)
    old = service.issue_code(_USER, now=issued_at)
    assert service.confirm(old, telegram_user_id=1).status == "expired"

    first = service.issue_code(_USER)
    second = service.issue_code(_USER)
    assert service.confirm(first, telegram_user_id=2).status == "invalid"

    for _ in range(MAX_ATTEMPTS_PER_HOUR):
        assert service.confirm("ZZZZZZZZ", telegram_user_id=3).status == "invalid"
    assert service.confirm(second, telegram_user_id=3).status == "rate_limited"
    assert service.confirm(second, telegram_user_id=4).status == "linked"


def test_telegram_side_links_only_after_confirmation_showing_the_max_name(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    storage.record_user(MaxUser(user_id=_USER, first_name="Анна", last_name="К."), chat_id=1)
    service = MaxTelegramLinkService(storage)
    code = service.issue_code(_USER)
    assert handle_max_link_text("привет", telegram_user_id=1, link_service=service) is None
    assert "/max_link" in handle_max_link_text("/max_link", telegram_user_id=1, link_service=service).text

    prompt = handle_max_link_text(f"/max_link {format_code(code)}", telegram_user_id=1, link_service=service)
    assert "«Анна К.»" in prompt.text
    assert service.linked_telegram_user(_USER) is None
    (_, confirm_data), (_, cancel_data) = prompt.buttons

    assert handle_max_link_confirm(cancel_data, telegram_user_id=1, link_service=service) == "Привязка отменена."
    assert service.linked_telegram_user(_USER) is None
    assert handle_max_link_confirm(confirm_data, telegram_user_id=1, link_service=service).startswith("✅")
    assert service.linked_telegram_user(_USER) == 1
    assert handle_max_link_confirm(b"other", telegram_user_id=1, link_service=service) is None


def test_record_user_keeps_names_when_event_has_none(tmp_path):
    storage = MaxBotStorage(tmp_path / "users.db", bot="ge")
    assert storage.record_user(MaxUser(user_id=1, first_name="Анна"), chat_id=10)
    assert not storage.record_user(MaxUser(user_id=1), chat_id=None)
    user = storage.get_user(1)
    assert user.first_name == "Анна" and user.chat_id == 10


async def test_second_tap_while_busy_gets_busy_notice_and_no_second_run(tmp_path):
    import asyncio

    release = asyncio.Event()
    started = []

    class _Slow:
        async def handle(self, event, responder):
            started.append(event.payload)
            await release.wait()

    class _Responder:
        def __init__(self):
            self.notified = []

        async def notify(self, event, text):
            self.notified.append(text)

    responder = _Responder()
    dispatcher = MaxDispatcher(MaxBotStorage(tmp_path / "users.db", bot="ge"), _Slow(), responder)
    first = asyncio.create_task(dispatcher.process(parse_update(_callback_update("ge:chk:1", callback_id="a"))))
    await asyncio.sleep(0)
    await dispatcher.process(parse_update(_callback_update("ge:chk:1", callback_id="b")))
    assert started == ["ge:chk:1"] and responder.notified and responder.notified[0].startswith("⏳")
    release.set()
    await first
    assert dispatcher._user_locks == {}


async def test_failed_slow_work_replaces_the_progress_message():
    api = _FakeApi()
    menu = [[keyboards.callback("Меню", "ge:menu")]]
    responder = MaxResponder(api, error_keyboard=menu)

    async def broken():
        raise RuntimeError("boom")

    await responder.run_slow(parse_update(_callback_update("ge:chk:1")), "⏳", broken)
    kind, message_id, kwargs = api.calls[-1]
    assert kind == "edit" and message_id == "mid-kb"
    assert "Не удалось" in kwargs["text"] and kwargs["attachments"][0]["type"] == "inline_keyboard"


async def test_messages_from_bots_are_ignored(tmp_path):
    handler = _RecordingHandler()
    dispatcher = MaxDispatcher(MaxBotStorage(tmp_path / "users.db", bot="ge"), handler, _NullResponder())
    raw = _message_update("hi")
    raw["message"]["sender"]["is_bot"] = True
    await dispatcher.process(parse_update(raw))
    assert handler.events == []


def test_markup_does_not_touch_link_addresses_or_span_lines():
    assert telethon_md_to_html("[x](https://x.com/a__b__c)") == '<a href="https://x.com/a__b__c">x</a>'
    assert "<b>" not in telethon_md_to_html("**строка\nдругая**")
