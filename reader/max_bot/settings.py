"""Настройки MAX-ботов — ТОЛЬКО из окружения (.env), не из config.yaml:
токены и секреты webhook не должны попадать в репозиторий, логи и отчёты
(repr их не показывает). Всё остальное (БД, police.ge, Georgian Card,
GİB/KGM) — общие настройки проекта из config/config.yaml.

Переменные (bot = GE или TR):
  MAX_<bot>_BOT_TOKEN        токен бота (business.max.ru -> Чат-боты)
  MAX_<bot>_MODE             webhook (production, по умолчанию) | polling (разработка)
  MAX_<bot>_WEBHOOK_SECRET   секрет X-Max-Bot-Api-Secret, [A-Za-z0-9_-]{5,256}
  MAX_<bot>_WEBHOOK_HOST     адрес локального listener'а (127.0.0.1)
  MAX_<bot>_WEBHOOK_PORT     порт (GE 8781, TR 8782)
  MAX_<bot>_WEBHOOK_PATH     путь (/max/ge/webhook, /max/tr/webhook)
  MAX_<bot>_MANAGER_USER_IDS MAX user_id менеджеров через запятую (не Telegram ID)
  MAX_API_BASE_URL           https://platform-api2.max.ru
  MAX_CA_BUNDLE              путь к CA-bundle с корневым сертификатом Минцифры (если нужен)
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from reader.max_bot.api import DEFAULT_BASE_URL

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
_DEFAULTS = {
    "ge": {"port": 8781, "path": "/max/ge/webhook", "telegram_bot": "ProtocolGEbot"},
    "tr": {"port": 8782, "path": "/max/tr/webhook", "telegram_bot": "ProtocolTRbot"},
}


class MaxConfigError(Exception):
    pass


@dataclass(frozen=True)
class MaxBotConfig:
    bot: str
    token: str = field(repr=False)
    mode: str
    webhook_secret: str | None = field(repr=False)
    webhook_host: str
    webhook_port: int
    webhook_path: str
    manager_user_ids: frozenset[int]
    api_base_url: str
    ca_bundle: str | None
    telegram_bot_username: str

    @property
    def tls_verify(self) -> bool | str:
        return self.ca_bundle or True


def _manager_ids(raw: str | None, name: str) -> frozenset[int]:
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise MaxConfigError(f"{name}: ожидаются числовые MAX user_id через запятую")
        ids.add(int(part))
    return frozenset(ids)


def load_max_bot_config(bot: str, env: Mapping[str, str]) -> MaxBotConfig:
    if bot not in _DEFAULTS:
        raise MaxConfigError("bot должен быть 'ge' или 'tr'")
    prefix = f"MAX_{bot.upper()}_"
    defaults = _DEFAULTS[bot]

    token = (env.get(prefix + "BOT_TOKEN") or "").strip()
    if not token:
        raise MaxConfigError(f"{prefix}BOT_TOKEN не задан в .env")

    mode = (env.get(prefix + "MODE") or "webhook").strip().lower()
    if mode not in ("webhook", "polling"):
        raise MaxConfigError(f"{prefix}MODE: webhook или polling")

    secret = (env.get(prefix + "WEBHOOK_SECRET") or "").strip() or None
    if mode == "webhook" and (secret is None or not _SECRET_RE.match(secret)):
        raise MaxConfigError(f"{prefix}WEBHOOK_SECRET обязателен для webhook: 5-256 символов [A-Za-z0-9_-]")

    port_raw = (env.get(prefix + "WEBHOOK_PORT") or str(defaults["port"])).strip()
    if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
        raise MaxConfigError(f"{prefix}WEBHOOK_PORT: число 1-65535")

    path = (env.get(prefix + "WEBHOOK_PATH") or defaults["path"]).strip()
    if not path.startswith("/"):
        raise MaxConfigError(f"{prefix}WEBHOOK_PATH должен начинаться с /")

    return MaxBotConfig(
        bot=bot,
        token=token,
        mode=mode,
        webhook_secret=secret,
        webhook_host=(env.get(prefix + "WEBHOOK_HOST") or "127.0.0.1").strip(),
        webhook_port=int(port_raw),
        webhook_path=path,
        manager_user_ids=_manager_ids(env.get(prefix + "MANAGER_USER_IDS"), prefix + "MANAGER_USER_IDS"),
        api_base_url=(env.get("MAX_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        ca_bundle=(env.get("MAX_CA_BUNDLE") or "").strip() or None,
        telegram_bot_username=defaults["telegram_bot"],
    )
