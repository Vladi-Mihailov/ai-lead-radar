"""In-process, НЕ persistent store — ЕДИНСТВЕННОЕ место, где живёт номер
протокола между двумя сообщениями Telegram у варианта "📄 По протоколу"
(см. задачу "Проверить протокол" п.4: "Sensitive input этого flow держать
только в памяти процесса... Использовать отдельное in-memory ephemeral
state storage").

Намеренно НЕ используется BotConversationStateRepository — её payload
сохраняется как plaintext JSON в SQLite без TTL (см. диагностику той же
задачи), а номер протокола/техпаспорта/Violator ID-TAX явно перечислены
как то, что НЕ должно туда попадать. Намеренно НЕ переживает restart
процесса (см. задачу: "после restart процесса незавершённый flow может
быть потерян — для MVP это нормально и безопаснее, чем сохранять
документы в SQLite") — обычный dict в памяти, без файла/БД под ним.

Номер техпаспорта (variant "🚗 По автомобилю") и ID/TAX-номер нарушителя
(второй шаг variant "📄 По протоколу") здесь НЕ хранятся вовсе — они
вводятся на ПОСЛЕДНЕМ шаге своего flow и используются немедленно, в той же
async-функции, что их получает (см. reader/public_bot/conversation.py::
_handle_protocol_check_vehicle_document_input/
_handle_protocol_check_personal_number_input) — сохранять их куда-либо,
даже сюда, не нужно."""


class ProtocolCheckEphemeralStore:
    def __init__(self) -> None:
        self._protocol_numbers: dict[int, str] = {}

    def set_protocol_number(self, chat_id: int, protocol_no: str) -> None:
        self._protocol_numbers[chat_id] = protocol_no

    def pop_protocol_number(self, chat_id: int) -> str | None:
        """Получить И сразу забыть — используется РОВНО один раз, на шаге
        ввода ID/TAX-номера нарушителя (см.
        ConversationController._handle_protocol_check_personal_number_input)."""
        return self._protocol_numbers.pop(chat_id, None)

    def clear(self, chat_id: int) -> None:
        """Вызывается на КАЖДОМ выходе из flow до его завершения —
        Cancel/новый главный flow/Back туда, где значение больше не нужно
        (см. задачу п.9) — идемпотентно, безопасно вызывать даже если
        значения для этого chat_id никогда не было."""
        self._protocol_numbers.pop(chat_id, None)
