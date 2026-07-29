from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramUserInfo:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool = False

    @property
    def full_name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or None
