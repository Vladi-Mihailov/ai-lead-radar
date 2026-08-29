from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramUserInfo:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool = False
    # Для восстановления InputPeerUser(user_id, access_hash) без @username —
    # см. UserRepository.upsert(). None означает "неизвестно/не передано" (а
    # не "нет"), поэтому уже сохранённое значение не затирается.
    access_hash: int | None = None
    peer_type: str | None = None

    @property
    def full_name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or None

    @classmethod
    def from_telethon_user(cls, entity) -> "TelegramUserInfo":
        """Строит TelegramUserInfo из полноценного объекта
        telethon.tl.types.User (например, результата
        TelegramClient.get_entity()) — тот же набор полей, что уже
        собирают вручную reader/sources/telegram_source.py,
        reader/users/sync.py и reader/users/history_sync.py. Новым
        местам (см. reader/commands/fine.py — резолв @username, которого
        ещё нет в локальной БД) следует переиспользовать этот метод, а не
        дублировать маппинг entity -> TelegramUserInfo ещё раз."""
        return cls(
            user_id=entity.id,
            username=getattr(entity, "username", None),
            first_name=getattr(entity, "first_name", None),
            last_name=getattr(entity, "last_name", None),
            is_bot=bool(getattr(entity, "bot", False)),
            access_hash=getattr(entity, "access_hash", None),
            peer_type=type(entity).__name__,
        )
