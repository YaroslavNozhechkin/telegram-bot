import os
import telebot
from telebot import types
import threading
import time
import qrcode
from io import BytesIO
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
from dotenv import load_dotenv
import logging
import atexit
import sys
import psycopg2
from psycopg2 import pool
import traceback

# ========== ЗАГРУЗКА КОНФИГУРАЦИИ ==========
# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Получаем токены из переменных окружения
ADMIN_BOT_TOKEN = os.getenv('ADMIN_BOT_TOKEN')
USER_BOT_TOKEN = os.getenv('USER_BOT_TOKEN')

# Получаем ID админов (через запятую)
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()]

# Получаем строку подключения к PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    logger.error("❌ DATABASE_URL не найден!")
    print("❌ ОШИБКА: DATABASE_URL не найден в переменных окружения!")
    print("   Для Railway: Добавьте PostgreSQL базу данных и переменную DATABASE_URL будет создана автоматически")

# Проверка токенов (для отладки)
if not ADMIN_BOT_TOKEN:
    logger.error("❌ ADMIN_BOT_TOKEN не найден! Проверьте файл .env")
    print("❌ ОШИБКА: ADMIN_BOT_TOKEN не найден в .env файле!")
    print("   Убедитесь что в .env есть строка: ADMIN_BOT_TOKEN=ваш_токен")

if not USER_BOT_TOKEN:
    logger.error("❌ USER_BOT_TOKEN не найден! Проверьте файл .env")
    print("❌ ОШИБКА: USER_BOT_TOKEN не найден в .env файле!")

if not ADMIN_IDS:
    logger.warning("⚠️ ADMIN_IDS пустой! Вы не сможете использовать админ-команды")
    print("⚠️ ВНИМАНИЕ: ADMIN_IDS пустой в .env файле!")

# Создаем боты
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
user_bot = telebot.TeleBot(USER_BOT_TOKEN)

logger.info("🤖 Боты инициализированы")
print("✅ Токены загружены из .env файла")
print(f"👑 ID админов: {ADMIN_IDS}")

# ========== СОЗДАНИЕ ПОДКЛЮЧЕНИЯ К POSTGRESQL ==========
connection_pool = None

def init_database():
    """Инициализирует подключение к PostgreSQL и создает таблицы"""
    global connection_pool
    
    try:
        # Создаем пул соединений
        connection_pool = psycopg2.pool.SimpleConnectionPool(
            1,  # минимальное количество соединений
            20,  # максимальное количество соединений
            DATABASE_URL,
            sslmode='require'  # для Railway требуется SSL
        )
        
        print("✅ Подключение к PostgreSQL установлено")
        
        # Создаем таблицы
        create_tables()
        
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к PostgreSQL: {e}")
        traceback.print_exc()
        return False

def get_connection():
    """Получает соединение из пула"""
    return connection_pool.getconn()

def return_connection(conn):
    """Возвращает соединение в пул"""
    connection_pool.putconn(conn)

def execute_query(query, params=None, fetchone=False, fetchall=False):
    """Выполняет SQL запрос"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())
            
            if fetchone:
                result = cursor.fetchone()
            elif fetchall:
                result = cursor.fetchall()
            else:
                result = None
                
            conn.commit()
            return result
    except Exception as e:
        print(f"❌ Ошибка выполнения запроса: {e}")
        print(f"Запрос: {query}")
        print(f"Параметры: {params}")
        conn.rollback()
        raise e
    finally:
        return_connection(conn)

def create_tables():
    """Создает все необходимые таблицы в PostgreSQL"""
    tables = [
        '''CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            surname TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        
        '''CREATE TABLE IF NOT EXISTS events (
            event_id SERIAL PRIMARY KEY,
            event_name TEXT NOT NULL,
            event_photo_id TEXT,
            invitation_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        
        '''CREATE TABLE IF NOT EXISTS user_responses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            event_id INTEGER NOT NULL,
            response TEXT NOT NULL,
            qr_sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, event_id)
        )''',
        
        '''CREATE TABLE IF NOT EXISTS invitation_messages (
            id SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            event_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, event_id)
        )''',
        
        '''CREATE TABLE IF NOT EXISTS attendance (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            event_name TEXT NOT NULL,
            attendance_status INTEGER DEFAULT 0,  -- 0 = не отсканирован, 1 = отсканирован
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, event_name)
        )'''
    ]
    
    # Создаем таблицы
    for table_query in tables:
        execute_query(table_query)
    
    # Создаем индексы
    indexes = [
        'CREATE INDEX IF NOT EXISTS idx_user_responses_user_event ON user_responses (user_id, event_id)',
        'CREATE INDEX IF NOT EXISTS idx_invitation_messages_user_event ON invitation_messages (user_id, event_id)',
        'CREATE INDEX IF NOT EXISTS idx_attendance_user_event ON attendance (user_id, event_name)',
        'CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users (telegram_id)',
        'CREATE INDEX IF NOT EXISTS idx_events_event_id ON events (event_id)'
    ]
    
    for index_query in indexes:
        try:
            execute_query(index_query)
        except:
            pass  # Индекс уже существует
    
    print("✅ Все таблицы PostgreSQL созданы/проверены")

# Инициализируем базу данных
if not init_database():
    print("❌ Не удалось инициализировать базу данных!")
    sys.exit(1)

print("🤖 Запуск системы приглашений...")

# ========== КЛАВИАТУРЫ ==========
# Обычная пользовательская клавиатура (используется после регистрации)
user_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
user_keyboard.add("📝 Регистрация (/start)", "🆔 Мой ID (/id)")
user_keyboard.add("👑 Админ (/admin)")

# Клавиатура для отмены в админ боте
cancel_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
cancel_keyboard.add("❌ Отмена")

# Клавиатура для команды /admin в пользовательском боте
admin_cancel_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
admin_cancel_keyboard.add("❌ Отмена")

# Клавиатура админа
admin_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
admin_keyboard.add("/Sending_messages", "/scan_qr", "/announce", "/edit_user", "/cancel")

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ==========
def create_inline_keyboard(event_id):
    """Создает инлайн-клавиатуру для конкретного мероприятия"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    callback_data_yes = f"response_yes_event_{event_id}"
    callback_data_no = f"response_no_event_{event_id}"

    keyboard.add(
        types.InlineKeyboardButton("✅ Да, буду участвовать", callback_data=callback_data_yes),
        types.InlineKeyboardButton("❌ Нет, не смогу", callback_data=callback_data_no)
    )

    return keyboard

def create_qr_code(event_number, user_id):
    """Создает QR-код с данными: номер мероприятия + 'U' + ID пользователя"""
    qr_data = f"{event_number}U{user_id}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)

    return bio, qr_data

def get_next_event_number():
    """Получает следующий номер мероприятия"""
    result = execute_query('SELECT MAX(event_id) FROM events', fetchone=True)
    if result and result[0] is not None:
        return result[0] + 1
    return 1

def check_user_response(user_id, event_id):
    """Проверяет ответ пользователя на приглашение"""
    return execute_query(
        'SELECT response, qr_sent FROM user_responses WHERE user_id = %s AND event_id = %s',
        (user_id, event_id),
        fetchone=True
    )

def save_user_response(user_id, event_id, response):
    """Сохраняет ответ пользователя"""
    try:
        execute_query(
            '''INSERT INTO user_responses (user_id, event_id, response, qr_sent) 
               VALUES (%s, %s, %s, FALSE)
               ON CONFLICT (user_id, event_id) 
               DO UPDATE SET response = EXCLUDED.response, qr_sent = FALSE''',
            (user_id, event_id, response)
        )
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения ответа: {e}")
        return False

def mark_qr_sent(user_id, event_id):
    """Отмечает что QR-код отправлен"""
    try:
        execute_query(
            'UPDATE user_responses SET qr_sent = TRUE WHERE user_id = %s AND event_id = %s',
            (user_id, event_id)
        )
        return True
    except Exception as e:
        print(f"❌ Ошибка обновления статуса QR: {e}")
        return False

def save_invitation_message(user_id, event_id, message_id):
    """Сохраняет ID сообщения с приглашением"""
    try:
        execute_query(
            '''INSERT INTO invitation_messages (user_id, event_id, message_id) 
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, event_id) 
               DO UPDATE SET message_id = EXCLUDED.message_id''',
            (user_id, event_id, message_id)
        )
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения ID сообщения: {e}")
        return False

def get_invitation_message_id(user_id, event_id):
    """Получает ID сообщения с приглашением"""
    result = execute_query(
        'SELECT message_id FROM invitation_messages WHERE user_id = %s AND event_id = %s',
        (user_id, event_id),
        fetchone=True
    )
    return result[0] if result else None

def mark_attendance(user_id, event_name):
    """Отмечает посещение пользователя"""
    try:
        # Проверяем, не отмечен ли уже пользователь
        existing = execute_query(
            'SELECT attendance_status FROM attendance WHERE user_id = %s AND event_name = %s',
            (user_id, event_name),
            fetchone=True
        )

        if existing and existing[0] == 1:
            return "already_scanned"  # Уже отсканирован

        # Добавляем или обновляем запись
        execute_query(
            '''INSERT INTO attendance (user_id, event_name, attendance_status) 
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, event_name) 
               DO UPDATE SET attendance_status = EXCLUDED.attendance_status, scanned_at = CURRENT_TIMESTAMP''',
            (user_id, event_name, 1)
        )
        return "success"
    except Exception as e:
        print(f"❌ Ошибка отметки посещения: {e}")
        return "error"

def decode_qr_code_from_photo(file_path):
    """УЛУЧШЕННАЯ функция сканирования QR-кодов"""
    try:
        # Загружаем изображение
        pil_img = Image.open(file_path)

        # Проверяем, нужно ли увеличить изображение
        width, height = pil_img.size
        if width < 300 or height < 300:
            new_width = max(600, width * 3)
            new_height = max(600, height * 3)
            pil_img = pil_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Сохраняем оригинал для разных методов
        original_img = pil_img.copy()

        # Конвертируем в OpenCV формат
        img = cv2.cvtColor(np.array(original_img), cv2.COLOR_RGB2BGR)

        # Пробуем разные методы сканирования
        qr_detector = cv2.QRCodeDetector()

        # Список методов обработки
        processing_methods = [
            ("Оригинал", img),
            ("Черно-белое", cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)),
            ("Повышенная яркость", cv2.convertScaleAbs(img, alpha=1.5, beta=40)),
            ("Высокий контраст", cv2.convertScaleAbs(img, alpha=2.0, beta=0)),
            ("Размытие + резкость", cv2.GaussianBlur(img, (5, 5), 0)),
            ("Медианный фильтр", cv2.medianBlur(img, 3)),
            ("Бинаризация", cv2.adaptiveThreshold(
                cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 11, 2
            )),
        ]

        found_data = []

        for method_name, processed_img in processing_methods:
            try:
                data, bbox, _ = qr_detector.detectAndDecode(processed_img)
                if data and len(data) > 0:
                    found_data.append((method_name, data))
            except:
                pass

        # Если нашли несколько результатов, выбираем самый частый
        if found_data:
            # Ищем самый частый результат
            data_counts = {}
            for _, data in found_data:
                data_counts[data] = data_counts.get(data, 0) + 1

            most_common_data = max(data_counts.items(), key=lambda x: x[1])
            return most_common_data[0]

        # Дополнительный метод: инверсия цветов
        try:
            inverted = cv2.bitwise_not(img)
            data, bbox, _ = qr_detector.detectAndDecode(inverted)
            if data and len(data) > 0:
                return data
        except:
            pass

        return None

    except Exception as e:
        print(f"❌ Ошибка сканирования: {e}")
        return None

def enhanced_qr_decode(file_path):
    """УЛУЧШЕННАЯ функция сканирования QR-кодов с дополнительными методами"""
    try:
        # Загружаем изображение
        pil_img = Image.open(file_path)

        # Улучшаем качество изображения различными способами
        methods = []

        # Метод 1: Увеличение контраста
        img1 = pil_img.copy()
        enhancer = ImageEnhance.Contrast(img1)
        img1 = enhancer.enhance(2.0)
        methods.append(("Высокий контраст", img1))

        # Метод 2: Увеличение резкости
        img2 = pil_img.copy()
        enhancer = ImageEnhance.Sharpness(img2)
        img2 = enhancer.enhance(3.0)
        methods.append(("Высокая резкость", img2))

        # Метод 3: Черно-белое с высоким контрастом
        img3 = pil_img.copy()
        img3 = ImageOps.grayscale(img3)
        enhancer = ImageEnhance.Contrast(img3)
        img3 = enhancer.enhance(3.0)
        methods.append(("Черно-белый контраст", img3))

        # Метод 4: Инверсия цветов
        img4 = pil_img.copy()
        if img4.mode == 'RGB':
            img4 = ImageOps.invert(img4)
        methods.append(("Инверсия цветов", img4))

        # Метод 5: Увеличение размера
        img5 = pil_img.copy()
        width, height = img5.size
        img5 = img5.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
        methods.append(("Увеличенный размер", img5))

        # Метод 6: Автоконтраст
        img6 = pil_img.copy()
        img6 = ImageOps.autocontrast(img6, cutoff=2)
        methods.append(("Автоконтраст", img6))

        # Пробуем все методы
        qr_detector = cv2.QRCodeDetector()

        for method_name, processed_img in methods:
            try:
                # Конвертируем PIL в OpenCV
                opencv_img = cv2.cvtColor(np.array(processed_img), cv2.COLOR_RGB2BGR)

                # Пробуем сканировать
                data, bbox, _ = qr_detector.detectAndDecode(opencv_img)
                if data and len(data) > 0:
                    return data

            except:
                continue

        # Если не нашли, пробуем комбинации методов
        # Комбинация: увеличение размера + контраст
        combined_img = pil_img.copy()
        width, height = combined_img.size
        combined_img = combined_img.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
        combined_img = ImageOps.autocontrast(combined_img, cutoff=5)
        enhancer = ImageEnhance.Sharpness(combined_img)
        combined_img = enhancer.enhance(3.0)

        try:
            opencv_img = cv2.cvtColor(np.array(combined_img), cv2.COLOR_RGB2BGR)
            data, bbox, _ = qr_detector.detectAndDecode(opencv_img)
            if data and len(data) > 0:
                return data
        except:
            pass

        return None

    except Exception as e:
        print(f"❌ Ошибка в улучшенном сканировании: {e}")
        return None

def send_invitation_to_user(user_id, name, surname, event_id, event_name, invitation_text, event_photo_id=None):
    """Отправляет приглашение пользователю с инлайн-кнопками и фотографией"""
    try:
        # Формируем приглашение
        invitation = (
            f"🎫 *Приглашение на мероприятие*\n\n"
            f"Здравствуйте, *{name} {surname}*!\n\n"
            f"Вы приглашены на мероприятие:\n"
            f"*{event_name}* (№{event_id})\n\n"
            f"📝 *Описание:*\n"
            f"{invitation_text}\n\n"
            f"❓ *Вы желаете поучаствовать?*\n\n"
            f"_Нажмите одну из кнопок ниже для ответа:_"
        )

        # Создаем инлайн-клавиатуру
        keyboard = create_inline_keyboard(event_id)

        if event_photo_id:
            try:
                # Скачиваем фото через админ-бота
                file_info = admin_bot.get_file(event_photo_id)
                downloaded_file = admin_bot.download_file(file_info.file_path)

                # Создаем объект BytesIO для отправки через пользовательский бот
                photo_bytes = BytesIO(downloaded_file)
                photo_bytes.seek(0)

                # Отправляем фото через пользовательский бот
                sent_message = user_bot.send_photo(
                    user_id,
                    photo_bytes,
                    caption=invitation,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )

                photo_bytes.close()

            except Exception as photo_error:
                print(f"❌ Ошибка отправки фото пользователю {user_id}: {photo_error}")
                # Если не удалось отправить фото, отправляем текстовое приглашение
                sent_message = user_bot.send_message(
                    user_id,
                    invitation,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
        else:
            # Если фото нет, отправляем только текст
            sent_message = user_bot.send_message(
                user_id,
                invitation,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        # Сохраняем ID сообщения
        save_invitation_message(user_id, event_id, sent_message.message_id)

        return True

    except Exception as e:
        print(f"❌ Ошибка отправки приглашения пользователю {user_id}: {e}")
        return False

def broadcast_message_to_all(chat_id, message_text):
    """Рассылает сообщение всем пользователям"""
    try:
        users = execute_query('SELECT telegram_id, name, surname FROM users', fetchall=True)

        sent = 0
        failed = 0

        broadcast_message = (
            f"📢 *Оповещение от администратора*\n\n"
            f"{message_text}"
        )

        admin_bot.send_message(chat_id,
                               f"📤 Начинаю рассылку сообщения...\n\n"
                               f"👥 Пользователей: {len(users) if users else 0}\n"
                               f"📝 Сообщение: {message_text[:50]}...")

        if users:
            for user in users:
                user_id, name, surname = user
                try:
                    # Отправляем через пользовательского бота
                    user_bot.send_message(
                        user_id,
                        broadcast_message,
                        parse_mode='Markdown'
                    )
                    sent += 1
                    time.sleep(0.2)  # Пауза чтобы не было ограничений

                except Exception as e:
                    failed += 1
                    print(f"❌ Ошибка отправки пользователю {user_id}: {e}")

        stats_message = (
            f"✅ Рассылка завершена!\n\n"
            f"📝 Сообщение: {message_text[:100]}...\n"
            f"👥 Всего пользователей: {len(users) if users else 0}\n"
            f"✅ Успешно отправлено: {sent}\n"
            f"❌ Не удалось отправить: {failed}"
        )

        admin_bot.send_message(chat_id, stats_message, reply_markup=admin_keyboard)
        return True

    except Exception as e:
        print(f"❌ Ошибка в рассылке: {e}")
        admin_bot.send_message(chat_id,
                               f"❌ Ошибка рассылки: {str(e)[:200]}",
                               reply_markup=admin_keyboard)
        return False

# ========== ПОЛЬЗОВАТЕЛЬСКИЙ БОТ ==========
user_data = {}

def is_command(text):
    """Проверяет, является ли текст командой (начинается с /)"""
    return text and text.startswith('/')

def is_invalid_name(text):
    """Проверяет, является ли текст недопустимым для имени/фамилии"""
    # Проверяем команды
    if is_command(text):
        return True

    # Проверяем слишком короткие имена (менее 2 символов)
    if len(text.strip()) < 2:
        return True

    # Проверяем, содержит ли текст только цифры или спецсимволы
    if text.strip().isdigit():
        return True

    # Проверяем наличие недопустимых символов
    invalid_chars = set('!@#$%^&*()_+=[]{}|;:,.<>?~`"')
    if any(char in invalid_chars for char in text):
        return True

    return False

def is_user_registered(user_id):
    """Проверяет, зарегистрирован ли пользователь"""
    result = execute_query(
        'SELECT telegram_id FROM users WHERE telegram_id = %s',
        (user_id,),
        fetchone=True
    )
    return result is not None

@user_bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id

    # Проверяем, не зарегистрирован ли пользователь уже
    if is_user_registered(user_id):
        user_info = execute_query(
            'SELECT name, surname FROM users WHERE telegram_id = %s',
            (user_id,),
            fetchone=True
        )
        
        if user_info:
            name, surname = user_info

            already_registered_text = (
                "👋 *Вы уже зарегистрированы!*\n\n"
                f"👤 *Имя:* {name}\n"
                f"👥 *Фамилия:* {surname}\n\n"
                "✅ Вы уже зарегистрированы и будете получать приглашения на мероприятия.\n\n"
                "📱 *Доступные команды:*\n"
                "/admin - Проверить админ права\n"
                "/id - Узнать свой ID"
            )

            user_bot.send_message(message.chat.id, already_registered_text,
                                  parse_mode='Markdown',
                                  reply_markup=user_keyboard)
            return

    # Если не зарегистрирован, начинаем регистрацию
    user_data[user_id] = {'step': 'name'}

    welcome_text = (
        "👋 *Приветствую!*\n\n"
        "Этот бот служит для приглашения учеников на мероприятия.\n\n"
        "📝 *Сначала пройдите регистрацию:*\n\n"
        "Введите ваше имя:"
    )

    msg = user_bot.send_message(message.chat.id, welcome_text,
                                parse_mode='Markdown')
    user_bot.register_next_step_handler(msg, get_name)

def get_name(message):
    user_id = message.from_user.id

    # Проверяем, не зарегистрирован ли пользователь уже
    if is_user_registered(user_id):
        user_bot.send_message(user_id,
                              "❌ Вы уже зарегистрированы!\n\n"
                              "Используйте другие команды из меню.",
                              reply_markup=user_keyboard)
        if user_id in user_data:
            del user_data[user_id]
        return

    # Проверяем валидность введенного имени
    if is_invalid_name(message.text):
        user_bot.send_message(user_id,
                              "⚠️ *Некорректное имя!*\n\n"
                              "Имя должно:\n"
                              "• Быть длиннее 1 символа\n"
                              "• Содержать только буквы\n"
                              "• Не быть командой (не начинаться с /)\n"
                              "• Не содержать спецсимволы\n\n"
                              "Введите ваше имя еще раз:",
                              parse_mode='Markdown')
        msg = user_bot.send_message(user_id, 'Введите ваше имя:')
        user_bot.register_next_step_handler(msg, get_name)
        return

    user_data[user_id]['name'] = message.text.strip()
    user_data[user_id]['step'] = 'surname'

    user_bot.send_message(user_id,
                          f"✅ Имя принято: {message.text.strip()}\n\n"
                          "Теперь введите вашу фамилию:")
    user_bot.register_next_step_handler(message, get_surname)

def get_surname(message):
    user_id = message.from_user.id

    # Проверяем, не зарегистрирован ли пользователь уже
    if is_user_registered(user_id):
        user_bot.send_message(user_id,
                              "❌ Вы уже зарегистрированы!\n\n"
                              "Используйте другие команды из меню.",
                              reply_markup=user_keyboard)
        if user_id in user_data:
            del user_data[user_id]
        return

    # Проверяем валидность введенной фамилии
    if is_invalid_name(message.text):
        user_bot.send_message(user_id,
                              "⚠️ *Некорректная фамилия!*\n\n"
                              "Фамилия должна:\n"
                              "• Быть длиннее 1 символа\n"
                              "• Содержать только буквы\n"
                              "• Не быть командой (не начинаться с /)\n"
                              "• Не содержать спецсимволы\n\n"
                              "Введите вашу фамилию еще раз:",
                              parse_mode='Markdown')
        user_bot.send_message(user_id, "Введите вашу фамилию:")
        user_bot.register_next_step_handler(message, get_surname)
        return

    if user_id not in user_data or 'name' not in user_data[user_id]:
        user_bot.send_message(user_id,
                              "❌ Что-то пошло не так. Начните сначала: /start",
                              reply_markup=user_keyboard)
        return

    name = user_data[user_id]['name']
    surname = message.text.strip()

    try:
        execute_query(
            '''INSERT INTO users (telegram_id, name, surname) 
               VALUES (%s, %s, %s)
               ON CONFLICT (telegram_id) 
               DO UPDATE SET name = EXCLUDED.name, surname = EXCLUDED.surname''',
            (user_id, name, surname)
        )

        success_text = (
            "✅ *Регистрация завершена!*\n\n"
            f"👤 *Имя:* {name}\n"
            f"👥 *Фамилия:* {surname}\n\n"
            "🎯 *Теперь вы будете получать приглашения на мероприятия*\n\n"
            "📱 *Ваши команды:*\n"
            "/admin - Проверить админ права\n"
            "/id - Узнать свой ID"
        )

        user_bot.send_message(user_id, success_text,
                              parse_mode='Markdown',
                              reply_markup=user_keyboard)

        # ⭐ ВАЖНАЯ ИНФОРМАЦИЯ: Кто зарегистрировался
        print(f"✅ Зарегистрирован: {name} {surname} (ID: {user_id})")

    except Exception as e:
        print(f"❌ Ошибка SQL при сохранении пользователя {user_id}: {e}")
        user_bot.send_message(user_id,
                              f"❌ Ошибка сохранения: {str(e)[:100]}\n\nПопробуйте снова: /start",
                              reply_markup=user_keyboard)

    if user_id in user_data:
        del user_data[user_id]

@user_bot.callback_query_handler(func=lambda call: call.data.startswith('response_'))
def handle_inline_response(call):
    """Обрабатывает ответ пользователя через инлайн кнопки"""
    user_id = call.from_user.id
    callback_data = call.data

    # Разбираем callback_data: response_yes_event_123
    parts = callback_data.split('_')
    if len(parts) != 4:
        user_bot.answer_callback_query(call.id, "❌ Ошибка обработки ответа")
        return

    response_type = parts[1]  # yes или no
    event_id = int(parts[3])  # ID мероприятия

    # Получаем информацию о пользователе
    user_info = execute_query(
        'SELECT name, surname FROM users WHERE telegram_id = %s',
        (user_id,),
        fetchone=True
    )

    if not user_info:
        user_bot.answer_callback_query(call.id, "❌ Сначала зарегистрируйтесь через /start")
        user_bot.send_message(user_id, "❌ Сначала зарегистрируйтесь: /start", reply_markup=user_keyboard)
        return

    name, surname = user_info

    # Получаем информацию о мероприятии
    event_info = execute_query(
        'SELECT event_name, invitation_text, event_photo_id FROM events WHERE event_id = %s',
        (event_id,),
        fetchone=True
    )

    if not event_info:
        user_bot.answer_callback_query(call.id, "❌ Мероприятие не найдено")
        return

    event_name, invitation_text, event_photo_id = event_info

    # Получаем ID сообщения приглашения
    message_id = get_invitation_message_id(user_id, event_id)
    if not message_id:
        user_bot.answer_callback_query(call.id, "❌ Сообщение с приглашением не найдено")
        return

    # Проверяем не отвечал ли уже пользователь
    existing_response = check_user_response(user_id, event_id)

    if existing_response:
        # Обновляем сообщение с информацией
        response_text = "✅ Да" if existing_response[0] == 'yes' else "❌ Нет"
        updated_text = (
            f"🎫 *Приглашение на мероприятие*\n\n"
            f"Здравствуйте, *{name} {surname}*!\n\n"
            f"Вы приглашены на мероприятие:\n"
            f"*{event_name}* (№{event_id})\n\n"
            f"📝 *Описание:*\n"
            f"{invitation_text}\n\n"
            f"✅ *Вы уже ответили:* {response_text}\n\n"
            f"_Статус: {'✅ QR-код отправлен' if existing_response[1] else '⏳ Ожидание QR-кода'}_"
        )

        # Редактируем сообщение
        try:
            if event_photo_id:
                user_bot.edit_message_caption(
                    chat_id=user_id,
                    message_id=message_id,
                    caption=updated_text,
                    parse_mode='Markdown'
                )
            else:
                user_bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=updated_text,
                    parse_mode='Markdown'
                )
        except Exception as e:
            print(f"❌ Ошибка редактирования сообщения: {e}")
            user_bot.send_message(user_id, updated_text, parse_mode='Markdown')

        user_bot.answer_callback_query(call.id, f"Вы уже ответили: {response_text}")
        return

    try:
        # Сохраняем ответ пользователя
        save_user_response(user_id, event_id, response_type)

    except Exception as e:
        print(f"❌ Ошибка сохранения ответа: {e}")
        user_bot.answer_callback_query(call.id, "❌ Ошибка сохранения ответа")
        return

    # Обновляем сообщение
    if response_type == 'yes':
        # Создаем и отправляем QR-код
        try:
            qr_image, qr_data = create_qr_code(event_id, user_id)

            updated_text = (
                f"🎫 *Приглашение на мероприятие*\n\n"
                f"Здравствуйте, *{name} {surname}*!\n\n"
                f"Вы приглашены на мероприятие:\n"
                f"*{event_name}* (№{event_id})\n\n"
                f"📝 *Описание:*\n"
                f"{invitation_text}\n\n"
                f"✅ *Вы ответили:* Да, буду участвовать\n\n"
                f"_Статус: ✅ QR-код отправлен_"
            )

            # Редактируем сообщение с приглашением
            try:
                if event_photo_id:
                    user_bot.edit_message_caption(
                        chat_id=user_id,
                        message_id=message_id,
                        caption=updated_text,
                        parse_mode='Markdown'
                    )
                else:
                    user_bot.edit_message_text(
                        chat_id=user_id,
                        message_id=message_id,
                        text=updated_text,
                        parse_mode='Markdown'
                    )
            except Exception as edit_error:
                print(f"❌ Ошибка редактирования сообщения: {edit_error}")
                user_bot.send_message(user_id, updated_text, parse_mode='Markdown')

            # Отправляем QR-код отдельным сообщением
            qr_message = (
                f"🎉 *Отлично! Вы подтвердили участие!*\n\n"
                f"Мероприятие: *{event_name}*\n\n"
                f"📱 *Это ваш пригласительный QR-код:*\n"
                f"Покажите его на мероприятии и вас пропустят.\n\n"
                f"💡 *Совет:* Сохраните этот QR-код в галерее телефона."
            )

            user_bot.send_message(user_id, qr_message, parse_mode='Markdown')

            # Отправляем QR-код как фото
            qr_image.seek(0)
            user_bot.send_photo(user_id, qr_image,
                                caption=f"QR-код для мероприятия: {event_name}\nКод: {qr_data}")

            # Отмечаем что QR отправлен
            mark_qr_sent(user_id, event_id)

            # Создаем запись в таблице посещаемости со статусом 0 (не отсканирован)
            try:
                execute_query(
                    '''INSERT INTO attendance (user_id, event_name, attendance_status) 
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, event_name) 
                       DO NOTHING''',
                    (user_id, event_name, 0)
                )
            except Exception as attendance_error:
                print(f"❌ Ошибка создания записи о посещаемости: {attendance_error}")

            # ⭐ ВАЖНАЯ ИНФОРМАЦИЯ: Кто принял приглашение
            print(f"✅ Принял приглашение: {name} {surname} на {event_name}")

            user_bot.answer_callback_query(call.id, "✅ Спасибо за ответ! QR-код отправлен")

        except Exception as e:
            print(f"❌ Ошибка создания QR для {name} {surname}: {e}")
            user_bot.answer_callback_query(call.id, "❌ Ошибка при создании QR-кода")
            user_bot.send_message(user_id,
                                  "❌ Ошибка при создании QR-кода. Попробуйте позже.",
                                  reply_markup=user_keyboard)

    else:  # response_type == 'no'
        updated_text = (
            f"🎫 *Приглашение на мероприятие*\n\n"
            f"Здравствуйте, *{name} {surname}*!\n\n"
            f"Вы приглашены на мероприятие:\n"
            f"*{event_name}* (№{event_id})\n\n"
            f"📝 *Описание:*\n"
            f"{invitation_text}\n\n"
            f"❌ *Вы ответили:* Нет, не смогу\n\n"
            f"_Спасибо за ваш ответ!_"
        )

        # Редактируем сообщение
        try:
            if event_photo_id:
                user_bot.edit_message_caption(
                    chat_id=user_id,
                    message_id=message_id,
                    caption=updated_text,
                    parse_mode='Markdown'
                )
            else:
                user_bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=updated_text,
                    parse_mode='Markdown'
                )
        except Exception as e:
            print(f"❌ Ошибка редактирования сообщения: {e}")
            user_bot.send_message(user_id, updated_text, parse_mode='Markdown')

        decline_message = (
            f"📭 *Ваш ответ сохранен*\n\n"
            f"Вы отказались от участия в мероприятии:\n"
            f"*{event_name}*\n\n"
            f"Спасибо за ваш ответ!"
        )

        user_bot.send_message(user_id, decline_message,
                              parse_mode='Markdown',
                              reply_markup=user_keyboard)

        # ⭐ ВАЖНАЯ ИНФОРМАЦИЯ: Кто отказался
        print(f"❌ Отказался: {name} {surname} от {event_name}")

        user_bot.answer_callback_query(call.id, "❌ Ваш отказ сохранен")

@user_bot.message_handler(commands=['admin'])
def admin(message):
    # Проверяем, зарегистрирован ли пользователь
    if not is_user_registered(message.from_user.id):
        user_bot.send_message(message.chat.id,
                              "❌ Сначала зарегистрируйтесь через /start",
                              reply_markup=user_keyboard)
        return

    msg = user_bot.send_message(message.chat.id,
                                "🔑 Введите ID администратора:\n\n"
                                "_Для отмены нажмите кнопку ниже_",
                                reply_markup=admin_cancel_keyboard)
    user_bot.register_next_step_handler(msg, check_admin_status)

def check_admin_status(message):
    # Проверяем, если пользователь хочет отменить
    if message.text == "❌ Отмена":
        user_bot.send_message(message.chat.id,
                              "❌ Проверка админ прав отменена",
                              reply_markup=user_keyboard)
        return

    try:
        user_id = int(message.text.strip())
        if user_id in ADMIN_IDS:
            response = "✅ ДА! Вы администратор!"
        else:
            response = "❌ НЕТ! Вы не администратор!"
        user_bot.send_message(message.chat.id, response, reply_markup=user_keyboard)
    except ValueError:
        user_bot.send_message(message.chat.id,
                              "❌ Ошибка! Введите только цифры ID.\n\n"
                              "Попробуйте снова: /admin",
                              reply_markup=user_keyboard)

@user_bot.message_handler(commands=['id'])
def send_user_id(message):
    # Проверяем, зарегистрирован ли пользователь
    if not is_user_registered(message.from_user.id):
        user_bot.send_message(message.chat.id,
                              "❌ Сначала зарегистрируйтесь через /start",
                              reply_markup=user_keyboard)
        return

    user_bot.reply_to(message,
                      f"Ваш ID: `{message.from_user.id}`",
                      parse_mode='Markdown',
                      reply_markup=user_keyboard)

@user_bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    text = message.text
    user_id = message.from_user.id

    # Проверяем, находится ли пользователь в процессе регистрации
    if user_id in user_data:
        step = user_data[user_id].get('step')
        if step == 'name':
            get_name(message)
        elif step == 'surname':
            get_surname(message)
        return

    # Обработка обычных команд
    if text == "/start" or text == "📝 Регистрация (/start)":
        send_welcome(message)
    elif text == "/admin" or text == "👑 Админ (/admin)":
        admin(message)
    elif text == "/id" or text == "🆔 Мой ID (/id)":
        send_user_id(message)
    elif text.startswith('/'):
        user_bot.send_message(message.chat.id,
                              "❌ Неизвестная команда\n\n"
                              "Доступные команды:\n"
                              "/start - Регистрация\n"
                              "/admin - Проверить админ права\n"
                              "/id - Узнать свой ID",
                              reply_markup=user_keyboard)
    else:
        user_bot.send_message(message.chat.id,
                              "Для регистрации используйте команду /start",
                              reply_markup=user_keyboard)

# ========== АДМИН БОТ ==========
def is_cancel_command(text):
    """Проверяет, является ли сообщение командой отмены"""
    return text in ["❌ Отмена", "/cancel"]

@admin_bot.message_handler(commands=['edit_user'])
def edit_user_command(message):
    """Команда для редактирования данных пользователя"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

    admin_bot.send_message(message.chat.id,
                           "👤 *Редактирование данных пользователя*\n\n"
                           "Введите данные в формате:\n"
                           "`ID_пользователя Новое_Имя Новая_Фамилия`\n\n"
                           "Пример:\n"
                           "`123456789 Иван Петров`\n\n"
                           "Или нажмите ❌ Отмена для отмены",
                           parse_mode='Markdown',
                           reply_markup=cancel_keyboard)

    admin_bot.register_next_step_handler(message, process_user_edit)

def process_user_edit(message):
    """Обрабатывает редактирование пользователя"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Редактирование отменено",
                               reply_markup=admin_keyboard)
        return

    try:
        # Разбираем ввод: ID Имя Фамилия
        parts = message.text.strip().split()

        if len(parts) < 3:
            admin_bot.send_message(message.chat.id,
                                   "❌ Неверный формат!\n\n"
                                   "Введите в формате: `ID Имя Фамилия`\n"
                                   "Пример: `123456789 Иван Петров`\n\n"
                                   "Попробуйте снова: /edit_user",
                                   parse_mode='Markdown',
                                   reply_markup=admin_keyboard)
            return

        user_id = int(parts[0])
        name = parts[1]
        surname = ' '.join(parts[2:])  # Объединяем оставшиеся части как фамилию

        # Проверяем валидность имени и фамилии
        if is_invalid_name(name) or is_invalid_name(surname):
            admin_bot.send_message(message.chat.id,
                                   "⚠️ *Некорректное имя или фамилия!*\n\n"
                                   "Имя и фамилия должны:\n"
                                   "• Быть длиннее 1 символа\n"
                                   "• Содержать только буквы\n"
                                   "• Не быть командой (не начинаться с /)\n"
                                   "• Не содержать спецсимволы\n\n"
                                   "Попробуйте снова: /edit_user",
                                   parse_mode='Markdown',
                                   reply_markup=admin_keyboard)
            return

        # Проверяем, существует ли пользователь
        user_info = execute_query(
            'SELECT name, surname FROM users WHERE telegram_id = %s',
            (user_id,),
            fetchone=True
        )

        if not user_info:
            admin_bot.send_message(message.chat.id,
                                   f"❌ Пользователь с ID {user_id} не найден!\n\n"
                                   f"Пользователь должен быть сначала зарегистрирован через /start в пользовательском боте.\n\n"
                                   f"Попробуйте снова: /edit_user",
                                   reply_markup=admin_keyboard)
            return

        old_name, old_surname = user_info

        # Обновляем данные пользователя
        try:
            execute_query(
                'UPDATE users SET name = %s, surname = %s WHERE telegram_id = %s',
                (name, surname, user_id)
            )

            response = (
                f"✅ *Данные пользователя обновлены!*\n\n"
                f"👤 *ID пользователя:* {user_id}\n\n"
                f"📝 *Было:*\n"
                f"Имя: {old_name}\n"
                f"Фамилия: {old_surname}\n\n"
                f"📝 *Стало:*\n"
                f"Имя: {name}\n"
                f"Фамилия: {surname}\n\n"
                f"✅ Данные успешно перезаписаны!"
            )

            # ⭐ ВАЖНАЯ ИНФОРМАЦИЯ: Кто отредактирован
            print(f"✏️ Отредактирован: {old_name} {old_surname} → {name} {surname} (ID: {user_id})")

        except Exception as e:
            print(f"❌ Ошибка SQL при обновлении пользователя {user_id}: {e}")
            response = f"❌ Ошибка обновления в базе данных: {str(e)[:100]}"

        admin_bot.send_message(message.chat.id, response,
                               parse_mode='Markdown',
                               reply_markup=admin_keyboard)

    except ValueError:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка! ID должен быть числом.\n\n"
                               "Пример: `123456789 Иван Петров`\n\n"
                               "Попробуйте снова: /edit_user",
                               parse_mode='Markdown',
                               reply_markup=admin_keyboard)
    except Exception as e:
        print(f"❌ Ошибка редактирования пользователя: {e}")
        admin_bot.send_message(message.chat.id,
                               f"❌ Ошибка: {str(e)[:100]}\n\n"
                               f"Попробуйте снова: /edit_user",
                               reply_markup=admin_keyboard)

@admin_bot.message_handler(commands=['scan_qr'])
def scan_qr_command(message):
    """Команда для сканирования QR-кодов"""
    admin_bot.send_message(message.chat.id,
                           "📷 Сканирование QR-кодов\n\n"
                           "Отправьте фото QR-кода для проверки\n\n"
                           "Или /cancel для отмены",
                           reply_markup=cancel_keyboard)

    admin_bot.register_next_step_handler(message, process_qr_scan)

def process_qr_scan(message):
    """Обрабатывает фото с QR-кодом"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Сканирование отменено",
                               reply_markup=admin_keyboard)
        return

    if not message.photo:
        admin_bot.send_message(message.chat.id,
                               "❌ Пожалуйста, отправьте фото с QR-кодом\n\n"
                               "Попробуйте снова: /scan_qr",
                               reply_markup=admin_keyboard)
        return

    try:
        admin_bot.send_message(message.chat.id, "🔍 Сканирую QR-код...")

        # Скачиваем фото
        file_id = message.photo[-1].file_id
        file_info = admin_bot.get_file(file_id)
        downloaded_file = admin_bot.download_file(file_info.file_path)

        # Сохраняем временный файл
        temp_file = f"temp_qr_{message.message_id}.jpg"
        with open(temp_file, 'wb') as f:
            f.write(downloaded_file)

        # Сканируем QR-код
        qr_data = decode_qr_code_from_photo(temp_file)

        # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: Если не найден, пробуем улучшить изображение
        if not qr_data:
            qr_data = enhanced_qr_decode(temp_file)

        # Удаляем временные файлы
        if os.path.exists(temp_file):
            os.remove(temp_file)

        if qr_data:
            # Проверяем формат с разделителем 'U'
            if 'U' not in qr_data:
                admin_bot.send_message(message.chat.id,
                                       f"❌ Неверный формат QR-кода!\n\n"
                                       f"Получено: {qr_data}\n"
                                       f"Ожидался формат: номер мероприятияUid пользователя\n"
                                       f"Пример: 1U123456789\n\n"
                                       f"Проверьте правильность QR-кода.",
                                       reply_markup=admin_keyboard)
                return

            # Разделяем на номер мероприятия и ID пользователя
            try:
                event_id_str, user_id_str = qr_data.split('U')
                event_id = int(event_id_str)
                user_id = int(user_id_str)

                # Проверяем пользователя
                user = execute_query(
                    'SELECT name, surname FROM users WHERE telegram_id = %s',
                    (user_id,),
                    fetchone=True
                )

                if not user:
                    admin_bot.send_message(message.chat.id,
                                           f"❌ Пользователь не найден!\n\n"
                                           f"ID пользователя: {user_id}\n"
                                           f"Возможно, пользователь не зарегистрирован через /start\n"
                                           f"Или QR-код создан для другого пользователя.",
                                           reply_markup=admin_keyboard)
                    return

                name, surname = user

                # Проверяем мероприятие
                event = execute_query(
                    'SELECT event_name FROM events WHERE event_id = %s',
                    (event_id,),
                    fetchone=True
                )

                if not event:
                    admin_bot.send_message(message.chat.id,
                                           f"❌ Мероприятие не найдено!\n\n"
                                           f"Номер мероприятия: {event_id}",
                                           reply_markup=admin_keyboard)
                    return

                event_name = event[0]

                # Проверяем, есть ли уже запись о посещении
                attendance_record = execute_query(
                    '''SELECT attendance_status FROM attendance 
                       WHERE user_id = %s AND event_name = %s''',
                    (user_id, event_name),
                    fetchone=True
                )

                if attendance_record:
                    attendance_status = attendance_record[0]

                    if attendance_status == 1:
                        admin_bot.send_message(message.chat.id,
                                               f"⚠️ *Этот QR-код уже был отсканирован!*\n\n"
                                               f"🎫 Мероприятие: {event_name} (№{event_id})\n"
                                               f"👤 Участник: {name} {surname}\n"
                                               f"🆔 ID: {user_id}\n\n"
                                               f"❌ Этот участник уже был зарегистрирован на мероприятии.",
                                               reply_markup=admin_keyboard)
                        return

                # Отмечаем посещение (статус 1)
                attendance_result = mark_attendance(user_id, event_name)

                if attendance_result == "success":
                    response = (
                        f"✅ QR-код проверен и посещение отмечено!\n\n"
                        f"🎫 Мероприятие: {event_name} (№{event_id})\n"
                        f"👤 Участник: {name} {surname}\n"
                        f"🆔 ID: {user_id}\n\n"
                        f"✅ Доступ разрешен!"
                    )

                    # ⭐ ВАЖНАЯ ИНФОРМАЦИЯ: Кто отсканирован
                    print(f"📱 Отсканирован: {name} {surname} на {event_name}")

                    # Создаем запись в user_responses если её нет
                    existing_response = execute_query(
                        '''SELECT response FROM user_responses 
                           WHERE user_id = %s AND event_id = %s''',
                        (user_id, event_id),
                        fetchone=True
                    )

                    if not existing_response:
                        execute_query(
                            '''INSERT INTO user_responses (user_id, event_id, response, qr_sent) 
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (user_id, event_id) 
                               DO UPDATE SET response = EXCLUDED.response, qr_sent = EXCLUDED.qr_sent''',
                            (user_id, event_id, 'yes', True)
                        )

                elif attendance_result == "already_scanned":
                    response = (
                        f"⚠️ *Этот QR-код уже был отсканирован!*\n\n"
                        f"🎫 Мероприятие: {event_name} (№{event_id})\n"
                        f"👤 Участник: {name} {surname}\n"
                        f"🆔 ID: {user_id}\n\n"
                        f"❌ Этот участник уже был зарегистрирован на мероприятии."
                    )
                else:
                    response = (
                        f"❌ Ошибка отметки посещения!\n\n"
                        f"🎫 Мероприятие: {event_name} (№{event_id})\n"
                        f"👤 Участник: {name} {surname}\n"
                        f"🆔 ID: {user_id}"
                    )

                admin_bot.send_message(message.chat.id, response,
                                       reply_markup=admin_keyboard)

            except ValueError as e:
                print(f"❌ Ошибка преобразования чисел: {e}")
                admin_bot.send_message(message.chat.id,
                                       f"❌ Ошибка обработки QR-кода!\n\n"
                                       f"Некорректные данные в QR-коде: {qr_data}\n"
                                       f"Ожидался формат: числоUчисле\n"
                                       f"Пример: 1U123456789",
                                       reply_markup=admin_keyboard)
            except Exception as e:
                print(f"❌ Ошибка обработки QR-кода: {e}")
                admin_bot.send_message(message.chat.id,
                                       f"❌ Ошибка обработки QR-кода: {str(e)[:100]}",
                                       reply_markup=admin_keyboard)

        else:
            admin_bot.send_message(message.chat.id,
                                   "❌ QR-код не найден на фото!\n\n"
                                   "**Советы для лучшего сканирования:**\n"
                                   "1. 📸 Сфотографируйте QR-код при хорошем освещении\n"
                                   "2. 🔍 Убедитесь, что весь QR-код в кадре\n"
                                   "3. 📱 Держите камеру прямо напротив QR-кода\n"
                                   "4. 💡 Избегайте бликов и теней\n"
                                   "5. 🎯 QR-код должен занимать большую часть кадра\n\n"
                                   "🔄 *Попробуйте сделать фото еще раз:* /scan_qr",
                                   reply_markup=admin_keyboard)

    except Exception as e:
        print(f"❌ Ошибка обработки фото: {e}")
        admin_bot.send_message(message.chat.id,
                               f"❌ Ошибка обработки!\n\n"
                               f"Подробности: {str(e)[:100]}\n\n"
                               f"Попробуйте снова: /scan_qr",
                               reply_markup=admin_keyboard)

@admin_bot.message_handler(commands=['Sending_messages'])
def admin_sending(message):
    event_num = get_next_event_number()

    if not hasattr(admin_bot, 'user_data'):
        admin_bot.user_data = {}

    admin_bot.user_data[message.chat.id] = {
        'next_event_num': event_num,
        'step': 'waiting_for_name'
    }

    admin_bot.send_message(message.chat.id,
                           f"🎬 Создание мероприятия №{event_num}\n\n"
                           f"Введите название мероприятия:\n\n"
                           f"Или нажмите ❌ Отмена для отмены",
                           reply_markup=cancel_keyboard)
    admin_bot.register_next_step_handler(message, get_event_name)

def get_event_name(message):
    if is_cancel_command(message.text):
        admin_bot.send_message(message.chat.id,
                               "❌ Создание мероприятия отменено",
                               reply_markup=admin_keyboard)
        if hasattr(admin_bot, 'user_data') and message.chat.id in admin_bot.user_data:
            del admin_bot.user_data[message.chat.id]
        return

    if not hasattr(admin_bot, 'user_data'):
        admin_bot.user_data = {}

    if message.chat.id not in admin_bot.user_data:
        admin_bot.user_data[message.chat.id] = {}

    event_name = message.text
    event_num = admin_bot.user_data[message.chat.id].get('next_event_num', 1)

    admin_bot.user_data[message.chat.id]['event_num'] = event_num
    admin_bot.user_data[message.chat.id]['event_name'] = event_name
    admin_bot.user_data[message.chat.id]['step'] = 'waiting_for_photo'

    admin_bot.send_message(message.chat.id,
                           f"✅ Название сохранено!\n\n"
                           f"🎫 Номер: #{event_num}\n"
                           f"📝 Название: {event_name}\n\n"
                           f"📸 Теперь отправьте фотографию для мероприятия "
                           f"(или отправьте любое текстовое сообщение, чтобы пропустить):\n\n"
                           f"Или нажмите ❌ Отмена для отмены",
                           reply_markup=cancel_keyboard)
    admin_bot.register_next_step_handler(message, get_event_photo)

def get_event_photo(message):
    if is_cancel_command(message.text):
        admin_bot.send_message(message.chat.id,
                               "❌ Создание мероприятия отменено",
                               reply_markup=admin_keyboard)
        if hasattr(admin_bot, 'user_data') and message.chat.id in admin_bot.user_data:
            del admin_bot.user_data[message.chat.id]
        return

    if not hasattr(admin_bot, 'user_data') or message.chat.id not in admin_bot.user_data:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: данные сессии потеряны.\n\nПопробуйте снова: /Sending_messages",
                               reply_markup=admin_keyboard)
        return

    user_data = admin_bot.user_data[message.chat.id]
    event_num = user_data.get('event_num', 0)
    event_name = user_data.get('event_name', '')

    if event_num == 0 or not event_name:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: не найдены данные мероприятия.\n\nПопробуйте снова: /Sending_messages",
                               reply_markup=admin_keyboard)
        return

    event_photo_id = None

    if message.photo:
        try:
            event_photo_id = message.photo[-1].file_id
            user_data['event_photo_id'] = event_photo_id
            admin_bot.send_message(message.chat.id,
                                   f"✅ Фотография получена!\n\n"
                                   f"Теперь введите текст приглашения:")
        except Exception as e:
            print(f"❌ Ошибка получения file_id фото: {e}")
            admin_bot.send_message(message.chat.id,
                                   f"❌ Ошибка при получении фотографии. Попробуйте снова с текстом приглашения:")
            user_data['event_photo_id'] = None
    else:
        admin_bot.send_message(message.chat.id,
                               f"✅ Пропускаем добавление фотографии.\n\n"
                               f"Теперь введите текст приглашения:")
        user_data['event_photo_id'] = None

    user_data['step'] = 'waiting_for_invitation_text'
    admin_bot.register_next_step_handler(message, get_invitation_text)

def get_invitation_text(message):
    if is_cancel_command(message.text):
        admin_bot.send_message(message.chat.id,
                               "❌ Создание мероприятия отменено",
                               reply_markup=admin_keyboard)
        if hasattr(admin_bot, 'user_data') and message.chat.id in admin_bot.user_data:
            del admin_bot.user_data[message.chat.id]
        return

    if not hasattr(admin_bot, 'user_data') or message.chat.id not in admin_bot.user_data:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: данные сессии потеряны.\n\nПопробуйте снова: /Sending_messages",
                               reply_markup=admin_keyboard)
        return

    invitation_text = message.text
    user_data = admin_bot.user_data[message.chat.id]

    event_num = user_data.get('event_num', 0)
    event_name = user_data.get('event_name', '')
    event_photo_id = user_data.get('event_photo_id', None)

    if event_num == 0 or not event_name:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: не найдены данные мероприятия.\n\nПопробуйте снова: /Sending_messages",
                               reply_markup=admin_keyboard)
        return

    try:
        execute_query(
            'INSERT INTO events (event_id, event_name, invitation_text, event_photo_id) VALUES (%s, %s, %s, %s)',
            (event_num, event_name, invitation_text, event_photo_id)
        )

        print(f"🎫 Создано мероприятие: №{event_num} - {event_name}")

        preview_message = (
            f"✅ Мероприятие создано!\n\n"
            f"🎫 Номер: #{event_num}\n"
            f"📝 Название: {event_name}\n"
            f"📸 Фото: {'✅ Есть' if event_photo_id else '❌ Нет'}\n"
            f"📝 Текст: {invitation_text[:100]}...\n\n"
            f"Начинаю рассылку..."
        )

        admin_bot.send_message(message.chat.id, preview_message)

        start_broadcast(message.chat.id, event_num, event_name, invitation_text, event_photo_id)

    except Exception as e:
        print(f"❌ Ошибка при сохранении мероприятия в базу: {e}")
        admin_bot.send_message(message.chat.id,
                               f"❌ Ошибка при создании мероприятия: {str(e)[:200]}\n\n"
                               f"Попробуйте снова: /Sending_messages",
                               reply_markup=admin_keyboard)

def start_broadcast(chat_id, event_num, event_name, invitation_text, event_photo_id=None):
    """Начинает рассылку приглашений"""
    users = execute_query('SELECT telegram_id, name, surname FROM users', fetchall=True)

    sent = 0
    failed = 0

    print(f"📤 Начинаю рассылку приглашений на {event_name} ({len(users) if users else 0} пользователей)")

    admin_bot.send_message(chat_id,
                           f"🚀 Начинаю рассылку...\n\n"
                           f"👥 Пользователей: {len(users) if users else 0}\n"
                           f"🎫 Мероприятие: {event_name}\n"
                           f"📸 С фото: {'✅ Да' if event_photo_id else '❌ Нет'}")

    if users:
        for user in users:
            user_id, name, surname = user
            try:
                success = send_invitation_to_user(
                    user_id, name, surname,
                    event_num, event_name,
                    invitation_text,
                    event_photo_id
                )

                if success:
                    sent += 1
                else:
                    failed += 1
                    print(f"❌ Ошибка отправки приглашения {name} {surname}")

                time.sleep(0.3)

            except Exception as e:
                failed += 1
                print(f"❌ Критическая ошибка отправки пользователю {user_id}: {e}")

    stats_message = (
        f"✅ Рассылка завершена!\n\n"
        f"🎫 Мероприятие: №{event_num} - {event_name}\n"
        f"📸 С фото: {'✅ Да' if event_photo_id else '❌ Нет'}\n"
        f"👥 Всего пользователей: {len(users) if users else 0}\n"
        f"✅ Успешно отправлено: {sent}\n"
        f"❌ Не удалось отправить: {failed}\n\n"
        f"📊 QR-коды будут отправлены пользователям, которые ответят 'Да'"
    )

    admin_bot.send_message(chat_id, stats_message,
                           reply_markup=admin_keyboard)

    print(f"✅ Рассылка завершена: {sent} отправлено, {failed} ошибок")

    if hasattr(admin_bot, 'user_data') and chat_id in admin_bot.user_data:
        del admin_bot.user_data[chat_id]

@admin_bot.message_handler(commands=['announce'])
def announce_command(message):
    """Команда рассылки сообщений всем пользователям"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

    admin_bot.send_message(message.chat.id,
                           "📢 *Рассылка сообщения всем пользователям*\n\n"
                           "Напишите сообщение, которое будет отправлено всем зарегистрированным пользователям:\n\n"
                           "Или нажмите ❌ Отмена для отмены",
                           parse_mode='Markdown',
                           reply_markup=cancel_keyboard)

    admin_bot.register_next_step_handler(message, process_announcement_message)

def process_announcement_message(message):
    """Обрабатывает сообщение для рассылки"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Рассылка отменена",
                               reply_markup=admin_keyboard)
        return

    if not message.text or message.text.startswith('/'):
        admin_bot.send_message(message.chat.id,
                               "❌ Пожалуйста, введите текст сообщения\n\n"
                               "Попробуйте снова: /announce",
                               reply_markup=admin_keyboard)
        return

    message_text = message.text

    # Сразу запускаем рассылку (без подтверждения)
    admin_bot.send_message(message.chat.id,
                           f"⏳ Начинаю рассылку...",
                           reply_markup=admin_keyboard)

    broadcast_message_to_all(message.chat.id, message_text)

@admin_bot.message_handler(commands=['cancel'])
def cancel_command(message):
    if hasattr(admin_bot, 'user_data') and message.chat.id in admin_bot.user_data:
        admin_bot.send_message(message.chat.id,
                               "❌ Текущая операция отменена",
                               reply_markup=admin_keyboard)
        del admin_bot.user_data[message.chat.id]
    else:
        admin_bot.send_message(message.chat.id,
                               "❌ Нет активной операции для отмены",
                               reply_markup=admin_keyboard)

@admin_bot.message_handler(commands=['start'])
def admin_start(message):
    admin_bot.send_message(message.chat.id,
                           "👑 Админ-панель\n\n"
                           "Доступные команды:\n"
                           "/Sending_messages - Рассылка приглашений\n"
                           "/scan_qr - Сканировать QR-коды\n"
                           "/announce - Рассылка сообщений\n"
                           "/edit_user - Редактировать данные пользователя\n"
                           "/cancel - Отмена текущей операции",
                           reply_markup=admin_keyboard)

@admin_bot.message_handler(func=lambda message: True)
def handle_admin_messages(message):
    if message.text.startswith('/'):
        admin_bot.send_message(message.chat.id,
                               "❌ Неизвестная команда\n\n"
                               "Доступные команды:\n"
                               "/Sending_messages - Рассылка приглашений\n"
                               "/scan_qr - Сканировать QR-коды\n"
                               "/announce - Рассылка сообщений\n"
                               "/edit_user - Редактировать данные пользователя\n"
                               "/cancel - Отмена операции",
                               reply_markup=admin_keyboard)
    else:
        admin_bot.send_message(message.chat.id,
                               "Используйте команды из меню",
                               reply_markup=admin_keyboard)

# ========== ЗАПУСК БОТОВ ==========
def run_bot(bot, bot_name):
    """Запускает бота с перезапуском при ошибках"""
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            print(f"❌ Ошибка в {bot_name}: {e}")
            print(f"🔄 Перезапуск {bot_name} через 5 секунд...")
            time.sleep(5)

# ========== ФУНКЦИЯ ДЛЯ КОРРЕКТНОГО ЗАВЕРШЕНИЯ ==========
def cleanup():
    """Закрываем соединения при выходе"""
    print("\n" + "=" * 50)
    print("🔴 ЗАВЕРШЕНИЕ РАБОТЫ БОТА")
    print("=" * 50)

    try:
        if connection_pool:
            connection_pool.closeall()
            print("✅ Закрыто соединение с PostgreSQL")
    except:
        pass

    print("✅ Все соединения закрыты")
    print("=" * 50)

# Регистрируем функцию очистки
atexit.register(cleanup)

# ========== ОСНОВНОЙ ЗАПУСК ==========
if __name__ == '__main__':
    print("=" * 50)
    print("🤖 СИСТЕМА ПРИГЛАШЕНИЙ НА МЕРОПРИЯТИЯ")
    print("=" * 50)
    print(f"👑 Администраторы: {ADMIN_IDS}")
    print(f"🤖 Админ-бот: {'✅ Загружен' if ADMIN_BOT_TOKEN else '❌ Ошибка'}")
    print(f"👤 Пользовательский бот: {'✅ Загружен' if USER_BOT_TOKEN else '❌ Ошибка'}")
    print(f"🗄️ PostgreSQL: {'✅ Подключен' if DATABASE_URL else '❌ Ошибка'}")
    print("=" * 50)

    # Проверяем токены перед запуском
    if not ADMIN_BOT_TOKEN or not USER_BOT_TOKEN:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не все токены загружены!")
        print("   Проверьте файл .env в папке проекта")
        print("   Убедитесь что там есть ADMIN_BOT_TOKEN и USER_BOT_TOKEN")
        input("   Нажмите Enter для выхода...")
        exit(1)

    if not DATABASE_URL:
        print("⚠️ ВНИМАНИЕ: DATABASE_URL не найден!")
        print("   Для локальной разработки можно использовать SQLite")
        print("   Для продакшена добавьте PostgreSQL базу данных")

    try:
        print("🚀 Запуск ботов...")

        # Создаем потоки с демон-режимом (автоматически завершатся при выходе)
        admin_thread = threading.Thread(target=run_bot, args=(admin_bot, "ADMIN БОТ"))
        user_thread = threading.Thread(target=run_bot, args=(user_bot, "USER БОТ"))

        # Устанавливаем как демоны (завершатся при выходе главного потока)
        admin_thread.daemon = True
        user_thread.daemon = True

        admin_thread.start()
        user_thread.start()

        print("✅ Боты запущены в фоновом режиме")
        print("-" * 50)
        print("🟢 Сервер работает...")
        print("📝 Для остановки нажмите Ctrl+C в этом окне")
        print("-" * 50)

        # Бесконечный цикл, чтобы программа не завершалась
        # Обрабатываем Ctrl+C для красивого выхода
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n⚠️ Получен сигнал завершения (Ctrl+C)")
            print("⏳ Завершаю работу...")

    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ: {e}")
        import traceback
        traceback.print_exc()
        print("\n⚠️ Подробности ошибки выше")
        input("Нажмите Enter для выхода...")

