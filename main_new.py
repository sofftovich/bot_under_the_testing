import asyncio
import json
import re
import time
import random
import logging
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, BotCommand
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Загрузка разрешённых пользователей
ALLOWED_USERS = []
for i in range(1, 4):
    user_id = os.getenv(f"ALLOWED_USER_{i}")
    if user_id:
        try:
            ALLOWED_USERS.append(int(user_id))
        except ValueError:
            logger.warning(f"Неверный формат ALLOWED_USER_{i}: {user_id}")

if not ALLOWED_USERS:
    logger.error("❌ Не указано ни одного разрешённого пользователя!")
    exit(1)

logger.info(f"✅ Разрешённые пользователи: {ALLOWED_USERS}")

if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен!")
    exit(1)

# Глобальные настройки
POST_INTERVAL = None
last_post_time = 0
posting_enabled = True
DEFAULT_SIGNATURE = None
ALLOWED_WEEKDAYS = None
START_TIME = None
END_TIME = None
DELAYED_START_ENABLED = False
DELAYED_START_TIME = None
TIME_WINDOW_ENABLED = True
WEEKDAYS_ENABLED = False
EXACT_TIMING_ENABLED = True
NOTIFICATIONS_ENABLED = True

# Новые настройки для языка и часового пояса
USER_LANGUAGE = "en"  # По умолчанию английский
USER_TIMEZONE = "Europe/Prague"  # По умолчанию чешский часовой пояс

# Файлы для хранения данных
QUEUE_FILE = Path("queue.json")
STATE_FILE = Path("state.json")
LANG_DIR = Path("lang")

# Структуры данных
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}
is_posting_locked = False

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Загрузка языковых файлов
def load_language_files():
    """Загружает все языковые файлы"""
    texts = {}
    if LANG_DIR.exists():
        for lang_file in LANG_DIR.glob("*.yml"):
            lang_code = lang_file.stem
            try:
                with open(lang_file, 'r', encoding='utf-8') as f:
                    texts[lang_code] = yaml.safe_load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки языкового файла {lang_file}: {e}")
    return texts

# Глобальная переменная для текстов
TEXTS = load_language_files()

def get_text(key, language=None):
    """Получает текст по ключу для указанного языка"""
    if language is None:
        language = USER_LANGUAGE
    
    if language not in TEXTS:
        language = "en"  # Fallback на английский
    
    # Поддержка вложенных ключей через точку
    keys = key.split('.')
    value = TEXTS[language]
    
    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            # Fallback на английский
            value = TEXTS.get("en", {}).get(key, key)
            break
    
    return value if isinstance(value, str) else key

def get_user_time():
    """Получает текущее время в пользовательском часовом поясе"""
    return datetime.now(ZoneInfo(USER_TIMEZONE))

def check_user_access(user_id):
    return user_id in ALLOWED_USERS

def parse_interval(interval_str):
    """Парсит интервал с учётом дней, часов и минут (без секунд)"""
    total_seconds = 0
    patterns = [('d', 24*3600), ('h', 3600), ('m', 60)]

    for suffix, multiplier in patterns:
        match = re.search(rf'(\d+){suffix}', interval_str)
        if match:
            total_seconds += int(match.group(1)) * multiplier

    # Проверяем минимальный интервал (1 минута)
    if total_seconds > 0 and total_seconds < 60:
        return None  # Слишком короткий интервал
    
    return total_seconds if total_seconds > 0 else None

def format_interval(seconds):
    """Форматирует секунды в читаемый вид"""
    periods = [('д', 86400), ('ч', 3600), ('м', 60)]
    parts = []

    for name, period in periods:
        if seconds >= period:
            count = seconds // period
            parts.append(f"{count}{name}")
            seconds %= period

    return " ".join(parts) if parts else "0м"

def calculate_exact_posting_times():
    """Рассчитывает точные моменты времени для постинга в рамках временного окна"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return []

    # Если временное окно отключено или не назначено
    if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
        posting_times = []
        current_seconds = 0
        while current_seconds < 24 * 3600:
            hours = current_seconds // 3600
            minutes = (current_seconds % 3600) // 60
            if hours < 24:
                posting_times.append(dt_time(hours, minutes))
            current_seconds += POST_INTERVAL
        return posting_times

    # Рассчитываем продолжительность окна
    if START_TIME <= END_TIME:
        window_duration = (END_TIME.hour - START_TIME.hour) * 3600 + (END_TIME.minute - START_TIME.minute) * 60
    else:
        window_duration = (24 * 3600 - (START_TIME.hour * 3600 + START_TIME.minute * 60)) + (END_TIME.hour * 3600 + END_TIME.minute * 60)

    max_posts_in_window = max(1, int(window_duration // POST_INTERVAL))

    if max_posts_in_window == 1:
        return [START_TIME]

    posting_times = []
    start_seconds = START_TIME.hour * 3600 + START_TIME.minute * 60

    for i in range(max_posts_in_window):
        total_seconds = start_seconds + i * POST_INTERVAL
        if total_seconds >= 24 * 3600:
            total_seconds -= 24 * 3600

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        current_time = dt_time(hours, minutes)

        # Проверяем, что время в пределах окна (включая границы)
        if START_TIME <= END_TIME:
            if START_TIME <= current_time <= END_TIME:
                posting_times.append(current_time)
        else:
            if current_time >= START_TIME or current_time <= END_TIME:
                posting_times.append(current_time)

    return posting_times

def get_next_exact_posting_time():
    """Возвращает следующее точное время для постинга"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return None

    now = get_user_time()
    current_time = now.time()
    posting_times = calculate_exact_posting_times()

    if not posting_times:
        return None

    # Ищем ближайшее время сегодня
    for post_time in posting_times:
        if current_time < post_time:
            if not (WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and now.weekday() not in ALLOWED_WEEKDAYS):
                return now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)

    # Ищем следующий разрешённый день
    for days_ahead in range(1, 8):
        check_date = now + timedelta(days=days_ahead)
        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_date.weekday() in ALLOWED_WEEKDAYS:
            first_time = posting_times[0]
            return check_date.replace(hour=first_time.hour, minute=first_time.minute, second=0, microsecond=0)

    return None

def calculate_queue_schedule(queue_length):
    """Рассчитывает расписание для всей очереди"""
    if queue_length == 0:
        return None, None

    if EXACT_TIMING_ENABLED:
        next_time = get_next_exact_posting_time()
        if not next_time:
            return None, None

        posting_times = calculate_exact_posting_times()
        if not posting_times:
            return None, None

        # Находим индекс текущего времени
        current_time_index = 0
        for i, post_time in enumerate(posting_times):
            if abs((post_time.hour * 60 + post_time.minute) - (next_time.time().hour * 60 + next_time.time().minute)) <= 1:
                current_time_index = i
                break

        # Рассчитываем время последнего поста
        last_time_index = (current_time_index + queue_length - 1) % len(posting_times)
        days_offset = (current_time_index + queue_length - 1) // len(posting_times)

        last_post_time = posting_times[last_time_index]
        last_post_date = next_time.date() + timedelta(days=days_offset)
        last_post_datetime = datetime.combine(last_post_date, last_post_time, tzinfo=ZoneInfo(USER_TIMEZONE))

        return next_time, last_post_datetime
    else:
        now = get_user_time()
        first_post_time = now + timedelta(seconds=get_time_until_next_post())
        last_post_time = first_post_time + timedelta(seconds=(queue_length - 1) * POST_INTERVAL)
        return first_post_time, last_post_time

def get_time_until_next_post():
    """Возвращает время до следующего поста в секундах"""
    if POST_INTERVAL is None:
        return 24 * 3600

    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_user_time()
            return max(0, int((next_exact_time - now).total_seconds()))
        return 0

    # Интервальное планирование
    now_timestamp = time.time()
    time_since_last = now_timestamp - last_post_time

    if time_since_last >= POST_INTERVAL:
        return get_next_allowed_time()
    else:
        interval_wait = POST_INTERVAL - int(time_since_last)
        allowed_wait = get_next_allowed_time()
        return max(interval_wait, allowed_wait)

def get_next_allowed_time():
    """Возвращает время до следующего разрешённого интервала"""
    now = get_user_time()

    if is_posting_allowed()[0]:
        return 0

    # Ищем следующий разрешённый интервал
    for days_ahead in range(8):
        check_date = now + timedelta(days=days_ahead)
        check_date = check_date.replace(hour=0, minute=0, second=0, microsecond=0)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:
                if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
                    return 0
                elif START_TIME <= END_TIME:
                    if now.time() < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
                    elif now.time() > END_TIME:
                        continue
                else:
                    if END_TIME < now.time() < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
            else:
                if not TIME_WINDOW_ENABLED or START_TIME is None:
                    return int((check_date - now).total_seconds())
                else:
                    target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                    return int((target_time - now).total_seconds())

    return 24 * 3600

def load_queue():
    """Загружает очередь из файла"""
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки очереди: {e}")
        return []

def save_queue(queue):
    """Сохраняет очередь в файл"""
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения очереди: {e}")

def load_state():
    """Загружает состояние бота"""
    global last_post_time, CHANNEL_ID, posting_enabled
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    global USER_LANGUAGE, USER_TIMEZONE

    # Дефолтные значения
    posting_enabled = True
    TIME_WINDOW_ENABLED = True
    WEEKDAYS_ENABLED = False
    EXACT_TIMING_ENABLED = True
    NOTIFICATIONS_ENABLED = True
    USER_LANGUAGE = "en"
    USER_TIMEZONE = "Europe/Prague"

    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                last_post_time = state.get("last_post_time", 0)
                saved_channel = state.get("channel_id")
                if saved_channel:
                    CHANNEL_ID = saved_channel
                
                # Загружаем новые настройки
                USER_LANGUAGE = state.get("user_language", "en")
                USER_TIMEZONE = state.get("user_timezone", "Europe/Prague")
                
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")

def save_state():
    """Сохраняет состояние бота"""
    state = {
        "last_post_time": last_post_time,
        "default_signature": DEFAULT_SIGNATURE,
        "channel_id": CHANNEL_ID,
        "posting_enabled": posting_enabled,
        "post_interval": POST_INTERVAL,
        "allowed_weekdays": ALLOWED_WEEKDAYS,
        "start_time": START_TIME.strftime("%H:%M") if START_TIME else None,
        "end_time": END_TIME.strftime("%H:%M") if END_TIME else None,
        "delayed_start_enabled": DELAYED_START_ENABLED,
        "delayed_start_time": DELAYED_START_TIME.isoformat() if DELAYED_START_TIME else None,
        "time_window_enabled": TIME_WINDOW_ENABLED,
        "weekdays_enabled": WEEKDAYS_ENABLED,
        "exact_timing_enabled": EXACT_TIMING_ENABLED,
        "notifications_enabled": NOTIFICATIONS_ENABLED,
        "user_language": USER_LANGUAGE,
        "user_timezone": USER_TIMEZONE
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")

def is_posting_allowed():
    """Проверяет, разрешён ли сейчас постинг с учётом всех ограничений"""
    now = get_user_time()
    current_weekday = now.weekday()
    current_time = now.time()

    # Проверка дня недели
    if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and current_weekday not in ALLOWED_WEEKDAYS:
        weekday_name = get_text("weekdays", USER_LANGUAGE)[current_weekday]
        return False, get_text("status_not_allowed", USER_LANGUAGE).format(weekday=weekday_name)

    # Проверка временного окна (включая границы)
    if TIME_WINDOW_ENABLED and START_TIME is not None and END_TIME is not None:
        if START_TIME <= END_TIME:
            if not (START_TIME <= current_time <= END_TIME):
                return False, get_text("status_outside_window", USER_LANGUAGE).format(
                    start=START_TIME.strftime('%H:%M'), 
                    end=END_TIME.strftime('%H:%M')
                )
        else:
            if not (current_time >= START_TIME or current_time <= END_TIME):
                return False, get_text("status_outside_window", USER_LANGUAGE).format(
                    start=START_TIME.strftime('%H:%M'), 
                    end=END_TIME.strftime('%H:%M')
                )

    return True, get_text("status_allowed", USER_LANGUAGE)

def is_delayed_start_ready():
    """Проверяет, готов ли отложенный старт"""
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return True
    return get_user_time() >= DELAYED_START_TIME

def get_weekday_name(weekday):
    """Получает название дня недели на текущем языке"""
    weekdays = get_text("weekdays", USER_LANGUAGE)
    return weekdays[weekday] if 0 <= weekday < len(weekdays) else str(weekday)

def get_weekday_short(weekday):
    """Получает короткое название дня недели на текущем языке"""
    weekdays_short = get_text("weekdays_short", USER_LANGUAGE)
    return weekdays_short[weekday] if 0 <= weekday < len(weekdays_short) else str(weekday)

def count_queue_stats(queue):
    """Подсчитывает статистику очереди"""
    total_media = 0
    media_groups = 0
    total_posts = len(queue)
    photos = videos = gifs = documents = 0

    for item in queue:
        if isinstance(item, dict) and item.get("type") == "media_group":
            media_groups += 1
            for media in item.get("media", []):
                total_media += 1
                media_type = media["type"]
                if media_type == "photo":
                    photos += 1
                elif media_type == "video":
                    videos += 1
                elif media_type == "gif":
                    gifs += 1
                elif media_type == "document":
                    documents += 1
        else:
            total_media += 1
            if isinstance(item, dict):
                media_type = item.get("type", "photo")
                if media_type == "photo":
                    photos += 1
                elif media_type == "video":
                    videos += 1
                elif media_type == "gif":
                    gifs += 1
                elif media_type == "document":
                    documents += 1
            else:
                photos += 1

    return total_media, media_groups, total_posts, photos, videos, gifs, documents

def format_queue_stats(queue):
    """Форматирует статистику очереди в читаемый вид"""
    total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)

    parts = []
    if photos > 0:
        media_type = get_text("media_types.photo", USER_LANGUAGE)
        parts.append(f"{photos} {media_type}")
    if videos > 0:
        media_type = get_text("media_types.video", USER_LANGUAGE)
        parts.append(f"{videos} {media_type}")
    if gifs > 0:
        media_type = get_text("media_types.gif", USER_LANGUAGE)
        parts.append(f"{gifs} {media_type}")
    if documents > 0:
        media_type = get_text("media_types.document", USER_LANGUAGE)
        parts.append(f"{documents} {media_type}")
    if media_groups > 0:
        media_type = get_text("media_types.media_group", USER_LANGUAGE)
        parts.append(f"{media_groups} {media_type}")
    parts.append(f"{total_posts} постов")

    return " | ".join(parts)

def shuffle_queue():
    """Перемешивает очередь"""
    queue = load_queue()
    if len(queue) > 1:
        random.shuffle(queue)
        save_queue(queue)
        return True
    return False

def update_user_tracking_after_post():
    """Обновляет отслеживание пользователей после поста"""
    global user_media_tracking
    updated_tracking = {}
    for idx, uid in user_media_tracking.items():
        if idx > 0:
            updated_tracking[idx - 1] = uid
    user_media_tracking = updated_tracking

def add_user_to_queue_tracking(user_id, queue_position):
    """Добавляет пользователя в отслеживание очереди"""
    user_media_tracking[queue_position] = user_id

def get_users_for_next_post():
    """Получает пользователей для следующего поста"""
    if 0 in user_media_tracking:
        return [user_media_tracking[0]]
    return []

def parse_signature_with_link(text):
    """Парсит подпись в формате 'текст # ссылка' и возвращает HTML с кликабельной ссылкой"""
    if " # " in text:
        parts = text.rsplit(" # ", 1)
        if len(parts) == 2:
            caption_text = parts[0].strip()
            link_url = parts[1].strip()

            if (link_url and 
                ('.' in link_url or 
                 link_url.startswith(("http://", "https://", "t.me/", "tg://")))):

                if not link_url.startswith(("http://", "https://", "tg://")):
                    link_url = "https://" + link_url

                return f'<a href="{link_url}">{caption_text}</a>'

    return text

def apply_signature_to_all_queue(signature):
    """Применяет подпись ко всем постам в очереди"""
    queue = load_queue()
    if not queue:
        return 0

    parsed_signature = parse_signature_with_link(signature)
    updated_count = 0

    for i, item in enumerate(queue):
        if isinstance(item, dict):
            item["caption"] = parsed_signature
        else:
            queue[i] = {"file_id": item, "caption": parsed_signature, "type": "photo"}
        updated_count += 1

    save_queue(queue)
    return updated_count

async def verify_post_published(channel_id, expected_type=None, timeout=5):
    """Проверяет, что пост действительно опубликован в канале"""
    try:
        await asyncio.sleep(1)
        await bot.get_chat_member_count(channel_id)
        await bot.get_chat(channel_id)
        logger.info(f"✅ Канал {channel_id} доступен, пост считается опубликованным")
        return True
    except Exception as e:
        logger.error(f"Ошибка проверки публикации: {e}")
        return False

async def send_media_group_to_channel(media_group_data):
    """Отправляет медиагруппу в канал"""
    try:
        media_group = MediaGroupBuilder()

        for i, media in enumerate(media_group_data["media"]):
            caption = media_group_data["caption"] if i == 0 else None

            if media["type"] == "photo":
                media_group.add_photo(media=media["file_id"], caption=caption)
            elif media["type"] == "video":
                media_group.add_video(media=media["file_id"], caption=caption)
            elif media["type"] == "document":
                media_group.add_document(media=media["file_id"], caption=caption)

        await bot.send_media_group(chat_id=CHANNEL_ID, media=media_group.build())
        logger.info(f"✅ Медиагруппа из {len(media_group_data['media'])} элементов опубликована")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки медиагруппы: {e}")
        raise e

async def notify_users_about_publication(media_type, is_success=True, error_msg=None):
    """Отправляет уведомления пользователям о публикации их медиа"""
    if not NOTIFICATIONS_ENABLED or not pending_notifications:
        return

    users_to_notify = list(pending_notifications.keys())

    for user_id in users_to_notify:
        try:
            if is_success:
                if media_type == "media_group":
                    message_text = get_text("notify_success_group", USER_LANGUAGE)
                else:
                    media_type_name = get_text(f"media_types.{media_type}", USER_LANGUAGE)
                    message_text = get_text("notify_success_single", USER_LANGUAGE).format(type=media_type_name)
            else:
                media_type_name = get_text(f"media_types.{media_type}", USER_LANGUAGE)
                message_text = get_text("notify_error", USER_LANGUAGE).format(type=media_type_name, error=error_msg or "Unknown error")

            await bot.send_message(user_id, message_text)
            logger.info(f"✅ Уведомление отправлено пользователю {user_id}")

        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления пользователю {user_id}: {e}")

    # Очищаем уведомления
    pending_notifications.clear()

async def send_single_media(media_data):
    """Отправляет одиночное медиа в канал"""
    try:
        caption = media_data.get("caption", "")
        media_type = media_data.get("type", "photo")

        if media_type == "photo":
            await bot.send_photo(chat_id=CHANNEL_ID, photo=media_data["file_id"], caption=caption)
        elif media_type == "video":
            await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], caption=caption)
        elif media_type == "document":
            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=caption)

        logger.info(f"✅ {media_type} опубликовано")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки {media_type}: {e}")
        raise e

async def post_next_media():
    """Публикует следующее медиа из очереди с новой логикой времени"""
    global last_post_time

    queue = load_queue()
    if not queue:
        return

    posting_allowed, reason = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()

    if not posting_enabled:
        logger.info("🔴 Автопостинг отключён")
        return

    if not posting_allowed:
        logger.info(f"⏰ Постинг запрещён: {reason}")
        return

    if not delayed_ready:
        logger.info(f"⏰ Ожидание отложенного старта до {DELAYED_START_TIME}")
        return

    if not CHANNEL_ID:
        logger.error("❌ CHANNEL_ID не установлен")
        return

    # Новая логика: бот целится заранее за минуту до запланированного времени
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_user_time()
            time_diff = (next_exact_time - now).total_seconds()

            # Бот ждёт до 10 секунд до запланированного времени
            if time_diff > 10:
                logger.info(f"⏰ Ожидание точного времени постинга: {next_exact_time.strftime('%H:%M:%S')} (через {int(time_diff)}с)")
                return
            elif time_diff > 0:
                # Ждём до наступления запланированного времени
                logger.info(f"⏰ Ожидание {int(time_diff)} секунд до запланированного времени")
                await asyncio.sleep(time_diff)

    # Публикуем медиа
    media_data = queue.pop(0)
    published_successfully = False

    # Получаем пользователей для уведомления
    users_for_notification = get_users_for_next_post()
    if users_for_notification:
        for user_id in users_for_notification:
            pending_notifications[user_id] = True

    try:
        if isinstance(media_data, dict) and media_data.get("type") == "media_group":
            await send_media_group_to_channel(media_data)
            verification_success = await verify_post_published(CHANNEL_ID, "media_group")

            if verification_success:
                published_successfully = True
                await notify_users_about_publication("media_group", True)
                logger.info("✅ Медиагруппа успешно опубликована и пользователи уведомлены")
            else:
                await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию в канале")
                logger.error("❌ Не удалось подтвердить публикацию медиагруппы в канале")
        else:
            await send_single_media(media_data)
            media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
            verification_success = await verify_post_published(CHANNEL_ID, media_type)

            if verification_success:
                published_successfully = True
                await notify_users_about_publication(media_type, True)
                logger.info(f"✅ {media_type} успешно опубликовано и пользователи уведомлены")
            else:
                await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию в канале")
                logger.error(f"❌ Не удалось подтвердить публикацию {media_type} в канале")

        if published_successfully:
            last_post_time = time.time()
            save_state()
            update_user_tracking_after_post()

    except Exception as e:
        logger.error(f"❌ Ошибка отправки медиа: {e}")
        await notify_users_about_publication("медиа", False, str(e))
        queue.insert(0, media_data)

    save_queue(queue)

async def posting_loop():
    """Основной цикл постинга"""
    logger.info("🔄 Запущен цикл автопостинга")

    while True:
        try:
            queue = load_queue()
            if queue and posting_enabled and CHANNEL_ID:
                time_until_next = get_time_until_next_post()

                if time_until_next <= 0:
                    await post_next_media()
                    await asyncio.sleep(3)
                else:
                    # Убираем костыль sleep(30) - теперь бот работает более точно
                    sleep_time = min(time_until_next, 60)  # Максимум 1 минута
                    await asyncio.sleep(sleep_time)
            else:
                await asyncio.sleep(15)

        except Exception as e:
            logger.error(f"❌ Ошибка в цикле постинга: {e}")
            await asyncio.sleep(30)

# Продолжение в следующей части...