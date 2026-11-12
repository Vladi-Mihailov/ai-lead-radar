"""Тесты reader/turkey_bot/kgm/relay_version.py — отпечаток кода KGM
(session.py, parser.py, models.py), который сверяют production и relay:
форма запроса к KGM живёт в session.py на relay, разбор ответа — на
production, и они обязаны быть из одного коммита."""

import hashlib
import re
from pathlib import Path

from reader.turkey_bot.kgm import relay_version
from reader.turkey_bot.kgm.relay_version import FINGERPRINT_FILES, fingerprint_files, kgm_code_fingerprint

KGM_DIR = Path(relay_version.__file__).parent


def _copy_kgm_files(directory: Path, *, newline: bytes = b"\n", changed: str | None = None) -> list[Path]:
    directory.mkdir()
    paths = []
    for name in FINGERPRINT_FILES:
        data = (KGM_DIR / name).read_bytes().replace(b"\r\n", b"\n")
        if name == changed:
            data += b"\n# hdnSorguTipi=YABANCI\n"
        path = directory / name
        path.write_bytes(data.replace(b"\n", newline))
        paths.append(path)
    return paths


def test_fingerprint_is_12_lowercase_hex_and_stable():
    fingerprint = kgm_code_fingerprint()

    assert re.fullmatch(r"[0-9a-f]{12}", fingerprint)
    assert kgm_code_fingerprint() == fingerprint


def test_fingerprint_follows_the_documented_algorithm():
    digest = hashlib.sha256()
    for name in ("session.py", "parser.py", "models.py"):
        content = (KGM_DIR / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode("ascii") + b"\0" + content + b"\0")

    assert FINGERPRINT_FILES == ("session.py", "parser.py", "models.py")
    assert kgm_code_fingerprint() == digest.hexdigest()[:12]


def test_crlf_checkout_gives_the_same_fingerprint(tmp_path):
    lf = _copy_kgm_files(tmp_path / "lf")
    crlf = _copy_kgm_files(tmp_path / "crlf", newline=b"\r\n")

    assert fingerprint_files(lf) == fingerprint_files(crlf) == kgm_code_fingerprint()


def test_a_change_in_any_fingerprinted_file_changes_the_fingerprint(tmp_path):
    for name in FINGERPRINT_FILES:
        changed = _copy_kgm_files(tmp_path / name.removesuffix(".py"), changed=name)

        assert fingerprint_files(changed) != kgm_code_fingerprint(), name


def test_file_names_are_part_of_the_fingerprint(tmp_path):
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_bytes(b"same")
    second.write_bytes(b"same")

    assert fingerprint_files([first]) != fingerprint_files([second])
