"""
Тесты reader/ocr/prompt.py::SYSTEM_PROMPT — само правило про латиницу (и
остальные существующие правила) применяет модель, а не Python-код (см.
reader/ocr/service.py — там нет ни транслитерации, ни проверки алфавита).
Поэтому здесь только regression-проверка СОСТАВА текста prompt'а — что
нужные инструкции присутствуют и не были случайно ослаблены/удалены при
будущих правках.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.ocr.prompt import SYSTEM_PROMPT  # noqa: E402


def test_prompt_requires_latin_script_for_all_three_name_fields():
    assert "owner_full_name, driver_full_name и policyholder_full_name" in SYSTEM_PROMPT
    assert "ВСЕГДА латиницей" in SYSTEM_PROMPT


def test_prompt_requires_exact_latin_spelling_when_both_scripts_present():
    """Если в документе есть и латиница, и кириллица — брать латиницу как
    есть, не переизобретать её транслитерацией."""
    assert "ТОЧНОЕ латинское написание из документа" in SYSTEM_PROMPT
    assert "ничего не транслитерируя самостоятельно" in SYSTEM_PROMPT


def test_prompt_requires_transliteration_when_only_cyrillic_present():
    assert "ТОЛЬКО кириллицей" in SYSTEM_PROMPT
    assert "транслитерируй его в латиницу сам" in SYSTEM_PROMPT
    assert "не выдумывая другое имя" in SYSTEM_PROMPT


def test_prompt_forbids_mixing_scripts_in_one_value():
    assert "Не смешивай кириллицу и латиницу в одном значении" in SYSTEM_PROMPT


def test_prompt_scopes_latin_script_rule_to_name_fields_only():
    """Марка/модель и остальные vehicle-поля не должны затрагиваться этим
    правилом (см. задачу: "Остальные поля не менять")."""
    assert (
        "ни на одно другое поле (manufacturer, model и т.д.) оно не распространяется"
        in SYSTEM_PROMPT
    )


def test_prompt_still_forbids_owner_full_name_for_legal_entities():
    """Regression: правило про латиницу не должно было случайно затронуть
    уже существующее правило про юрлицо-собственника."""
    assert "owner_full_name = null" in SYSTEM_PROMPT
    assert "юридическое лицо" in SYSTEM_PROMPT


def test_prompt_still_requires_category_from_techpassport_only():
    """Regression: остальные правила (category и т.п.) не изменились при
    добавлении правила про латиницу."""
    assert "category НЕ определяется по паспорту/ID или водительскому удостоверению" in SYSTEM_PROMPT


def test_prompt_still_forbids_guessing_missing_values():
    assert "Не угадывай отсутствующие или нечитаемые значения" in SYSTEM_PROMPT
