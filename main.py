import os
import telebot
from telebot import types
import sqlite3
import threading
import time
import qrcode
from io import BytesIO
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
import concurrent.futures
from config import ADMIN_BOT_TOKEN, USER_BOT_TOKEN, SCANNER_BOT_TOKEN, ADMIN_IDS

# ========== СОЗДАНИЕ ВСЕХ БОТОВ ==========
print("=" * 50)
print("🤖 ЗАГРУЗКА СИСТЕМЫ ПРИГЛАШЕНИЙ")
print("=" * 50)

# Создаем боты
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
user_bot = telebot.TeleBot(USER_BOT_TOKEN)
scanner_bot = telebot.TeleBot(SCANNER_BOT_TOKEN)  # Новый бот для сканирования

print(f"✅ Создано ботов:")
print(f"   📱 Админ-бот: {ADMIN_BOT_TOKEN[:10]}...")
print(f"   👥 Пользовательский бот: {USER_BOT_TOKEN[:10]}...")
print(f"   🔍 QR-Сканер: {SCANNER_BOT_TOKEN[:10]}...")
print("=" * 50)

# ========== КЕШИРОВАНИЕ ФОТО ==========
photo_cache = {}


def get_cached_photo(event_photo_id):
    """Кеширует фото мероприятия для повторного использования"""
    if event_photo_id is None:
        return None

    if event_photo_id in photo_cache:
        return photo_cache[event_photo_id]

    try:
        file_info = admin_bot.get_file(event_photo_id)
        downloaded_file = admin_bot.download_file(file_info.file_path)
        photo_cache[event_photo_id] = downloaded_file
        return downloaded_file
    except Exception as e:
        print(f"❌ Ошибка получения фото: {e}")
        return None


# ========== СОЗДАНИЕ НОВЫХ БАЗ ДАННЫХ ==========
# 1. База данных для пользователей
users_conn = sqlite3.connect('users.db', check_same_thread=False)
users_cursor = users_conn.cursor()

users_cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    surname TEXT NOT NULL
)
''')
users_conn.commit()

# 2. База данных для мероприятий
events_conn = sqlite3.connect('events.db', check_same_thread=False)
events_cursor = events_conn.cursor()

events_cursor.execute('''
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    event_name TEXT NOT NULL,
    event_photo_id TEXT,
    invitation_text TEXT
)
''')
events_conn.commit()

# 3. База данных для ответов пользователей на приглашения
responses_conn = sqlite3.connect('responses.db', check_same_thread=False)
responses_cursor = responses_conn.cursor()

responses_cursor.execute('''
CREATE TABLE IF NOT EXISTS user_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    response TEXT NOT NULL,
    qr_sent BOOLEAN DEFAULT 0,
    UNIQUE(user_id, event_id)
)
''')

# 4. Таблица для хранения сообщений приглашений
responses_cursor.execute('''
CREATE TABLE IF NOT EXISTS invitation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    UNIQUE(user_id, event_id)
)
''')

# 5. БАЗА ДАННЫХ ДЛЯ ПОСЕЩАЕМОСТИ (УПРОЩЕННАЯ)
attendance_conn = sqlite3.connect('attendance.db', check_same_thread=False)
attendance_cursor = attendance_conn.cursor()

attendance_cursor.execute('''
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    attendance_status INTEGER DEFAULT 0,  -- 0 = не отсканирован, 1 = отсканирован
    UNIQUE(user_id, event_name)
)
''')

# Создаем индексы
responses_cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_event ON user_responses (user_id, event_id)')
responses_cursor.execute('CREATE INDEX IF NOT EXISTS idx_msg_user_event ON invitation_messages (user_id, event_id)')
attendance_cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_user_event ON attendance (user_id, event_name)')

responses_conn.commit()
attendance_conn.commit()

print("✅ Все базы данных созданы/проверены")
print("=" * 50)

# ========== КЛАВИАТУРЫ ==========

# Обычная пользовательская клавиатура (используется после регистрации)
user_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
user_keyboard.add("📝 Регистрация (/start)", "🆔 Мой ID (/id)")

# Клавиатура для отмены в админ боте
cancel_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
cancel_keyboard.add("❌ Отмена")

# ОСНОВНАЯ КЛАВИАТУРА АДМИНА (РУССКИЙ) - ОБНОВЛЕННАЯ
admin_keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
admin_keyboard.add("📨 Рассылка приглашений", "🔍 Сканировать QR")
admin_keyboard.add("📢 Объявление", "👤 Редактировать пользователя")
admin_keyboard.add("📊 Статистика приглашений", "👥 Статистика посетивших")
admin_keyboard.add("❌ Отмена операции")


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
    events_cursor.execute('SELECT MAX(event_id) FROM events')
    result = events_cursor.fetchone()[0]
    if result is None:
        return 1
    return result + 1


def check_user_response(user_id, event_id):
    """Проверяет ответ пользователя на приглашение"""
    responses_cursor.execute(
        'SELECT response, qr_sent FROM user_responses WHERE user_id = ? AND event_id = ?',
        (user_id, event_id)
    )
    return responses_cursor.fetchone()


def save_user_response(user_id, event_id, response):
    """Сохраняет ответ пользователя"""
    try:
        responses_cursor.execute(
            'INSERT OR REPLACE INTO user_responses (user_id, event_id, response, qr_sent) VALUES (?, ?, ?, 0)',
            (user_id, event_id, response)
        )
        responses_conn.commit()
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения ответа: {e}")
        return False


def mark_qr_sent(user_id, event_id):
    """Отмечает что QR-код отправлен"""
    try:
        responses_cursor.execute(
            'UPDATE user_responses SET qr_sent = 1 WHERE user_id = ? AND event_id = ?',
            (user_id, event_id)
        )
        responses_conn.commit()
        return True
    except Exception as e:
        print(f"❌ Ошибка обновления статуса QR: {e}")
        return False


def save_invitation_message(user_id, event_id, message_id):
    """Сохраняет ID сообщения с приглашением"""
    try:
        responses_cursor.execute(
            'INSERT OR REPLACE INTO invitation_messages (user_id, event_id, message_id) VALUES (?, ?, ?)',
            (user_id, event_id, message_id)
        )
        responses_conn.commit()
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения ID сообщения: {e}")
        return False


def get_invitation_message_id(user_id, event_id):
    """Получает ID сообщения с приглашением"""
    responses_cursor.execute(
        'SELECT message_id FROM invitation_messages WHERE user_id = ? AND event_id = ?',
        (user_id, event_id)
    )
    result = responses_cursor.fetchone()
    return result[0] if result else None


def mark_attendance(user_id, event_name):
    """Отмечает посещение пользователя"""
    try:
        # Проверяем, не отмечен ли уже пользователь
        attendance_cursor.execute(
            'SELECT attendance_status FROM attendance WHERE user_id = ? AND event_name = ?',
            (user_id, event_name)
        )
        existing = attendance_cursor.fetchone()

        if existing and existing[0] == 1:
            return "already_scanned"

        # Добавляем или обновляем запись
        attendance_cursor.execute(
            'INSERT OR REPLACE INTO attendance (user_id, event_name, attendance_status) '
            'VALUES (?, ?, ?)',
            (user_id, event_name, 1)
        )
        attendance_conn.commit()
        return "success"
    except Exception as e:
        print(f"❌ Ошибка отметки посещения: {e}")
        return "error"


def get_user_info(user_id):
    """Получает информацию о пользователе"""
    users_cursor.execute('SELECT name, surname FROM users WHERE telegram_id = ?', (user_id,))
    return users_cursor.fetchone()


def get_event_info(event_id):
    """Получает информацию о мероприятии"""
    events_cursor.execute('SELECT event_name FROM events WHERE event_id = ?', (event_id,))
    return events_cursor.fetchone()


# ========== ФУНКЦИИ ДЛЯ СКАНИРОВАНИЯ QR-КОДОВ ==========
def decode_qr_code_from_photo(file_path):
    """УЛУЧШЕННАЯ функция сканирования QR-кодов"""
    try:
        pil_img = Image.open(file_path)

        width, height = pil_img.size
        if width < 300 or height < 300:
            new_width = max(600, width * 3)
            new_height = max(600, height * 3)
            pil_img = pil_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        original_img = pil_img.copy()
        img = cv2.cvtColor(np.array(original_img), cv2.COLOR_RGB2BGR)
        qr_detector = cv2.QRCodeDetector()

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

        if found_data:
            data_counts = {}
            for _, data in found_data:
                data_counts[data] = data_counts.get(data, 0) + 1

            most_common_data = max(data_counts.items(), key=lambda x: x[1])
            return most_common_data[0]

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
        pil_img = Image.open(file_path)

        methods = []

        img1 = pil_img.copy()
        enhancer = ImageEnhance.Contrast(img1)
        img1 = enhancer.enhance(2.0)
        methods.append(("Высокий контраст", img1))

        img2 = pil_img.copy()
        enhancer = ImageEnhance.Sharpness(img2)
        img2 = enhancer.enhance(3.0)
        methods.append(("Высокая резкость", img2))

        img3 = pil_img.copy()
        img3 = ImageOps.grayscale(img3)
        enhancer = ImageEnhance.Contrast(img3)
        img3 = enhancer.enhance(3.0)
        methods.append(("Черно-белый контраст", img3))

        img4 = pil_img.copy()
        if img4.mode == 'RGB':
            img4 = ImageOps.invert(img4)
        methods.append(("Инверсия цветов", img4))

        img5 = pil_img.copy()
        width, height = img5.size
        img5 = img5.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
        methods.append(("Увеличенный размер", img5))

        img6 = pil_img.copy()
        img6 = ImageOps.autocontrast(img6, cutoff=2)
        methods.append(("Автоконтраст", img6))

        qr_detector = cv2.QRCodeDetector()

        for method_name, processed_img in methods:
            try:
                opencv_img = cv2.cvtColor(np.array(processed_img), cv2.COLOR_RGB2BGR)

                data, bbox, _ = qr_detector.detectAndDecode(opencv_img)
                if data and len(data) > 0:
                    return data

            except:
                continue

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


def process_qr_photo(bot, message, bot_name="БОТ"):
    """Обрабатывает фото с QR-кодом (универсальная функция для всех ботов)"""
    try:
        bot.send_message(message.chat.id, "🔍 Сканирую QR-код...")

        # Скачиваем фото
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        # Сохраняем временный файл
        temp_file = f"temp_qr_{message.message_id}.jpg"
        with open(temp_file, 'wb') as f:
            f.write(downloaded_file)

        # Сканируем QR-код
        qr_data = decode_qr_code_from_photo(temp_file)

        # Дополнительная проверка если не найден
        if not qr_data:
            qr_data = enhanced_qr_decode(temp_file)

        # Удаляем временные файлы
        if os.path.exists(temp_file):
            os.remove(temp_file)

        if qr_data:
            # Проверяем формат с разделителем 'U'
            if 'U' not in qr_data:
                bot.send_message(message.chat.id,
                                 f"❌ *Неверный формат QR-кода!*\n\n"
                                 f"Получено: `{qr_data}`\n\n"
                                 f"Ожидался формат: номер мероприятияUid пользователя\n"
                                 f"Пример: `1U123456789`\n\n"
                                 f"Проверьте правильность QR-кода.",
                                 parse_mode='Markdown')
                return

            # Разделяем на номер мероприятия и ID пользователя
            try:
                event_id_str, user_id_str = qr_data.split('U')
                event_id = int(event_id_str)
                user_id = int(user_id_str)

                # Проверяем пользователя
                user_info = get_user_info(user_id)

                if not user_info:
                    bot.send_message(message.chat.id,
                                     f"❌ *Пользователь не найден!*\n\n"
                                     f"ID пользователя: `{user_id}`\n\n"
                                     f"Возможно, пользователь не зарегистрирован в системе.",
                                     parse_mode='Markdown')
                    return

                name, surname = user_info

                # Проверяем мероприятие
                event_info = get_event_info(event_id)

                if not event_info:
                    bot.send_message(message.chat.id,
                                     f"❌ *Мероприятие не найдено!*\n\n"
                                     f"ID мероприятия: `{event_id}`\n\n"
                                     f"Мероприятие с таким номером не существует.",
                                     parse_mode='Markdown')
                    return

                event_name = event_info[0]

                # Проверяем, есть ли уже запись о посещении
                attendance_cursor.execute('''
                    SELECT attendance_status FROM attendance 
                    WHERE user_id = ? AND event_name = ?
                ''', (user_id, event_name))

                attendance_record = attendance_cursor.fetchone()

                if attendance_record and attendance_record[0] == 1:
                    bot.send_message(message.chat.id,
                                     f"⚠️ *Этот QR-код уже был отсканирован!*\n\n"
                                     f"🎫 *Мероприятие:* {event_name}\n"
                                     f"👤 *Участник:* {name} {surname}\n"
                                     f"🆔 *ID:* {user_id}\n\n"
                                     f"❌ Этот участник уже был зарегистрирован.",
                                     parse_mode='Markdown')
                    return

                # Отмечаем посещение (статус 1)
                attendance_result = mark_attendance(user_id, event_name)

                if attendance_result == "success":
                    response = (
                        f"✅ *QR-код успешно отсканирован!*\n\n"
                        f"🎫 *Мероприятие:* {event_name} (№{event_id})\n"
                        f"👤 *Участник:* {name} {surname}\n"
                        f"🆔 *ID:* {user_id}\n\n"
                        f"✅ *Посещение отмечено!*"
                    )

                    # Логируем сканирование
                    print(f"📱 [{bot_name}] Отсканирован: {name} {surname} на {event_name}")

                    # Создаем запись в user_responses если её нет
                    responses_cursor.execute('''
                        SELECT response FROM user_responses 
                        WHERE user_id = ? AND event_id = ?
                    ''', (user_id, event_id))

                    if not responses_cursor.fetchone():
                        responses_cursor.execute(
                            'INSERT OR REPLACE INTO user_responses (user_id, event_id, response, qr_sent) VALUES (?, ?, ?, 1)',
                            (user_id, event_id, 'yes', 1)
                        )
                        responses_conn.commit()

                elif attendance_result == "already_scanned":
                    response = (
                        f"⚠️ *Этот QR-код уже был отсканирован!*\n\n"
                        f"🎫 *Мероприятие:* {event_name} (№{event_id})\n"
                        f"👤 *Участник:* {name} {surname}\n"
                        f"🆔 *ID:* {user_id}\n\n"
                        f"❌ Этот участник уже был зарегистрирован."
                    )
                else:
                    response = (
                        f"❌ *Ошибка отметки посещения!*\n\n"
                        f"🎫 Мероприятие: {event_name} (№{event_id})\n"
                        f"👤 Участник: {name} {surname}\n"
                        f"🆔 ID: {user_id}"
                    )

                bot.send_message(message.chat.id, response, parse_mode='Markdown')

            except ValueError:
                bot.send_message(message.chat.id,
                                 f"❌ *Ошибка обработки QR-кода!*\n\n"
                                 f"Получено: `{qr_data}`\n\n"
                                 f"Некорректные данные в QR-коде.\n"
                                 f"Ожидался формат: `числоUчисло`\n"
                                 f"Пример: `1U123456789`",
                                 parse_mode='Markdown')
            except Exception as e:
                print(f"❌ [{bot_name}] Ошибка обработки QR: {e}")
                bot.send_message(message.chat.id,
                                 f"❌ *Ошибка обработки!*\n\n"
                                 f"Подробности: {str(e)[:100]}\n\n"
                                 f"Попробуйте снова.",
                                 parse_mode='Markdown')

        else:
            bot.send_message(message.chat.id,
                             "❌ *QR-код не найден на фото!*\n\n"
                             "**Советы для лучшего сканирования:**\n"
                             "1. 📸 Сфотографируйте QR-код при хорошем освещении\n"
                             "2. 🔍 Убедитесь, что весь QR-код в кадре\n"
                             "3. 📱 Держите камеру прямо напротив QR-кода\n"
                             "4. 💡 Избегайте бликов и теней\n"
                             "5. 🎯 QR-код должен занимать большую часть кадра",
                             parse_mode='Markdown')

    except Exception as e:
        print(f"❌ [{bot_name}] Критическая ошибка: {e}")
        bot.send_message(
            message.chat.id,
            "❌ *Произошла ошибка при обработке фото!*\n\n"
            "Попробуйте отправить фото еще раз.",
            parse_mode='Markdown'
        )


# ========== ОПТИМИЗИРОВАННЫЕ ФУНКЦИИ РАССЫЛКИ ==========
def send_invitation_to_user_optimized(args):
    """Оптимизированная функция отправки приглашения (для многопоточности)"""
    user_id, name, surname, event_id, event_name, invitation_text, event_photo_data = args

    try:
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

        keyboard = create_inline_keyboard(event_id)

        if event_photo_data:
            try:
                photo_stream = BytesIO(event_photo_data)
                photo_stream.seek(0)

                sent_message = user_bot.send_photo(
                    user_id,
                    photo_stream,
                    caption=invitation,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
                photo_stream.close()
            except Exception as photo_error:
                print(f"❌ Ошибка отправки фото пользователю {user_id}: {photo_error}")
                sent_message = user_bot.send_message(
                    user_id,
                    invitation,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
        else:
            sent_message = user_bot.send_message(
                user_id,
                invitation,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        save_invitation_message(user_id, event_id, sent_message.message_id)
        return True

    except Exception as e:
        print(f"❌ Ошибка отправки приглашения пользователю {user_id}: {e}")
        return False


def start_broadcast(chat_id, event_num, event_name, invitation_text, event_photo_id=None):
    """Оптимизированная функция рассылки с многопоточностью"""
    # Загружаем фото один раз
    event_photo_data = get_cached_photo(event_photo_id)

    users_cursor.execute('SELECT telegram_id, name, surname FROM users')
    users = users_cursor.fetchall()

    print(f"📤 Начинаю рассылку приглашений на {event_name} ({len(users)} пользователей)")

    admin_bot.send_message(chat_id,
                           f"🚀 Начинаю рассылку...\n\n"
                           f"👥 Пользователей: {len(users)}\n"
                           f"🎫 Мероприятие: {event_name}\n"
                           f"📸 С фото: {'✅ Да' if event_photo_data else '❌ Нет'}")

    max_workers = 10

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        tasks = []
        for user in users:
            user_id, name, surname = user
            tasks.append((user_id, name, surname, event_num, event_name,
                          invitation_text, event_photo_data))

        future_to_user = {executor.submit(send_invitation_to_user_optimized, task): task
                          for task in tasks}

        sent = 0
        failed = 0

        for future in concurrent.futures.as_completed(future_to_user):
            try:
                result = future.result()
                if result:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"❌ Ошибка отправки: {e}")

    stats_message = (
        f"✅ Рассылка завершена!\n\n"
        f"🎫 Мероприятие: №{event_num} - {event_name}\n"
        f"📸 С фото: {'✅ Да' if event_photo_data else '❌ Нет'}\n"
        f"👥 Всего пользователей: {len(users)}\n"
        f"✅ Успешно отправлено: {sent}\n"
        f"❌ Не удалось отправить: {failed}\n\n"
        f"📊 QR-коды будут отправлены пользователям, которые ответят 'Да'"
    )

    admin_bot.send_message(chat_id, stats_message, reply_markup=admin_keyboard)
    print(f"✅ Рассылка завершена: {sent} отправлено, {failed} ошибок")

    if hasattr(admin_bot, 'user_data') and chat_id in admin_bot.user_data:
        del admin_bot.user_data[chat_id]


def send_broadcast_message(user_id, message):
    """Отправляет одно сообщение пользователю"""
    try:
        user_bot.send_message(user_id, message, parse_mode='Markdown')
        return True
    except:
        return False


def broadcast_message_to_all(chat_id, message_text):
    """Оптимизированная рассылка сообщений"""
    users_cursor.execute('SELECT telegram_id, name, surname FROM users')
    users = users_cursor.fetchall()

    broadcast_message = (
        f"📢 *Оповещение от администратора*\n\n"
        f"{message_text}"
    )

    max_workers = 15

    admin_bot.send_message(chat_id,
                           f"📤 Начинаю рассылку сообщения...\n\n"
                           f"👥 Пользователей: {len(users)}\n"
                           f"📝 Сообщение: {message_text[:50]}...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for user in users:
            user_id, name, surname = user
            futures.append(executor.submit(send_broadcast_message,
                                           user_id, broadcast_message))

        sent = sum(1 for f in futures if f.result())
        failed = len(users) - sent

    stats_message = (
        f"✅ Рассылка завершена!\n\n"
        f"👥 Всего пользователей: {len(users)}\n"
        f"✅ Успешно отправлено: {sent}\n"
        f"❌ Не удалось отправить: {failed}"
    )

    admin_bot.send_message(chat_id, stats_message, reply_markup=admin_keyboard)


# ========== ФУНКЦИИ ДЛЯ СТАТИСТИКИ ==========
def get_event_by_name(event_name):
    """Находит мероприятие по точному названию"""
    events_cursor.execute(
        'SELECT event_id, event_name FROM events WHERE event_name = ?',
        (event_name,)
    )
    return events_cursor.fetchone()


def get_invitation_stats(event_id):
    """Получает статистику по приглашениям для мероприятия"""
    users_cursor.execute('SELECT COUNT(*) FROM users')
    total_users = users_cursor.fetchone()[0]

    responses_cursor.execute(
        'SELECT COUNT(DISTINCT user_id) FROM invitation_messages WHERE event_id = ?',
        (event_id,)
    )
    received_invitations = responses_cursor.fetchone()[0] or 0

    failed_send = total_users - received_invitations

    responses_cursor.execute(
        'SELECT COUNT(*) FROM user_responses WHERE event_id = ? AND response = ?',
        (event_id, 'yes')
    )
    agreed_count = responses_cursor.fetchone()[0] or 0

    not_agreed_count = received_invitations - agreed_count

    if received_invitations > 0:
        agreed_percent = (agreed_count / received_invitations) * 100
    else:
        agreed_percent = 0

    return {
        'total_users': total_users,
        'received_invitations': received_invitations,
        'failed_send': failed_send,
        'agreed_count': agreed_count,
        'not_agreed_count': not_agreed_count,
        'agreed_percent': round(agreed_percent, 1)
    }


def format_stats_message(event_name, stats):
    """Форматирует сообщение со статистикой"""
    return (
        f"📊 *Статистика по мероприятию:* {event_name}\n\n"
        f"👥 *Всего пользователей в системе:* {stats['total_users']}\n"
        f"📨 *Получили приглашение:* {stats['received_invitations']}\n"
        f"✅ *Согласились прийти:* {stats['agreed_count']}\n"
        f"❌ *Отказались или еще не ответили:* {stats['not_agreed_count']}\n"
        f"⚠️ *Ошибок отправки:* {stats['failed_send']}\n\n"
        f"📈 *Процент согласий:* {stats['agreed_percent']}%\n\n"
        f"📋 *Сводка:*\n"
        f"• Всего приглашено: {stats['received_invitations']}/{stats['total_users']}\n"
        f"• Согласились: {stats['agreed_count']}/{stats['received_invitations']}"
    )


def get_attendance_stats(event_id, event_name):
    """Получает статистику посещаемости для мероприятия"""
    try:
        attendance_cursor.execute('''
            SELECT COUNT(*) FROM attendance 
            WHERE event_name = ? AND attendance_status = 1
        ''', (event_name,))

        visited_count = attendance_cursor.fetchone()[0] or 0

        responses_cursor.execute('''
            SELECT COUNT(*) FROM user_responses 
            WHERE event_id = ? AND response = 'yes'
        ''', (event_id,))

        agreed_count = responses_cursor.fetchone()[0] or 0

        not_visited_count = agreed_count - visited_count
        if not_visited_count < 0:
            not_visited_count = 0

        return {
            'event_name': event_name,
            'event_id': event_id,
            'visited_count': visited_count,
            'agreed_count': agreed_count,
            'not_visited_count': not_visited_count
        }

    except Exception as e:
        print(f"❌ Ошибка получения статистики посещаемости: {e}")
        return None


# ========== ПОЛЬЗОВАТЕЛЬСКИЙ БОТ ==========
user_data = {}


def is_command(text):
    """Проверяет, является ли текст командой (начинается с /)"""
    return text and text.startswith('/')


def is_invalid_name(text):
    """Проверяет, является ли текст недопустимым для имени/фамилии"""
    if is_command(text):
        return True

    if len(text.strip()) < 2:
        return True

    if text.strip().isdigit():
        return True

    invalid_chars = set('!@#$%^&*()_+=[]{}|;:,.<>?~`"')
    if any(char in invalid_chars for char in text):
        return True

    return False


def is_user_registered(user_id):
    """Проверяет, зарегистрирован ли пользователь"""
    users_cursor.execute('SELECT telegram_id FROM users WHERE telegram_id = ?', (user_id,))
    return users_cursor.fetchone() is not None


@user_bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id

    if is_user_registered(user_id):
        users_cursor.execute('SELECT name, surname FROM users WHERE telegram_id = ?', (user_id,))
        user_info = users_cursor.fetchone()
        name, surname = user_info

        already_registered_text = (
            "👋 *Вы уже зарегистрированы!*\n\n"
            f"👤 *Имя:* {name}\n"
            f"👥 *Фамилия:* {surname}\n\n"
            "✅ Вы уже зарегистрированы и будете получать приглашения на мероприятия.\n"
            "Если возникли проблемы с регистрацией обратитесь к админестраторам.\n\n"
            "📱 *Доступные команды:*\n"
            "/id - Узнать свой ID"
        )

        user_bot.send_message(message.chat.id, already_registered_text,
                              parse_mode='Markdown',
                              reply_markup=user_keyboard)
        return

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

    if is_user_registered(user_id):
        user_bot.send_message(user_id,
                              "❌ Вы уже зарегистрированы!\n\n"
                              "Используйте другие команды из меню.",
                              reply_markup=user_keyboard)
        if user_id in user_data:
            del user_data[user_id]
        return

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

    if is_user_registered(user_id):
        user_bot.send_message(user_id,
                              "❌ Вы уже зарегистрированы!\n\n"
                              "Используйте другие команды из меню.",
                              reply_markup=user_keyboard)
        if user_id in user_data:
            del user_data[user_id]
        return

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
        users_cursor.execute(
            'INSERT OR REPLACE INTO users (telegram_id, name, surname) VALUES (?, ?, ?)',
            (user_id, name, surname)
        )
        users_conn.commit()

        success_text = (
            "✅ *Регистрация завершена!*\n\n"
            f"👤 *Имя:* {name}\n"
            f"👥 *Фамилия:* {surname}\n\n"
            "🎯 *Теперь вы будете получать приглашения на мероприятия*\n\n"
            "📱 *Ваши команды:*\n"
            "/id - Узнать свой ID"
        )

        user_bot.send_message(user_id, success_text,
                              parse_mode='Markdown',
                              reply_markup=user_keyboard)

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

    parts = callback_data.split('_')
    if len(parts) != 4:
        user_bot.answer_callback_query(call.id, "❌ Ошибка обработки ответа")
        return

    response_type = parts[1]
    event_id = int(parts[3])

    users_cursor.execute('SELECT name, surname FROM users WHERE telegram_id = ?', (user_id,))
    user_info = users_cursor.fetchone()

    if not user_info:
        user_bot.answer_callback_query(call.id, "❌ Сначала зарегистрируйтесь через /start")
        user_bot.send_message(user_id, "❌ Сначала зарегистрируйтесь: /start", reply_markup=user_keyboard)
        return

    name, surname = user_info

    events_cursor.execute('SELECT event_name, invitation_text, event_photo_id FROM events WHERE event_id = ?',
                          (event_id,))
    event_info = events_cursor.fetchone()

    if not event_info:
        user_bot.answer_callback_query(call.id, "❌ Мероприятие не найдено")
        return

    event_name, invitation_text, event_photo_id = event_info

    message_id = get_invitation_message_id(user_id, event_id)
    if not message_id:
        user_bot.answer_callback_query(call.id, "❌ Сообщение с приглашением не найдено")
        return

    existing_response = check_user_response(user_id, event_id)

    if existing_response:
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
        responses_cursor.execute(
            'INSERT OR REPLACE INTO user_responses (user_id, event_id, response, qr_sent) VALUES (?, ?, ?, 0)',
            (user_id, event_id, response_type)
        )
        responses_conn.commit()

    except Exception as e:
        print(f"❌ Ошибка сохранения ответа: {e}")
        user_bot.answer_callback_query(call.id, "❌ Ошибка сохранения ответа")
        return

    if response_type == 'yes':
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

            qr_message = (
                f"🎉 *Отлично! Вы подтвердили участие!*\n\n"
                f"Мероприятие: *{event_name}*\n\n"
                f"📱 *Это ваш пригласительный QR-код:*\n"
                f"Покажите его на мероприятии и вас пропустят.\n\n"
                f"💡 *Совет:* Сохраните этот QR-код в галерее телефона."
            )

            user_bot.send_message(user_id, qr_message, parse_mode='Markdown')

            qr_image.seek(0)
            user_bot.send_photo(user_id, qr_image,
                                caption=f"QR-код для мероприятия: {event_name}\nКод: {qr_data}")

            mark_qr_sent(user_id, event_id)

            try:
                attendance_cursor.execute(
                    'INSERT OR IGNORE INTO attendance (user_id, event_name, attendance_status) VALUES (?, ?, ?)',
                    (user_id, event_name, 0)
                )
                attendance_conn.commit()
            except Exception as attendance_error:
                print(f"❌ Ошибка создания записи о посещаемости: {attendance_error}")

            print(f"✅ Принял приглашение: {name} {surname} на {event_name}")

            user_bot.answer_callback_query(call.id, "✅ Спасибо за ответ! QR-код отправлен")

        except Exception as e:
            print(f"❌ Ошибка создания QR для {name} {surname}: {e}")
            user_bot.answer_callback_query(call.id, "❌ Ошибка при создании QR-кода")
            user_bot.send_message(user_id,
                                  "❌ Ошибка при создании QR-кода. Попробуйте позже.",
                                  reply_markup=user_keyboard)

    else:
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

        print(f"❌ Отказался: {name} {surname} от {event_name}")

        user_bot.answer_callback_query(call.id, "❌ Ваш отказ сохранен")


@user_bot.message_handler(commands=['id'])
def send_user_id(message):
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

    if user_id in user_data:
        step = user_data[user_id].get('step')
        if step == 'name':
            get_name(message)
        elif step == 'surname':
            get_surname(message)
        return

    if text == "/start" or text == "📝 Регистрация (/start)":
        send_welcome(message)
    elif text == "/id" or text == "🆔 Мой ID (/id)":
        send_user_id(message)
    elif text.startswith('/'):
        user_bot.send_message(message.chat.id,
                              "❌ Неизвестная команда\n\n"
                              "Доступные команды:\n"
                              "/start - Регистрация\n"
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


@admin_bot.message_handler(func=lambda message: message.text == "👥 Статистика посетивших")
def visited_stats_button(message):
    """Обработка кнопки 'Статистика посетивших'"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

    events_cursor.execute('SELECT event_name FROM events ORDER BY event_id')
    events = events_cursor.fetchall()

    if not events:
        admin_bot.send_message(message.chat.id,
                               "❌ Нет созданных мероприятий.\n"
                               "Сначала создайте мероприятие через '📨 Рассылка приглашений'",
                               reply_markup=admin_keyboard)
        return

    events_list = "\n".join([f"• {event[0]}" for event in events])

    admin_bot.send_message(message.chat.id,
                           f"👥 *Статистика посетивших*\n\n"
                           f"📋 *Доступные мероприятия:*\n"
                           f"{events_list}\n\n"
                           f"✍️ *Введите точное название мероприятия:*\n\n"
                           f"Или нажмите ❌ Отмена для отмены",
                           parse_mode='Markdown',
                           reply_markup=cancel_keyboard)

    admin_bot.register_next_step_handler(message, process_visited_stats_request)


def process_visited_stats_request(message):
    """Обрабатывает запрос статистики посетивших"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Получение статистики отменено",
                               reply_markup=admin_keyboard)
        return

    event_name = message.text.strip()
    event = get_event_by_name(event_name)

    if not event:
        admin_bot.send_message(message.chat.id,
                               f"❌ Мероприятие '{event_name}' не найдено!\n\n"
                               f"Убедитесь, что вводите точное название мероприятия.\n"
                               f"Попробуйте снова через меню.",
                               reply_markup=admin_keyboard)
        return

    event_id, event_name = event
    stats = get_attendance_stats(event_id, event_name)

    if not stats:
        admin_bot.send_message(message.chat.id,
                               f"❌ Ошибка получения статистики для мероприятия: {event_name}",
                               reply_markup=admin_keyboard)
        return

    stats_message = (
        f"👥 *Статистика посетивших*\n\n"
        f"🎫 *Мероприятие:* {event_name} (№{event_id})\n\n"
        f"✅ *Согласились прийти:* {stats['agreed_count']} чел.\n"
        f"🎯 *Фактически посетили:* {stats['visited_count']} чел.\n"
        f"❌ *Согласились, но не пришли:* {stats['not_visited_count']} чел.\n\n"
        f"📊 *Статистика основана на отсканированных QR-кодах*"
    )

    admin_bot.send_message(message.chat.id,
                           stats_message,
                           parse_mode='Markdown',
                           reply_markup=admin_keyboard)


@admin_bot.message_handler(func=lambda message: message.text == "📨 Рассылка приглашений")
def admin_sending_button(message):
    """Обработка кнопки 'Рассылка приглашений'"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

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


@admin_bot.message_handler(func=lambda message: message.text == "🔍 Сканировать QR")
def scan_qr_button(message):
    """Обработка кнопки 'Сканировать QR' (СОХРАНЕНА В АДМИН-БОТЕ)"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

    admin_bot.send_message(message.chat.id,
                           "📷 *Сканирование QR-кодов (Админ-бот)*\n\n"
                           "Отправьте фото QR-кода для проверки\n\n"
                           "Или нажмите ❌ Отмена для отмены",
                           parse_mode='Markdown',
                           reply_markup=cancel_keyboard)
    admin_bot.register_next_step_handler(message, process_qr_scan_admin)


def process_qr_scan_admin(message):
    """Обработка фото с QR-кодом в админ-боте (СОХРАНЕНА)"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Сканирование отменено",
                               reply_markup=admin_keyboard)
        return

    if not message.photo:
        admin_bot.send_message(message.chat.id,
                               "❌ Пожалуйста, отправьте фото с QR-кодом\n\n"
                               "Попробуйте снова через меню",
                               reply_markup=admin_keyboard)
        return

    # Используем универсальную функцию обработки QR
    process_qr_photo(admin_bot, message, "ADMIN-BOT")


@admin_bot.message_handler(func=lambda message: message.text == "📢 Объявление")
def announce_button(message):
    """Обработка кнопки 'Объявление'"""
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
                               "Попробуйте снова через меню",
                               reply_markup=admin_keyboard)
        return

    message_text = message.text

    admin_bot.send_message(message.chat.id,
                           f"⏳ Начинаю рассылку...",
                           reply_markup=admin_keyboard)

    broadcast_message_to_all(message.chat.id, message_text)


@admin_bot.message_handler(func=lambda message: message.text == "👤 Редактировать пользователя")
def edit_user_button(message):
    """Обработка кнопки 'Редактировать пользователя'"""
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
        parts = message.text.strip().split()

        if len(parts) < 3:
            admin_bot.send_message(message.chat.id,
                                   "❌ Неверный формат!\n\n"
                                   "Введите в формате: `ID Имя Фамилия`\n"
                                   "Пример: `123456789 Иван Петров`\n\n"
                                   "Попробуйте снова через меню",
                                   parse_mode='Markdown',
                                   reply_markup=admin_keyboard)
            return

        user_id = int(parts[0])
        name = parts[1]
        surname = ' '.join(parts[2:])

        if is_invalid_name(name) or is_invalid_name(surname):
            admin_bot.send_message(message.chat.id,
                                   "⚠️ *Некорректное имя или фамилия!*\n\n"
                                   "Имя и фамилия должны:\n"
                                   "• Быть длиннее 1 символа\n"
                                   "• Содержать только буквы\n"
                                   "• Не быть командой (не начинаться с /)\n"
                                   "• Не содержать спецсимволы\n\n"
                                   "Попробуйте снова через меню",
                                   parse_mode='Markdown',
                                   reply_markup=admin_keyboard)
            return

        users_cursor.execute('SELECT name, surname FROM users WHERE telegram_id = ?', (user_id,))
        user_info = users_cursor.fetchone()

        if not user_info:
            admin_bot.send_message(message.chat.id,
                                   f"❌ Пользователь с ID {user_id} не найден!\n\n"
                                   f"Пользователь должен быть сначала зарегистрирован через /start в пользовательском боте.\n\n"
                                   f"Попробуйте снова через меню",
                                   reply_markup=admin_keyboard)
            return

        old_name, old_surname = user_info

        try:
            users_cursor.execute(
                'UPDATE users SET name = ?, surname = ? WHERE telegram_id = ?',
                (name, surname, user_id)
            )
            users_conn.commit()

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
                               "Пример: `123456789 Иван Петrov`\n\n"
                               "Попробуйте снова через меню",
                               parse_mode='Markdown',
                               reply_markup=admin_keyboard)
    except Exception as e:
        print(f"❌ Ошибка редактирования пользователя: {e}")
        admin_bot.send_message(message.chat.id,
                               f"❌ Ошибка: {str(e)[:100]}\n\n"
                               f"Попробуйте снова через меню",
                               reply_markup=admin_keyboard)


@admin_bot.message_handler(func=lambda message: message.text == "📊 Статистика приглашений")
def stats_button(message):
    """Обработка кнопки 'Статистика приглашений'"""
    admin_id = message.from_user.id

    if admin_id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id,
                               "❌ У вас нет прав администратора!",
                               reply_markup=admin_keyboard)
        return

    events_cursor.execute('SELECT event_name FROM events ORDER BY event_id')
    events = events_cursor.fetchall()

    if not events:
        admin_bot.send_message(message.chat.id,
                               "❌ Нет созданных мероприятий.\n"
                               "Сначала создайте мероприятие через '📨 Рассылка приглашений'",
                               reply_markup=admin_keyboard)
        return

    events_list = "\n".join([f"• {event[0]}" for event in events])

    admin_bot.send_message(message.chat.id,
                           f"📊 *Статистика приглашений*\n\n"
                           f"📋 *Доступные мероприятия:*\n"
                           f"{events_list}\n\n"
                           f"✍️ *Введите точное название мероприятия:*\n\n"
                           f"Или нажмите ❌ Отмена для отмены",
                           parse_mode='Markdown',
                           reply_markup=cancel_keyboard)

    admin_bot.register_next_step_handler(message, process_stats_request)


def process_stats_request(message):
    """Обрабатывает запрос статистики"""
    if message.text == "❌ Отмена":
        admin_bot.send_message(message.chat.id,
                               "❌ Получение статистики отменено",
                               reply_markup=admin_keyboard)
        return

    event_name = message.text.strip()
    event = get_event_by_name(event_name)

    if not event:
        admin_bot.send_message(message.chat.id,
                               f"❌ Мероприятие '{event_name}' не найдено!\n\n"
                               f"Убедитесь, что вводите точное название мероприятия.\n"
                               f"Попробуйте снова через меню.",
                               reply_markup=admin_keyboard)
        return

    event_id, event_name = event
    stats = get_invitation_stats(event_id)
    stats_message = format_stats_message(event_name, stats)

    admin_bot.send_message(message.chat.id,
                           stats_message,
                           parse_mode='Markdown',
                           reply_markup=admin_keyboard)


@admin_bot.message_handler(func=lambda message: message.text == "❌ Отмена операции")
def cancel_operation_button(message):
    """Обработка кнопки 'Отмена операции'"""
    if hasattr(admin_bot, 'user_data') and message.chat.id in admin_bot.user_data:
        admin_bot.send_message(message.chat.id,
                               "❌ Текущая операция отменена",
                               reply_markup=admin_keyboard)
        del admin_bot.user_data[message.chat.id]
    else:
        admin_bot.send_message(message.chat.id,
                               "❌ Нет активной операции для отмена",
                               reply_markup=admin_keyboard)


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
                           f"Или нажмите ❌ Отмена для отмена",
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
                               "❌ Ошибка: данные сессии потеряны.\n\nПопробуйте снова через меню",
                               reply_markup=admin_keyboard)
        return

    user_data = admin_bot.user_data[message.chat.id]
    event_num = user_data.get('event_num', 0)
    event_name = user_data.get('event_name', '')

    if event_num == 0 or not event_name:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: не найдены данные мероприятия.\n\nПопробуйте снова через меню",
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
                               "❌ Ошибка: данные сессии потеряны.\n\nПопробуйте снова через меню",
                               reply_markup=admin_keyboard)
        return

    invitation_text = message.text
    user_data = admin_bot.user_data[message.chat.id]

    event_num = user_data.get('event_num', 0)
    event_name = user_data.get('event_name', '')
    event_photo_id = user_data.get('event_photo_id', None)

    if event_num == 0 or not event_name:
        admin_bot.send_message(message.chat.id,
                               "❌ Ошибка: не найдены данные мероприятия.\n\nПопробуйте снова через меню",
                               reply_markup=admin_keyboard)
        return

    try:
        events_cursor.execute(
            'INSERT INTO events (event_id, event_name, invitation_text, event_photo_id) VALUES (?, ?, ?, ?)',
            (event_num, event_name, invitation_text, event_photo_id)
        )
        events_conn.commit()

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
                               f"Попробуйте снова через меню",
                               reply_markup=admin_keyboard)


@admin_bot.message_handler(commands=['start'])
def admin_start(message):
    admin_bot.send_message(message.chat.id,
                           "👑 *Админ-панель*\n\n"
                           "🛠️ *Доступные функции:*\n"
                           "📨 Рассылка приглашений - Создать мероприятие и разослать приглашения\n"
                           "🔍 Сканировать QR - Проверка QR-кодов на мероприятиях\n"
                           "📢 Объявление - Рассылка сообщений всем пользователям\n"
                           "👤 Редактировать пользователя - Изменить данные пользователя\n"
                           "📊 Статистика приглашений - Получить статистику по приглашениям\n"
                           "👥 Статистика посетивших - Узнать сколько человек пришло на мероприятие\n"
                           "❌ Отмена операции - Отменить текущую операцию\n\n"
                           "✅ Используйте кнопки ниже для навигации",
                           parse_mode='Markdown',
                           reply_markup=admin_keyboard)


@admin_bot.message_handler(func=lambda message: True)
def handle_admin_messages(message):
    if message.text.startswith('/'):
        admin_bot.send_message(message.chat.id,
                               "❌ Неизвестная команда\n\n"
                               "Используйте кнопки меню или /start для просмотра доступных функций",
                               reply_markup=admin_keyboard)
    else:
        admin_bot.send_message(message.chat.id,
                               "👑 Админ-панель\n\n"
                               "Используйте кнопки меню для управления системой",
                               reply_markup=admin_keyboard)


# ========== QR-СКАНЕР БОТ (ОТДЕЛЬНЫЙ ПРОСТОЙ БОТ) ==========
@scanner_bot.message_handler(commands=['start', 'help'])
def scanner_welcome(message):
    """Приветственное сообщение для QR-сканера"""
    welcome_text = (
        "🤖 *QR-Сканер*\n\n"
        "🚀 *Просто отправьте фото QR-кода!*\n\n"
        "📸 *Как использовать:*\n"
        "1. Сфотографируйте QR-код участника\n"
        "2. Отправьте фото в этот чат\n"
        "3. Получите результат сканирования\n\n"
        "✅ *Бот автоматически:*\n"
        "• Проверит QR-код\n"
        "• Найдет пользователя в базе\n"
        "• Отметит посещение\n"
        "• Отправит подтверждение"
    )

    scanner_bot.reply_to(message, welcome_text, parse_mode='Markdown')


@scanner_bot.message_handler(content_types=['photo'])
def handle_scanner_photo(message):
    """Обработка фото в QR-сканер боте"""
    process_qr_photo(scanner_bot, message, "QR-SCANNER")


@scanner_bot.message_handler(func=lambda message: True)
def handle_scanner_other_messages(message):
    """Обработка всех остальных сообщений в QR-сканер боте"""
    help_text = (
        "🤖 *QR-Сканер*\n\n"
        "Этот бот предназначен только для сканирования QR-кодов.\n\n"
        "🚀 *Просто отправьте фото QR-кода!*\n\n"
        "Бот автоматически проверит QR-код и отметит посещение."
    )

    scanner_bot.send_message(message.chat.id, help_text, parse_mode='Markdown')


# ========== ЗАПУСК БОТОВ ==========
def run_bot(bot, bot_name):
    """Запускает бота с перезапуском при ошибках"""
    while True:
        try:
            print(f"🚀 Запуск {bot_name}...")
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            print(f"❌ Ошибка в {bot_name}: {e}")
            print(f"🔄 Перезапуск {bot_name} через 5 секунд...")
            time.sleep(5)


def run_all_bots():
    """Запускает все три бота в отдельных потоках"""
    print("=" * 50)
    print("🤖 ЗАПУСК ВСЕХ БОТОВ")
    print("=" * 50)

    # Создаем потоки для каждого бота
    admin_thread = threading.Thread(target=run_bot, args=(admin_bot, "ADMIN БОТ"), daemon=True)
    user_thread = threading.Thread(target=run_bot, args=(user_bot, "USER БОТ"), daemon=True)
    scanner_thread = threading.Thread(target=run_bot, args=(scanner_bot, "QR-СКАНЕР"), daemon=True)

    # Запускаем все потоки
    admin_thread.start()
    user_thread.start()
    scanner_thread.start()

    print("✅ Все боты запущены в отдельных потоках!")
    print("-" * 50)
    print("📱 *Админ-бот:* /start - Управление системой")
    print("🔍 *QR-Сканер:* Просто отправляйте фото QR-кодов")
    print("👥 *Пользовательский бот:* Работает в фоне")
    print("-" * 50)

    # Держим главный поток активным
    try:
        # Ждем завершения всех потоков
        admin_thread.join()
        user_thread.join()
        scanner_thread.join()
    except KeyboardInterrupt:
        print("\n🛑 Остановка всех ботов...")


if __name__ == '__main__':
    run_all_bots()