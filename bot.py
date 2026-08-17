#!/usr/bin/env python3
"""
Telegram бот для автоматического принятия выгодных обменов на mangabuff.ru
Принимает предложения, где вы отдаёте 1 карту, а получаете 2 и более (2:1, 3:1, 4:1, ...)

Добавлено:
- WebSocket-слушатель (wss://wss10.mangabuff.ru) для мгновенного получения новых обменов.
- Детект капчи по редиректу на /security/page-captcha с уведомлением оператору.
- Адаптивный опрос (/trades) как fallback (базовый интервал 25 сек, максимум 60 сек).
- Поддержание сессии активной через частые запросы (аккаунт всегда онлайн).
- Уведомление при потере авторизации.
"""

import os
import sys
import json
import re
import time
import threading
import html
import random
from pathlib import Path
from urllib.parse import unquote

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ Установите beautifulsoup4: pip install beautifulsoup4")
    sys.exit(1)

try:
    from curl_cffi.requests import Session as CffiSession
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False
    print("[WARN] curl_cffi не установлен, используется requests. Возможны проблемы с Cloudflare.")

try:
    import telebot
    from telebot import types
except ImportError:
    print("❌ Установите pyTelegramBotAPI: pip install pyTelegramBotAPI")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("❌ Установите python-dotenv: pip install python-dotenv")
    sys.exit(1)

# ===== НОВОЕ: websocket-client =====
try:
    import websocket
except ImportError:
    print("❌ Установите websocket-client: pip install websocket-client")
    sys.exit(1)

# ==================== КЛАСС АВТОРИЗАЦИИ ====================
class MangaBuffAuth:
    BASE_URL = "https://mangabuff.ru"

    def __init__(self, proxy: dict = None, impersonate: str = "chrome131"):
        self.impersonate = impersonate
        self._setup_session(proxy)

    def _setup_session(self, proxy):
        if USE_CURL_CFFI:
            self.session = CffiSession(impersonate=self.impersonate)
        else:
            self.session = requests.Session()
        if proxy:
            self.session.proxies.update(proxy)

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.109 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Ch-Ua': '"Google Chrome";v="131", "Not_A Brand";v="8"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
        })

    def _get_csrf_from_cookies(self) -> str:
        xsrf = self.session.cookies.get('XSRF-TOKEN')
        if xsrf:
            return unquote(xsrf)
        for cookie in self.session.cookies:
            name = cookie.name if hasattr(cookie, 'name') else cookie
            if name.upper() == 'XSRF-TOKEN':
                value = cookie.value if hasattr(cookie, 'value') else self.session.cookies[name]
                return unquote(value)
        return ''

    def login(self, email: str, password: str):
        resp = self.session.get(f'{self.BASE_URL}/login')
        if resp.status_code != 200:
            return False, f'GET login failed: HTTP {resp.status_code}'

        csrf = self._get_csrf_from_cookies()
        if not csrf:
            return False, 'CSRF token not found'

        time.sleep(1)

        login_data = {'email': email, 'password': password, 'remember': 'on'}
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-XSRF-TOKEN': csrf,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.BASE_URL}/login',
            'Origin': self.BASE_URL,
        }
        resp = self.session.post(f'{self.BASE_URL}/login', data=login_data, headers=headers, allow_redirects=False)

        check = self.session.get(f'{self.BASE_URL}/')
        if check.status_code != 200:
            return False, 'Auth check failed'

        html_text = check.text
        match = re.search(r'data-userid="(\d+)"', html_text)
        if not match:
            match = re.search(r'/users/(\d+)', html_text)
        if match:
            user_id = match.group(1)
            cookies = []
            for name, value in self.session.cookies.items():
                cookies.append({'name': name, 'value': value, 'domain': 'mangabuff.ru'})
            return True, {'user_id': user_id, 'cookies': cookies}
        else:
            return False, 'User ID not found after login'

    def load_cookies(self, cookies_list: list):
        for c in cookies_list:
            name = c.get('name')
            value = c.get('value')
            domain = c.get('domain', 'mangabuff.ru')
            if name and value:
                self.session.cookies.set(name, value, domain=domain)

    def is_authenticated(self) -> bool:
        try:
            resp = self.session.get(f'{self.BASE_URL}/')
            if resp.status_code != 200:
                return False
            html_text = resp.text
            if re.search(r'data-userid="\d+"', html_text):
                return True
            if 'header__user' in html_text or '/logout' in html_text:
                return True
            return False
        except:
            return False

    def get_user_id(self) -> str:
        resp = self.session.get(f'{self.BASE_URL}/')
        if resp.status_code != 200:
            return None
        match = re.search(r'data-userid="(\d+)"', resp.text)
        if not match:
            match = re.search(r'/users/(\d+)', resp.text)
        return match.group(1) if match else None

    def get_cookie_string(self) -> str:
        """Возвращает строку Cookie для заголовка WebSocket."""
        return '; '.join([f'{name}={value}' for name, value in self.session.cookies.items()])

# ==================== ФУНКЦИИ ПАРСИНГА ОБМЕНОВ ====================
def get_trades(auth: MangaBuffAuth, chat_id=None):
    url = f"{auth.BASE_URL}/trades"
    response = auth.session.get(url)
    if chat_id and is_captcha_response(response):
        handle_captcha_detected(chat_id, auth)
        return []
    if response.status_code != 200:
        return []
    soup = BeautifulSoup(response.text, 'html.parser')
    trades = []
    trade_items = soup.find_all('a', class_=lambda c: c and 'trade__list-item' in c.split())
    for item in trade_items:
        href = item.get('href')
        if not href or '/trades/' not in href:
            continue
        trade_id = href.split('/')[-1]
        trade_url = f"{auth.BASE_URL}{href}"
        info_div = item.find('div', class_='trade__list-info')
        if not info_div:
            continue
        date_elem = info_div.find('div', class_='trade__list-date')
        date = date_elem.text.strip() if date_elem else ""
        name_elem = info_div.find('div', class_='trade__list-name')
        sender_name = name_elem.text.replace('от ', '').strip() if name_elem else ""
        header_div = info_div.find('div', class_='trade__list-header')
        is_new = bool(header_div and header_div.find('span', class_='trade__list-dot--new'))
        trades.append({
            'trade_id': trade_id,
            'sender_name': sender_name,
            'date': date,
            'is_new': is_new,
            'url': trade_url
        })
    return trades

def get_trade_details(auth: MangaBuffAuth, trade_id: str, chat_id=None):
    url = f"{auth.BASE_URL}/trades/{trade_id}"
    response = auth.session.get(url)
    if chat_id and is_captcha_response(response):
        handle_captcha_detected(chat_id, auth)
        return None
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    sender_elem = soup.find('a', class_='trade__header-name')
    if not sender_elem:
        return None
    sender_name = sender_elem.text.strip()
    sender_id = sender_elem.get('href', '').split('/')[-1]
    viewed_elem = soup.find('span', class_='trade__viewed--yes')
    viewed = bool(viewed_elem)

    offered_cards = []
    creator_div = soup.find('div', class_='trade__main-items trade__main-items--creator')
    if creator_div:
        card_links = creator_div.find_all('a', class_='trade__main-item')
        for link in card_links:
            card_url = f"{auth.BASE_URL}{link.get('href')}"
            card_id = card_url.split('/')[-2] if '/cards/' in card_url else ''
            img = link.find('img')
            img_url = img.get('src') if img else ''
            offered_cards.append({'card_id': card_id, 'url': card_url, 'image': img_url})

    required_cards = []
    receiver_div = soup.find('div', class_='trade__main-items trade__main-items--receiver')
    if receiver_div:
        card_links = receiver_div.find_all('a', class_='trade__main-item')
        for link in card_links:
            card_url = f"{auth.BASE_URL}{link.get('href')}"
            card_id = card_url.split('/')[-2] if '/cards/' in card_url else ''
            img = link.find('img')
            img_url = img.get('src') if img else ''
            required_cards.append({'card_id': card_id, 'url': card_url, 'image': img_url})

    return {
        'trade_id': trade_id,
        'sender_id': sender_id,
        'sender_name': sender_name,
        'offered_cards': offered_cards,
        'required_cards': required_cards,
        'viewed': viewed,
        'url': f"{auth.BASE_URL}/trades/{trade_id}"
    }

def accept_trade(auth: MangaBuffAuth, trade_id: str, chat_id=None, max_retries: int = 3):
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"[RETRY] Попытка {attempt + 1}/{max_retries} для обмена {trade_id}")
            time.sleep(10)
        
        csrf = auth._get_csrf_from_cookies()
        if not csrf:
            if attempt == max_retries - 1:
                return False, "CSRF token not found"
            continue
        
        headers = {
            'X-XSRF-TOKEN': csrf,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f"{auth.BASE_URL}/trades/{trade_id}",
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }
        
        endpoints = [
            f"{auth.BASE_URL}/trades/accept",
            f"{auth.BASE_URL}/trades/accept/{trade_id}",
            f"{auth.BASE_URL}/trades/{trade_id}/accept",
        ]
        
        for endpoint in endpoints:
            try:
                resp = auth.session.post(endpoint, headers=headers, data={'trade_id': trade_id})
                if chat_id and is_captcha_response(resp):
                    handle_captcha_detected(chat_id, auth)
                    return False, "Обнаружена капча"
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                        if data.get('error'):
                            continue
                    except:
                        pass
                    return True, "Обмен успешно принят!"
            except Exception as e:
                continue
        
        if attempt == max_retries - 1:
            return False, f"Не удалось принять обмен после {max_retries} попыток"
    
    return False, "Не удалось принять обмен"

# ===== НОВОЕ: проверка капчи =====
def is_captcha_response(resp) -> bool:
    try:
        url = getattr(resp, 'url', None)
        if url and 'page-captcha' in str(url):
            return True
        if resp.status_code == 403:
            return True
    except:
        pass
    return False

captcha_paused = False
captcha_paused_lock = threading.Lock()
captcha_notified = False

# ==================== НАСТРОЙКИ БОТА ====================
BOT_TOKEN = os.getenv("TRADE_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ Не найден TRADE_BOT_TOKEN или BOT_TOKEN в .env файле")
    sys.exit(1)

SESSIONS_FILE = Path(__file__).parent / "tg_sessions.json"
PROCESSED_TRADES_FILE = Path(__file__).parent / "processed_trades.json"

sessions = {}
processed_trades = set()
monitoring_active = False
monitoring_thread = None

ws_thread = None
ws_running = False
ws_connected = False
ws_should_stop = False
ws_lock = threading.Lock()

def load_sessions():
    global sessions
    if SESSIONS_FILE.exists():
        try:
            sessions = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except:
            sessions = {}

def save_sessions():
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

def load_processed_trades():
    global processed_trades
    if PROCESSED_TRADES_FILE.exists():
        try:
            data = json.loads(PROCESSED_TRADES_FILE.read_text(encoding="utf-8"))
            processed_trades = set(data.get("trades", []))
        except:
            processed_trades = set()

def save_processed_trades():
    data = {"trades": list(processed_trades)}
    PROCESSED_TRADES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

load_sessions()
load_processed_trades()

bot = telebot.TeleBot(BOT_TOKEN)

def get_auth_for_user(chat_id: int) -> MangaBuffAuth:
    auth = MangaBuffAuth()
    if str(chat_id) in sessions:
        cookies = sessions[str(chat_id)].get('cookies', [])
        if cookies:
            auth.load_cookies(cookies)
    return auth

def save_user_session(chat_id: int, user_id: str, cookies: list):
    sessions[str(chat_id)] = {'user_id': user_id, 'cookies': cookies}
    save_sessions()

def clear_user_session(chat_id: int):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

def get_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(
        types.KeyboardButton("🔁 Мониторинг обменов"),
        types.KeyboardButton("📊 Статус"),
    )
    return markup

def handle_captcha_detected(chat_id, auth):
    global captcha_paused, captcha_notified
    with captcha_paused_lock:
        if captcha_paused:
            return
        captcha_paused = True
        captcha_notified = False

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Прошёл — продолжить", callback_data="captcha_solved"))
    bot.send_message(
        chat_id,
        "⚠️ **Сайт показывает капчу!**\n"
        "Пожалуйста, зайди на mangabuff.ru и пройди проверку вручную.\n"
        "После этого нажми кнопку ниже, чтобы продолжить работу бота.",
        parse_mode='Markdown',
        reply_markup=markup
    )
    captcha_notified = True
    print(f"[CAPTCHA] Капча обнаружена для чата {chat_id}, бот на паузе.")

def process_new_trade(chat_id, auth, trade_id):
    global processed_trades
    if trade_id in processed_trades:
        return

    details = get_trade_details(auth, trade_id, chat_id=chat_id)
    if not details:
        return

    offered_count = len(details['offered_cards'])
    required_count = len(details['required_cards'])

    accept = (required_count == 1 and offered_count >= 2)
    result_msg = ""
    if accept:
        success, msg = accept_trade(auth, trade_id, chat_id=chat_id, max_retries=3)
        if success:
            result_msg = "✅ **Обмен автоматически ПРИНЯТ!**"
        else:
            result_msg = f"❌ **Не удалось принять обмен после 3 попыток**: {msg}"
    else:
        if required_count != 1:
            reason = f"вы отдаёте {required_count} карт (нужно ровно 1)"
        elif offered_count < 2:
            reason = f"вам предлагают только {offered_count} карт (нужно 2 и более)"
        else:
            reason = "неподходящие условия"
        result_msg = f"⏩ **Обмен проигнорирован** ({offered_count}:{required_count}) – {reason}"

    message = f"🔄 **Новое предложение обмена**\n\n"
    message += f"👤 *Отправитель:* {html.escape(details['sender_name'])}\n"
    message += f"🔗 [Ссылка на обмен]({details['url']})\n\n"
    message += f"📦 *Предлагают:* {offered_count} карт\n"
    for card in details['offered_cards']:
        message += f"  • [Карта]({card['url']})\n"
    message += f"\n📤 *Вы отдаёте:* {required_count} карт\n"
    for card in details['required_cards']:
        message += f"  • [Карта]({card['url']})\n"
    message += f"\n{result_msg}"

    try:
        bot.send_message(chat_id, message, parse_mode='Markdown', disable_web_page_preview=True)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

    processed_trades.add(trade_id)
    save_processed_trades()

# ===== WebSocket клиент =====
def ws_on_message(ws, message, chat_id, auth):
    global ws_connected
    try:
        if not message:
            return
        if message.startswith('0'):
            ws.send('40')
            print("[WS] Sent '40' (connect)")
        elif message.startswith('40'):
            user_id = auth.get_user_id()
            if user_id:
                join_msg = f'42["joinRoom",{{"room":"/","userId":{user_id}}}]'
                ws.send(join_msg)
                print(f"[WS] Sent joinRoom for user {user_id}")
                ws_connected = True
            else:
                print("[WS] Не удалось получить user_id для joinRoom")
        elif message == '2':
            ws.send('3')
            print("[WS] Pong sent")
        elif message.startswith('42'):
            try:
                payload = json.loads(message[2:])
                if isinstance(payload, list) and len(payload) >= 2:
                    event = payload[0]
                    data = payload[1] if len(payload) > 1 else {}
                    if event == 'new-sendNewTrade':
                        trade_id = data.get('tradeId') or data.get('id')
                        if trade_id and trade_id not in processed_trades:
                            print(f"[WS] Получен новый обмен {trade_id}")
                            threading.Thread(
                                target=process_new_trade,
                                args=(chat_id, auth, trade_id),
                                daemon=True
                            ).start()
            except json.JSONDecodeError:
                pass
    except Exception as e:
        print(f"[WS] Ошибка обработки сообщения: {e}")

def ws_on_error(ws, error, chat_id):
    print(f"[WS] Ошибка: {error}")

def ws_on_close(ws, close_status_code, close_msg, chat_id):
    global ws_connected, ws_running
    ws_connected = False
    print(f"[WS] Соединение закрыто (код {close_status_code})")

def ws_on_open(ws, chat_id, auth):
    print("[WS] Соединение установлено, ожидаем рукопожатие...")

def ws_loop(chat_id, auth):
    global ws_running, ws_should_stop
    ws_running = True
    ws_should_stop = False

    while not ws_should_stop:
        if captcha_paused:
            print("[WS] Капча приостановлена, WS спит...")
            time.sleep(10)
            continue

        try:
            cookie_str = auth.get_cookie_string()
            if not cookie_str:
                print("[WS] Нет cookies, невозможно подключиться")
                time.sleep(5)
                continue

            ws = websocket.WebSocketApp(
                "wss://wss10.mangabuff.ru/socket.io/?EIO=4&transport=websocket",
                header=[
                    f"Cookie: {cookie_str}",
                    "Origin: https://mangabuff.ru",
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.109 Safari/537.36"
                ],
                on_message=lambda ws, msg: ws_on_message(ws, msg, chat_id, auth),
                on_error=lambda ws, err: ws_on_error(ws, err, chat_id),
                on_close=lambda ws, code, msg: ws_on_close(ws, code, msg, chat_id),
                on_open=lambda ws: ws_on_open(ws, chat_id, auth)
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
            if not ws_should_stop:
                print("[WS] Переподключение через 3 секунды...")
                time.sleep(3)
        except Exception as e:
            print(f"[WS] Исключение: {e}")
            time.sleep(5)

    ws_running = False
    print("[WS] Остановлен.")

def start_ws(chat_id, auth):
    global ws_thread, ws_running
    if ws_thread and ws_thread.is_alive():
        print("[WS] Уже запущен")
        return
    ws_running = False
    ws_thread = threading.Thread(target=ws_loop, args=(chat_id, auth), daemon=True)
    ws_thread.start()

def stop_ws():
    global ws_should_stop, ws_running
    ws_should_stop = True
    if ws_thread:
        ws_thread.join(timeout=5)
    ws_running = False
    print("[WS] Остановлен по запросу.")

# ==================== МОНИТОРИНГ (адаптивный опрос) ====================
def monitoring_loop(chat_id):
    global monitoring_active, captcha_paused
    print(f"[TRADE-MONITOR] Запуск для чата {chat_id}")
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        monitoring_active = False
        return

    # Запускаем WS
    start_ws(chat_id, auth)

    # ИЗМЕНЕНО: сообщение о частоте опроса
    bot.send_message(chat_id, f"🔁 Мониторинг обменов запущен (WebSocket + опрос каждые ~25 сек).\nПринимаются обмены, где вы отдаёте 1 карту, а получаете 2 и более (2:1, 3:1, ...).")

    # ИЗМЕНЕНО: базовый интервал 25 сек, максимум 60 сек
    base_interval = 25
    max_interval = 60
    interval = base_interval
    empty_count = 0
    empty_threshold = 6  # после 6 пустых проверок начинаем увеличивать интервал

    # НОВОЕ: периодическая проверка сессии (каждые 5 минут)
    session_check_counter = 0
    SESSION_CHECK_INTERVAL = 300  # 5 минут в секундах

    while monitoring_active:
        with captcha_paused_lock:
            if captcha_paused:
                time.sleep(10)
                continue

        try:
            trades = get_trades(auth, chat_id=chat_id)
            if captcha_paused:
                continue

            # НОВОЕ: проверка сессии раз в 5 минут
            session_check_counter += interval
            if session_check_counter >= SESSION_CHECK_INTERVAL:
                session_check_counter = 0
                if not auth.is_authenticated():
                    bot.send_message(
                        chat_id,
                        "⚠️ **Сессия потеряна!**\n"
                        "Пожалуйста, выполните повторный вход командой /login."
                    )
                    monitoring_active = False
                    break

            new_trades = [t for t in trades if t['trade_id'] not in processed_trades]
            if new_trades:
                empty_count = 0
                interval = base_interval
                for trade in new_trades:
                    if trade['trade_id'] not in processed_trades:
                        process_new_trade(chat_id, auth, trade['trade_id'])
            else:
                empty_count += 1
                if empty_count > empty_threshold:
                    interval = min(interval + 5, max_interval)
                else:
                    interval = base_interval

            jitter = random.uniform(-0.1, 0.1) * interval
            sleep_time = max(15, interval + jitter)  # не меньше 15 секунд

            for _ in range(int(sleep_time)):
                if not monitoring_active or captcha_paused:
                    break
                time.sleep(1)

        except Exception as e:
            print(f"[TRADE-MONITOR] Ошибка: {e}")
            time.sleep(30)

    stop_ws()
    bot.send_message(chat_id, "🔕 Мониторинг обменов остановлен.")

# ==================== КОМАНДЫ БОТА ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Бот для автоматического обмена картами на mangabuff.ru\n\n"
        "Команды:\n"
        "/login email password – войти в аккаунт\n"
        "/logout – выйти\n"
        "/status – проверить авторизацию\n"
        "/monitor_start – запустить мониторинг обменов (автопринятие, если вы отдаёте 1 карту, а получаете 2+)\n"
        "/monitor_stop – остановить мониторинг\n\n"
        "Используйте кнопки для управления.",
        reply_markup=get_keyboard()
    )

@bot.message_handler(commands=['login'])
def cmd_login(message):
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 3:
        bot.send_message(chat_id, "❌ Использование: /login email password")
        return
    email = args[1]
    password = args[2]

    bot.send_message(chat_id, "⏳ Выполняю вход...")
    auth = MangaBuffAuth()
    success, result = auth.login(email, password)

    if success:
        user_id = result['user_id']
        save_user_session(chat_id, user_id, result['cookies'])
        bot.send_message(chat_id, f"✅ Успешный вход!\nВаш user_id: {user_id}\nСессия сохранена.")
    else:
        bot.send_message(chat_id, f"❌ Ошибка входа: {result}")

@bot.message_handler(commands=['logout'])
def cmd_logout(message):
    chat_id = message.chat.id
    clear_user_session(chat_id)
    bot.send_message(chat_id, "👋 Вы вышли. Сессия очищена.")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    auth = get_auth_for_user(chat_id)
    if auth.is_authenticated():
        user_id = auth.get_user_id()
        bot.send_message(chat_id, f"🟢 Вы авторизованы\nUser ID: {user_id}")
    else:
        bot.send_message(chat_id, "🔴 Вы не авторизованы. Используйте /login")

@bot.message_handler(commands=['monitor_start'])
def cmd_monitor_start(message):
    global monitoring_active, monitoring_thread
    chat_id = message.chat.id
    if monitoring_active:
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен.")
        return
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        return
    global captcha_paused, captcha_notified
    with captcha_paused_lock:
        captcha_paused = False
        captcha_notified = False
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, args=(chat_id,), daemon=True)
    monitoring_thread.start()
    bot.send_message(chat_id, "✅ Мониторинг обменов запущен.")

@bot.message_handler(commands=['monitor_stop'])
def cmd_monitor_stop(message):
    global monitoring_active
    if not monitoring_active:
        bot.send_message(message.chat.id, "ℹ️ Мониторинг не запущен.")
        return
    monitoring_active = False
    bot.send_message(message.chat.id, "⏹ Мониторинг остановлен.")

@bot.message_handler(func=lambda m: m.text in ["🔁 Мониторинг обменов", "📊 Статус"])
def handle_buttons(message):
    text = message.text
    chat_id = message.chat.id
    if text == "🔁 Мониторинг обменов":
        if monitoring_active:
            bot.send_message(chat_id, "⚠️ Мониторинг уже запущен. Используйте /monitor_stop для остановки.")
        else:
            cmd_monitor_start(message)
    elif text == "📊 Статус":
        cmd_status(message)

@bot.callback_query_handler(func=lambda call: call.data == "captcha_solved")
def callback_captcha_solved(call):
    global captcha_paused, captcha_notified
    chat_id = call.message.chat.id
    with captcha_paused_lock:
        if not captcha_paused:
            bot.answer_callback_query(call.id, "Капча не была обнаружена, всё в порядке.")
            return
        captcha_paused = False
        captcha_notified = False

    auth = get_auth_for_user(chat_id)
    if auth.is_authenticated():
        bot.edit_message_text(
            "✅ Капча пройдена! Сессия активна. Бот продолжает работу.",
            chat_id=chat_id,
            message_id=call.message.message_id
        )
        bot.answer_callback_query(call.id, "Продолжаем работу!")
    else:
        bot.edit_message_text(
            "❌ Сессия не активна. Пожалуйста, выполните вход заново командой /login.",
            chat_id=chat_id,
            message_id=call.message.message_id
        )
        bot.answer_callback_query(call.id, "Требуется перелогин.")
        with captcha_paused_lock:
            captcha_paused = False

def run_bot():
    while True:
        try:
            print("✅ Торговый бот запущен (WS + капча). Нажмите Ctrl+C для остановки.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"❌ Ошибка соединения: {e}. Переподключение через 10 секунд...")
            time.sleep(10)

if __name__ == '__main__':
    run_bot()