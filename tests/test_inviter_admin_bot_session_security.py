"""16. Session path/ownership handling — регрессионные проверки на
конкретный production incident (root-owned .session-файл, см. design
report "SESSION SECURITY").

Архитектурный вывод после аудита (см. финальный отчёт): reader/inviter/
уже запускается как systemd User=leadradar/Group=leadradar (см.
deploy/ai-lead-radar-inviter.service), и ЛЮБОЙ файл, который создаёт
Python-процесс, принадлежит тому пользователю, от имени которого запущен
процесс, — БЕЗ единой строчки chown/chmod в самом Python-коде. Поэтому
reader/inviter_admin_bot/ ДОЛЖЕН запускаться как systemd User=leadradar
тем же способом (см. итоговый отчёт про systemd unit) — и НЕ должен
содержать НИКАКОГО blind os.chown()/subprocess sudo — это и проверяют
тесты ниже, а не просто фиксируют текущее поведение."""

import ast
import asyncio
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.models import TelegramAccount
from reader.inviter_admin_bot.auth import AccountAuthCoordinator

_PACKAGE_DIR = PROJECT_ROOT / "reader" / "inviter_admin_bot"

_FORBIDDEN_NAMES = {
    "chown", "chmod", "lchown", "fchown",  # прямая смена владельца/прав
    "sudo", "setuid", "seteuid", "setgid", "setegid",  # эскалация привилегий
}


def _iter_py_files():
    return sorted(_PACKAGE_DIR.glob("*.py"))


def test_package_never_calls_chown_chmod_or_sudo():
    """Статическая проверка ВСЕГО пакета (AST, не grep по тексту — не
    ловит совпадения в докстроках/комментариях) — ни один вызов chown/
    chmod/sudo/setuid и т.п. нигде в reader/inviter_admin_bot/ (см.
    design: "Не делать blind sudo/chown внутри Python без анализа")."""
    offenders = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
                offenders.append(f"{path.name}: os.{node.attr}(...)")
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
                offenders.append(f"{path.name}: {node.id}(...)")
    assert offenders == []


def test_package_never_shells_out_to_sudo_or_systemctl():
    """"не использовать systemctl start/stop из Telegram bot, если этого
    можно избежать" (см. design про pause/resume) — статическая проверка,
    что ни subprocess, ни os.system нигде не упоминаются в пакете вовсе
    (pause/resume реализован через БД, см. reader/inviter/
    runtime_state_repository.py, а не через systemctl)."""
    offenders = []
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8")
        for needle in ("subprocess", "os.system(", "systemctl"):
            if needle in source:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


def test_new_account_session_path_stays_under_configured_sessions_dir():
    """Новый аккаунт (➕ Добавить аккаунт) — session_path, с которым
    реально строится TelegramClient, ВСЕГДА внутри sessions_dir,
    переданного вызывающим кодом (main.py — тот же data/sessions/, что и
    у остальных аккаунтов инвайтера, см. reader/inviter/authorize.py) —
    никакого произвольного/абсолютного пути, не связанного с ним.
    Чёрный ящик: перехватываем путь через сам client_factory, не трогая
    приватные методы координатора."""
    captured = []

    def factory(session_path):
        captured.append(session_path)

        class _Client:
            async def connect(self):
                raise ConnectionError("boom — достаточно дойти до вызова factory")

            async def disconnect(self):
                pass

        return _Client()

    sessions_dir = Path("data/sessions")
    coordinator = AccountAuthCoordinator(factory, account_repository=None, sessions_dir=sessions_dir)

    asyncio.run(coordinator.start_new(chat_id=1, phone="+995571024864"))

    assert len(captured) == 1
    assert Path(captured[0]).parent == sessions_dir
    assert Path(captured[0]).is_absolute() is False  # относительный, в пределах проекта, не /root/...


def test_reauthorize_never_changes_an_existing_accounts_session_path():
    """🔐 Переавторизовать переиспользует account.session_path НАПРЯМУЮ
    (см. auth.py::start_reauthorize) — не строит новый путь из
    sessions_dir/slug, поэтому существующий .session-файл (созданный
    правильным процессом/владельцем) не подменяется новым файлом в другом
    месте."""
    captured = []

    def factory(session_path):
        captured.append(session_path)

        class _Client:
            async def connect(self):
                raise ConnectionError("boom — достаточно дойти до вызова factory")

            async def disconnect(self):
                pass

        return _Client()

    account = TelegramAccount(
        id=1, name="@existing", phone="995500000099", session_name="legacy_custom_name",
        session_path="/home/leadradar/ai-lead-radar/data/sessions/legacy_custom_name",
        daily_limit=30, enabled=True, created_at=datetime(2026, 1, 1), last_used_at=None,  # noqa: DTZ001
    )
    coordinator = AccountAuthCoordinator(factory, account_repository=None, sessions_dir=Path("data/sessions"))

    asyncio.run(coordinator.start_reauthorize(chat_id=1, account=account))

    assert captured == [account.session_path]
