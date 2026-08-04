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
