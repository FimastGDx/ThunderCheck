import logging
import json
import time
import asyncio
from datetime import datetime
from typing import Dict
from openai import OpenAI
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode
import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MAIN_LOG = config.MAIN_LOG
TABLE_JSON = config.TABLE_JSON

# конфигурация
TELEGRAM_TOKEN = config.TELEGRAM_BOT_TOKEN
GROQ_API_KEY = config.GROQ_API_KEY
PROXY_URL = config.PROXY_URL
RATE_LIMIT_SECONDS = config.RATE_LIMIT_SECONDS
ADMIN_USERNAME = config.ADMIN_USERNAME

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
    http_client=httpx.Client(
        proxy=PROXY_URL,
        timeout=30.0
    )
)


class RateLimiter:
    """Класс для управления rate limiting пользователей"""
    
    def __init__(self, json_file: str = TABLE_JSON):
        self.json_file = json_file
        self.data = self._load_data()
    
    def _load_data(self) -> Dict:
        try:
            with open(self.json_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def _save_data(self):
        with open(self.json_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
    
    def can_report(self, user_id: int) -> bool:
        user_key = str(user_id)
        current_time = time.time()
        
        if user_key not in self.data:
            self.data[user_key] = current_time
            self._save_data()
            return True
        
        last_report_time = self.data[user_key]
        if current_time - last_report_time >= RATE_LIMIT_SECONDS:
            self.data[user_key] = current_time
            self._save_data()
            return True
        
        return False


def check_message_with_ai(message_text: str) -> bool:
    """
    Проверяет сообщение через Groq AI
    Возвращает True если сообщение нормальное, False если нужно удалить
    """
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a chat moderator."},
                {"role": "user", "content": f"На сообщение поступил репорт: твоя задача проверить его по критериям и дать ответ в формате \"true\", если сообщение не нужно удалять, и \"false\" если нужно. Допустимо пропускать в сообщении нецензурную лексику, оскорбления не жесткие, средней тяжести (например \"ты дебил\" и т.п. - не удалять). А вот оскорбления к личности, родне и т.д. - нужно отмечать false. Также если в сообщении реклама в любом виде - также отправить false. 'кто хочет вступить в тиму - пишите в лс' - считается рекламой. Сообщение с репортом: \"{message_text}\""}
            ],
            temperature=0.7,
            max_tokens=500
        )
        
        ai_response = response.choices[0].message.content.strip().lower()
        logger.info(f"AI ответ: {ai_response}")
        
        # парсим ответ AI
        if "false" in ai_response:
            return False
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при обращении к AI: {e}")
        return True


def log_deleted_message(message_data: dict):
    """Логирует удалённое сообщение в main.log"""
    try:
        with open(MAIN_LOG, 'a', encoding='utf-8') as f:
            log_entry = {
                'timestamp': datetime.now().isoformat(),
                'user_id': message_data.get('user_id'),
                'username': message_data.get('username'),
                'message_text': message_data.get('text'),
                'chat_id': message_data.get('chat_id')
            }
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            logger.info(f"Сообщение залогировано: {log_entry}")
    except Exception as e:
        logger.error(f"Ошибка при логировании: {e}")

rate_limiter = RateLimiter()


@dp.message(Command("track"))
async def handle_track_command(message: Message):
    """Обработчик команды /track"""
    await process_report(message)


@dp.message(F.text.startswith("!нарушение"))
async def handle_violation_command(message: Message):
    """Обработчик команды !нарушение"""
    await process_report(message)


async def process_report(message: Message):
    """Общая обработка репорта"""
    if not message.reply_to_message:
        try:
            await message.delete()
            logger.info(f"Удалена команда без ответа от пользователя {message.from_user.id}")
        except Exception as e:
            logger.error(f"Не удалось удалить команду без ответа: {e}")
        return
    
    user_id = message.from_user.id
    
    if not rate_limiter.can_report(user_id):
        try:
            await message.delete()
            logger.info(f"Удалена команда из-за rate limit от пользователя {user_id}")
        except Exception as e:
            logger.error(f"Не удалось удалить команду с rate limit: {e}")
        return
    
    
    reported_message = message.reply_to_message
    reported_text = reported_message.text or reported_message.caption or ""
    
    if not reported_text:
        logger.warning("Сообщение без текста, пропускаем проверку")
        return
    
    try:
        notification = await message.answer(
            text="_Сообщение отправлено на проверку_",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Уведомление отправлено, ID: {notification.message_id}")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление: {e}")
        return
    
    logger.info(f"Проверяем сообщение: {reported_text}")
    
    # проверяем через AI
    is_ok = check_message_with_ai(reported_text)
    
    if not is_ok:
        try:
            message_data = {
                'user_id': reported_message.from_user.id,
                'username': reported_message.from_user.username,
                'text': reported_text,
                'chat_id': reported_message.chat.id
            }
            
            log_deleted_message(message_data)
            
            await reported_message.delete()
            logger.info(f"Сообщение удалено: {reported_text}")
        except Exception as e:
            logger.error(f"Не удалось удалить нарушающее сообщение: {e}")
    else:
        logger.info(f"Сообщение прошло проверку: {reported_text}")


@dp.message(Command("sendinfo"))
async def handle_sendinfo_command(message: Message):
    """Обработчик команды /sendinfo"""
    await process_sendinfo(message)


@dp.message(F.text.startswith("!sendinfo"))
async def handle_sendinfo_exclamation(message: Message):
    """Обработчик команды !sendinfo"""
    await process_sendinfo(message)


async def process_sendinfo(message: Message):
    """Общая обработка команды sendinfo"""
    user = message.from_user
    
    if user.username != ADMIN_USERNAME:
        logger.warning(f"Попытка использования /sendinfo от {user.username}")
        try:
            await message.delete()
        except Exception as e:
            logger.error(f"Не удалось удалить неавторизованную команду: {e}")
        return
    
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Не удалось удалить команду sendinfo: {e}")
    
    try:
        await message.answer(
            text="**Если вы увидели нарушение - отправьте /track или !нарушение ответом на сообщение, которое нужно проверить**",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Не удалось отправить инфо-сообщение: {e}")


async def main():
    """Запуск бота"""
    logger.info("Бот запущен")
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
