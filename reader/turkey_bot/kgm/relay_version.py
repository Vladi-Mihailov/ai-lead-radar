"""Версии KGM relay для сверки production <-> грузинский сервер (см.
docs/kgm-relay.md, "Update the relay").

RELAY_PROTOCOL_VERSION — версия HTTP-контракта relay (маршруты, поля,
подпись). kgm_code_fingerprint() — отпечаток кода, который ОБЯЗАН совпадать
на обоих серверах: relay выполняет session.py (форма запроса к KGM, захват
ViewState), production разбирает ответ parser.py/models.py, а форма KGM
меняется именно в session.py (PR #50, hdnSorguTipi=YABANCI). /health relay
отдаёт оба значения, `python -m reader.turkey_bot.kgm.route` сравнивает их
со своими и при расхождении печатает MISMATCH.

Только stdlib и ничего из пакета: модуль одинаково лёгкий для обеих сторон."""

import hashlib
from collections.abc import Iterable
from pathlib import Path

RELAY_PROTOCOL_VERSION = "1"

FINGERPRINT_FILES = ("session.py", "parser.py", "models.py")


def fingerprint_files(paths: Iterable[Path]) -> str:
    """Первые 12 hex sha256: для каждого файла имя + "\\0" + содержимое +
    "\\0". \\r\\n -> \\n — checkout на Windows (core.autocrlf) даёт тот же
    отпечаток, что и на Linux."""
    digest = hashlib.sha256()
    for path in paths:
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(path.name.encode("ascii") + b"\0" + content + b"\0")
    return digest.hexdigest()[:12]


def kgm_code_fingerprint() -> str:
    """Отпечаток session.py, parser.py и models.py из каталога этого модуля.
    Читает файлы с диска — relay вызывает её один раз при старте, чтобы
    /health описывал загруженный код, а не подменённые позже файлы."""
    return fingerprint_files(Path(__file__).with_name(name) for name in FINGERPRINT_FILES)
