"""
Тесты reader/turkey_bot/gib/parser.py::parse_submit_response.

См. design report Stage 2 (и два его апдейта): ровно ДВА реально
наблюдавшихся текста message с type="ERROR" зафиксированы отдельными
регрессионными тестами -
test_observed_no_debt_response_is_classified_as_no_debt ("долг не найден")
и test_observed_captcha_rejected_response_is_classified_as_rejected
(неверный код). Всё остальное - по-прежнему грубая, evidence-based
классификация: любой ТРЕТИЙ, ещё не увиденный ERROR/WARNING -> "unexpected",
не "rejected" и не "no_debt" (см. задачу: "do not generalize other unknown
ERROR messages to rejected").
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.gib.parser import parse_submit_response  # noqa: E402


def test_empty_list_data_is_no_debt():
    outcome = parse_submit_response({"data": [], "messages": None})

    assert outcome.kind == "no_debt"
    assert outcome.messages == ()
    assert outcome.raw_data == []


def test_empty_dict_data_is_no_debt():
    outcome = parse_submit_response({"data": {}})

    assert outcome.kind == "no_debt"


def test_null_data_is_no_debt():
    outcome = parse_submit_response({"data": None})

    assert outcome.kind == "no_debt"


def test_non_empty_list_data_is_has_debt():
    outcome = parse_submit_response({"data": [{"someField": "someValue"}]})

    assert outcome.kind == "has_debt"
    assert outcome.raw_data == [{"someField": "someValue"}]


def test_non_empty_dict_data_is_has_debt():
    outcome = parse_submit_response({"data": {"count": 1}})

    assert outcome.kind == "has_debt"


def test_observed_no_debt_response_is_classified_as_no_debt():
    """Регрессионный тест на РЕАЛЬНЫЙ ответ первого live-запуска (см.
    design report Stage 2, апдейт): GIB прислал type="ERROR" для
    легитимного "штрафов/долга не найдено" - это НЕ отклонение CAPTCHA."""
    outcome = parse_submit_response(
        {
            "messages": [
                {"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}
            ],
            "data": None,
        }
    )

    assert outcome.kind == "no_debt"
    assert outcome.messages[0].type == "ERROR"
    assert outcome.raw_data is None


def test_observed_no_debt_response_without_explicit_data_key():
    """Тот же реальный текст, но без явного "data": null в payload (см.
    design report: печать manual_test.py не позволяла отличить null от
    отсутствующего ключа) - оба варианта должны давать одинаковый исход."""
    outcome = parse_submit_response(
        {"messages": [{"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}]}
    )

    assert outcome.kind == "no_debt"
    assert outcome.raw_data is None


def test_observed_captcha_rejected_response_is_classified_as_rejected():
    """Регрессионный тест на РЕАЛЬНЫЙ ответ второго live-запуска (заведомо
    неверный код, см. design report Stage 2, второй апдейт): GIB прислал
    type="ERROR" с текстом о неверном коде безопасности - ЭТО реальный
    текст отклонённой CAPTCHA."""
    outcome = parse_submit_response(
        {
            "messages": [
                {
                    "type": "ERROR",
                    "text": (
                        "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol "
                        "ederek tekrar deneyiniz."
                    ),
                }
            ],
            "data": None,
        }
    )

    assert outcome.kind == "rejected"
    assert outcome.messages[0].type == "ERROR"
    assert outcome.raw_data is None


def test_observed_captcha_rejected_response_without_explicit_data_key():
    outcome = parse_submit_response(
        {
            "messages": [
                {
                    "type": "ERROR",
                    "text": (
                        "Güvenlik kodunu yanlış girdiniz. Lütfen kontrol "
                        "ederek tekrar deneyiniz."
                    ),
                }
            ]
        }
    )

    assert outcome.kind == "rejected"
    assert outcome.raw_data is None


def test_unrecognized_error_message_is_unexpected_not_rejected():
    """Явное требование задачи: НЕ обобщать любой ДРУГОЙ, ещё не увиденный
    ERROR на "rejected" - только эти два конкретных, реально наблюдавшихся
    текста дают no_debt/rejected, всё остальное - "unexpected"."""
    outcome = parse_submit_response(
        {"messages": [{"type": "ERROR", "text": "some never-before-seen message"}], "data": []}
    )

    assert outcome.kind == "unexpected"
    assert outcome.messages[0].text == "some never-before-seen message"


def test_unrecognized_warning_message_is_also_unexpected():
    outcome = parse_submit_response({"messages": [{"type": "WARNING", "text": "expired"}]})

    assert outcome.kind == "unexpected"


def test_parser_only_produces_rejected_for_the_known_message():
    """"rejected" срабатывает ТОЛЬКО на реально увиденный текст - никакой
    другой известный входной случай (включая другой известный текст,
    no_debt) не должен давать "rejected" (см. задачу: "do not generalize
    other unknown ERROR messages to rejected")."""
    known_non_rejected_inputs = [
        {"data": []},
        {"data": [{"x": 1}]},
        {"messages": [{"type": "ERROR", "text": "Girdiğiniz plakaya ait borç bulunamadı."}]},
        {"messages": [{"type": "ERROR", "text": "anything else"}]},
        {"somethingElse": True},
        ["not", "a", "dict"],
    ]

    for payload in known_non_rejected_inputs:
        assert parse_submit_response(payload).kind != "rejected"


def test_info_and_success_messages_do_not_block_no_debt():
    outcome = parse_submit_response(
        {"messages": [{"type": "INFO", "text": "ok"}], "data": []}
    )

    assert outcome.kind == "no_debt"
    assert outcome.messages[0].type == "INFO"


def test_missing_messages_and_data_is_unexpected():
    outcome = parse_submit_response({"somethingElse": True})

    assert outcome.kind == "unexpected"
    assert outcome.raw_data == {"somethingElse": True}


def test_non_dict_payload_is_unexpected():
    outcome = parse_submit_response(["not", "a", "dict"])

    assert outcome.kind == "unexpected"
    assert outcome.raw_data == ["not", "a", "dict"]


def test_non_dict_items_inside_messages_are_ignored_defensively():
    outcome = parse_submit_response({"messages": ["not-a-dict", 123], "data": []})

    assert outcome.messages == ()
    assert outcome.kind == "no_debt"


def test_unexpected_from_unrecognized_error_still_exposes_data_field():
    """raw_data для "unexpected", возникшего из-за НЕизвестного
    ERROR/WARNING - это payload["data"] (а не весь payload) - тот же
    принцип, что и у "no_debt"/"has_debt", чтобы manual_test.py мог
    показать разработчику именно полезную часть ответа."""
    outcome = parse_submit_response(
        {"messages": [{"type": "ERROR", "text": "x"}], "data": {"irrelevant": True}}
    )

    assert outcome.kind == "unexpected"
    assert outcome.raw_data == {"irrelevant": True}
