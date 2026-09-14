"""
Тесты reader/turkey_bot/gib/parser.py::parse_submit_response.

См. design report Stage 2 (и апдейты после live-тестов): ТРИ реально
наблюдавшихся формы ответа зафиксированы отдельными регрессионными
тестами - test_observed_no_debt_response_is_classified_as_no_debt ("долг
не найден", type="ERROR"), test_observed_captcha_rejected_response_is_classified_as_rejected
(неверный код, type="ERROR") и test_observed_has_debt_response_with_borclar_is_classified_as_has_debt
(реально найденный штраф - плоская форма с "BORCLAR", БЕЗ обёртки "data").
Всё остальное - по-прежнему грубая, evidence-based классификация: любой
ЕЩЁ НЕ увиденный ERROR/WARNING -> "unexpected", не "rejected" и не
"no_debt" (см. задачу: "do not generalize other unknown ERROR messages to
rejected").
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
        {"BORCLAR": []},
        {"BORCLAR": [{"KK_BORC": "1.00"}]},
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


def _sanitized_borclar_response(*, borclar: list) -> dict:
    """Реальная, плоская форма успешного ответа с результатом (см. design
    report Stage 4/live-test) - БЕЗ обёртки "data", "messages" - буквально
    null (не список)."""
    return {
        "BORCLAR": borclar,
        "BORC_SORGU_TARIHI": "20260101",
        "SPOS_ISLEM_TIPI": "7",
        "messages": None,
        "pageDetail": None,
    }


# Сохраняет РЕАЛЬНЫЕ, увиденные вживую имена полей одной записи в BORCLAR -
# но НЕ реальные значения: hash/kimlik/сумма/описание/номер протокола
# заменены на фиктивные (см. задачу: "do not expose CAPTCHA/session
# security values unnecessarily" - KK_HASH/KK_KIMLIK выглядят как платёжные
# токены GIB, не captcha/session, но заменены из той же осторожности).
_SANITIZED_FINE_RECORD = {
    "KK_ACIKLAMA": "Example location description - speed limit exceeded",
    "KK_AD": "",
    "KK_BORC": "1000.00",
    "KK_GECIKMEZAMMI": "0.00",
    "KK_HASH": "SANITIZED-PLACEHOLDER-HASH",
    "KK_INDIRIM_MIKTARI": "0.00",
    "KK_KIMLIK": "SANITIZED-PLACEHOLDER-KIMLIK",
    "KK_KONTROL": "1",
    "KK_MIKTARODENEN": "1000.00",
    "KK_MYS_ETTN": "00000000-0000-0000-0000-000000000000",
    "KK_MYS_KURUM_ADI": "EMNİYET GENEL MÜDÜRLÜĞÜ",
    "KK_MYS_TAHSILAT_TURU": (
        "Yabancı Plakalı Araç ve Sürücüsüne Düzenlenen İdari Para Cezası Karar Tutanakları"
    ),
    "KK_ODEMETARIHI": "20260101",
    "KK_ODEMETURLERI": "111",
    "KK_ORGOID": "00000000000000",
    "KK_OZEL_PLAKA_KODU": "34ABC123",
    "KK_PLAKA": "34ABC123",
    "KK_SOYAD": "",
    "KK_TUTANAKNO": "XX00000000",
    "KK_VDKODU": "000000",
}


def test_observed_has_debt_response_with_borclar_is_classified_as_has_debt():
    """Регрессионный тест на РЕАЛЬНЫЙ ответ третьего live-запуска (принятая
    CAPTCHA + реально найденный штраф, см. design report Stage 4/live-test):
    успешный ответ - плоская форма с непустым "BORCLAR", БЕЗ обёртки "data"
    вовсе. См. _SANITIZED_FINE_RECORD про то, что именно в фикстуре
    настоящее (имена полей), а что - нет (значения)."""
    payload = _sanitized_borclar_response(borclar=[_SANITIZED_FINE_RECORD])

    outcome = parse_submit_response(payload)

    assert outcome.kind == "has_debt"
    assert outcome.messages == ()
    assert outcome.raw_data == payload


def test_empty_borclar_is_classified_as_no_debt():
    """Симметрично has_debt выше - пустой "BORCLAR" тоже классифицируется
    как "долгов нет" (в дополнение к уже известному текстовому сообщению,
    см. test_observed_no_debt_response_is_classified_as_no_debt)."""
    payload = _sanitized_borclar_response(borclar=[])

    outcome = parse_submit_response(payload)

    assert outcome.kind == "no_debt"


def test_borclar_shape_no_longer_falls_through_to_unexpected():
    """Регрессия на конкретный исправленный баг (см. design report): до
    фикса такой ответ ошибочно попадал в "unexpected" через ветку
    '"data" not in payload', потому что у этой формы ответа "data" на
    верхнем уровне действительно нет."""
    payload = _sanitized_borclar_response(borclar=[_SANITIZED_FINE_RECORD])

    outcome = parse_submit_response(payload)

    assert outcome.kind != "unexpected"


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
