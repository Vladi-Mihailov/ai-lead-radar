"""Распознавание именно автомобильно-страховой лексики в тексте — чистые
функции без Telegram/БД, общие для инвайтера (match_rule="ru_insurance",
см. reader/inviter/lead_pool.py) и ЛС-кампании "insurance" (см.
reader/dm_campaigns/observer.py), чтобы правило не дублировалось."""

import re

INSURANCE_ROOT = "страх"

# Однозначно страховые термины без корня "страх" ("где сделать ОСАГО?") —
# только то, что не встречается в обычной речи в другом смысле.
INSURANCE_TERMS = ("осаго", "каско", "зеленая карта", "зелёная карта", "green card")


def ru_insurance_words(text: str, keyword: str) -> list[str]:
    """Слова (целиком, по границам слова), где keyword — корень со
    страховой морфологией: за корнем идёт "ов…" (страховка, страхование,
    застрахован, автостраховка, медстраховка, страховщик) или "у" + ещё
    буквы (страхуйтесь, застрахуй). Любая приставка допустима (за-, авто-,
    мед-), КРОМЕ "пере-": "перестраховаться"/"перестраховка" в источнике —
    идиома "подстраховаться" (проверено по реальным сообщениям @sadahlo),
    а не страхование. Не совпадают: "Астрахань"/"астрахани" ("страх" +
    "ань"), "канистрах" (ничего после корня), голое "страх"/"страха"/
    "страхом" ("на свой страх и риск" — страх, а не страховка)."""
    root = keyword.lower().strip()
    if not root:
        return []
    pattern = re.compile(rf"\w*{re.escape(root)}(?:ов\w*|у\w+)")
    words = []
    for word in re.findall(r"\w+", (text or "").lower()):
        if not pattern.fullmatch(word):
            continue
        if word[: word.find(root)].endswith("пере"):
            continue
        words.append(word)
    return words


def is_insurance_text(text: str) -> bool:
    """Префильтр ЛС-кампании "insurance" поверх широкого сценария
    insurance (config/scenarios.yaml матчит и "авто"/"онлайн"/"$"):
    страховое слово по ru_insurance_words ИЛИ однозначный термин."""
    lowered = (text or "").lower()
    if ru_insurance_words(lowered, INSURANCE_ROOT):
        return True
    return any(term in lowered for term in INSURANCE_TERMS)
