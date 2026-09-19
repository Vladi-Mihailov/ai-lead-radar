# test_captcha_solver.py
import asyncio
import logging
from pathlib import Path

import httpx

from reader.turkey_bot_test.captcha_solver import CaptchaSolver
from reader.turkey_bot_test.gib.provider import GibProvider
from reader.turkey_bot_test.gib.session import GibSession

logging.basicConfig(level=logging.INFO)

async def test_captcha_solver():
    async with httpx.AsyncClient() as client:
        session = GibSession(client)
        provider = GibProvider(session)
        
        try:
            # Получаем капчу
            challenge = await provider.start()
            
            # Сохраняем изображение для визуальной проверки
            captcha_path = Path("test_captcha.png")
            captcha_path.write_bytes(challenge.image_png)
            print(f"Капча сохранена в {captcha_path}")
            
            # Пробуем распознать
            recognized_text = CaptchaSolver.solve_captcha(challenge.image_png)
            
            if recognized_text:
                print(f"Распознанный текст: {recognized_text}")
                
                # Отправляем на сервер для проверки
                outcome = await provider.submit(
                    plate="TEST123",  # Тестовый номер
                    image_id=challenge.image_id,
                    captcha_code=recognized_text
                )
                print(f"Результат проверки: {outcome.kind}")
            else:
                print("Не удалось распознать капчу")
                
        except Exception as e:
            print(f"Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(test_captcha_solver())