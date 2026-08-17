#!/usr/bin/env python3
"""
Telegram бот для автоматического принятия выгодных обменов на mangabuff.ru
Принимает предложения, где:
1. Вы отдаёте ровно 1 карту и получаете 1 или более карт (1:1, 2:1, 3:1, ...)
Работает с WebSocket + адаптивный HTTP-опрос (keep-alive каждые 25–60 сек)
"""

import os
import sys
import json
import re
import time
import threading
import html
import logging
from pathlib import Path
from urllib.parse import unquote

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ Установите beautifulsoup4: pip install beautifulsoup4")
    sys.exit(1)

# ========== curl_cffi (з перевіркою підтримки) ==========
USE_CURL_CFFI = True
try:
    from curl_cffi.requests import Session as CffiSession
except ImportError:
    USE_CURL_CFFI = False
    print("[WARN] curl_cffi не установлен, используем requests. Возможны проблемы с Cloudflare.")
    import requests

# Тільки ці версії підтримуються і працюють
SUPPORTED_IMPERSONATE = ["chrome131", "chrome133", "chrome134"]

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

try:
    import websocket
except ImportError:
    print("❌ Установите websocket-client: pip install websocket-client")
    sys.exit(1)

# ---------- настройка логирования ----------
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КЛАСС АВТОРИЗАЦИИ ====================
class MangaBuffAuth:
    BASE_URL = "https://mangabuff.ru"

    def __init__(self, proxy: dict = None, impersonate: str = "chrome133"):
        if impersonate not in SUPPORTED_IMPERSONATE:
            logger.warning(f"Impersonate '{impersonate}' не підтримується, використовуємо chrome133")
            impersonate = "chrome133"
        self.impersonate = impersonate
        self.proxy = proxy
        self._setup_session(proxy)

    def _setup_session(self, proxy):
        if USE_CURL_CFFI:
            self.session = CffiSession(impersonate=self.impersonate)
        else:
            self.session = requests.Session()
        if proxy:
            self.session.proxies.update(proxy)

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Sec-Ch-Ua': '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
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

    def _get_csrf_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, 'html.parser')
        meta = soup.find('meta', {'name': 'csrf-token'})
        if meta and meta.get('content'):
            return meta['content']
        token_input = soup.find('input', {'name': '_token'})
        if token_input and token_input.get('value'):
            return token_input['value']
        return ''

    def _request(self, method, url, **kwargs):
        global captcha_paused, captcha_notified
        if captcha_paused:
            logger.warning("⏸️ Бот на паузе из-за капчи, запрос пропущен")
            return None

        if 'timeout' not in kwargs:
            kwargs['timeout'] = 30

        try:
            response = self.session.request(method, url, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка запроса: {e}")
            return None

        final_url = response.url if hasattr(response, 'url') else None
        if final_url and "page-captcha" in final_url:
            logger.warning("⚠️ Обнаружена капча! Ставим бота на паузу.")
            captcha_paused = True
            captcha_notified = False
            save_captcha_pause()
            notify_captcha_operator()
            return None
        return response

    def get(self, url, **kwargs):
        return self._request('GET', url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        return self._request('POST', url, data=data, json=json, **kwargs)

    def login(self, email: str, password: str):
        last_error = None

        for imp in SUPPORTED_IMPERSONATE:
            try:
                logger.info(f"🔄 Пробуем вход с impersonate={imp}")
                if USE_CURL_CFFI:
                    self.session = CffiSession(impersonate=imp)
                else:
                    self.session = requests.Session()
                if self.proxy:
                    self.session.proxies.update(self.proxy)

                self.session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Sec-Ch-Ua': '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"Windows"',
                    'Upgrade-Insecure-Requests': '1',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                })

                resp = self.session.get(f'{self.BASE_URL}/login', timeout=30)
                if resp.status_code == 403:
                    logger.warning(f"⚠️ 403 Forbidden з {imp}, пробуємо наступний")
                    time.sleep(3)
                    continue
                if resp.status_code != 200:
                    last_error = f'GET login failed: HTTP {resp.status_code}'
                    continue

                csrf = self._get_csrf_from_cookies()
                if not csrf:
                    csrf = self._get_csrf_from_html(resp.text)
                if not csrf:
                    last_error = 'CSRF token not found'
                    continue

                login_data = {'email': email, 'password': password, 'remember': 'on'}
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-XSRF-TOKEN': csrf,
                    'X-CSRF-TOKEN': csrf,
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': f'{self.BASE_URL}/login',
                    'Origin': self.BASE_URL,
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                }

                resp = self.session.post(
                    f'{self.BASE_URL}/login',
                    data=login_data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=30
                )

                check = self.session.get(f'{self.BASE_URL}/', timeout=30)
                if check.status_code != 200:
                    last_error = 'Auth check failed'
                    continue

                if "page-captcha" in str(check.url):
                    last_error = 'Сайт показывает капчу. Пройдите её вручную и попробуйте снова.'
                    continue

                html_text = check.text
                match = re.search(r'data-userid="(\d+)"', html_text)
                if not match:
                    match = re.search(r'/users/(\d+)', html_text)

                if match:
                    user_id = match.group(1)
                    cookies = []
                    for name, value in self.session.cookies.items():
                        cookies.append({'name': name, 'value': value, 'domain': 'mangabuff.ru'})
                    logger.info(f"✅ Успешный вход з {imp}, user_id={user_id}")
                    self.impersonate = imp
                    return True, {
                        'user_id': user_id,
                        'cookies': cookies,
                        'impersonate': imp
                    }
                else:
                    last_error = 'User ID not found after login'
            except Exception as e:
                last_error = str(e)
                logger.error(f"❌ Ошибка при попытке {imp}: {e}")
                time.sleep(5)

        return False, last_error or "Не удалось войти после всех попыток"

    def load_cookies(self, cookies_list: list, impersonate: str = None):
        if impersonate and impersonate not in SUPPORTED_IMPERSONATE:
            logger.warning(f"Impersonate '{impersonate}' не підтримується, використовуємо chrome133")
            impersonate = "chrome133"
        if impersonate:
            self.impersonate = impersonate
            self._setup_session(self.proxy)
        for c in cookies_list:
            name = c.get('name')
            value = c.get('value')
            domain = c.get('domain', 'mangabuff.ru')
            if name and value:
                self.session.cookies.set(name, value, domain=domain)

    def is_authenticated(self) -> bool:
        try:
            resp = self.get(f'{self.BASE_URL}/')
            if resp is None or resp.status_code != 200:
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
        resp = self.get(f'{self.BASE_URL}/')
        if resp is None or resp.status_code != 200:
            return None
        match = re.search(r'data-userid="\d+"', resp.text)
        if not match:
            match = re.search(r'/users/(\d+)', resp.text)
        return match.group(1) if match else None

    def get_cookies_dict(self):
        return {name: value for name, value in self.session.cookies.items()}

# ==================== ФУНКЦИИ ПАРСИНГА ====================
def get_trades(auth: MangaBuffAuth):
    url = f"{auth.BASE_URL}/trades"
    response = auth.get(url)
    if response is None or response.status_code != 200:
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

def get_trade_details(auth: MangaBuffAuth, trade_id: str):
    url = f"{auth.BASE_URL}/trades/{trade_id}"
    response = auth.get(url)
    if response is None or response.status_code != 200:
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

def accept_trade(auth: MangaBuffAuth, trade_id: str, max_retries: int = 3):
    for attempt in range(max_retries):
        if attempt > 0:
            logger.info(f"[RETRY] Попытка {attempt+1}/{max_retries} для обмена {trade_id}")
            time.sleep(10)

        if captcha_paused:
            return False, "Бот на паузе из-за капчи"

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
                resp = auth.post(endpoint, headers=headers, data={'trade_id': trade_id})
                if resp is None:
                    return False, "Бот на паузе из-за капчи"
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

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("TRADE_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ Не найден TRADE_BOT_TOKEN или BOT_TOKEN в .env файле")
    sys.exit(1)

# ===== ИЗМЕНЕНО: частый опрос для поддержания сессии =====
CHECK_INTERVAL = 25           # было 60  – опрос каждые 25 сек
MAX_CHECK_INTERVAL = 60       # было 300 – даже при простое не реже 1 минуты

SESSIONS_FILE = Path(__file__).parent / "tg_sessions.json"
PROCESSED_TRADES_FILE = Path(__file__).parent / "processed_trades.json"
CAPTCHA_PAUSE_FILE = Path(__file__).parent / "captcha_pause.json"

sessions = {}
processed_trades = set()
processed_lock = threading.Lock()

monitoring_active = False
monitoring_thread = None

ws_running = False
ws_thread = None
ws_connection = None
ws_connected = False
ws_auth = None
ws_chat_id = None

captcha_paused = False
captcha_notified = False

current_check_interval = CHECK_INTERVAL
last_trade_time = None

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
    else:
        processed_trades = set()
    logger.info(f"Загружено processed_trades: {processed_trades}")

def save_processed_trades():
    data = {"trades": list(processed_trades)}
    PROCESSED_TRADES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Сохранено processed_trades: {data}")

def load_captcha_pause():
    global captcha_paused, captcha_notified
    if CAPTCHA_PAUSE_FILE.exists():
        try:
            data = json.loads(CAPTCHA_PAUSE_FILE.read_text(encoding="utf-8"))
            captcha_paused = data.get("paused", False)
            captcha_notified = data.get("notified", False)
        except:
            pass

def save_captcha_pause():
    CAPTCHA_PAUSE_FILE.write_text(
        json.dumps({"paused": captcha_paused, "notified": captcha_notified}, indent=2),
        encoding="utf-8"
    )

load_sessions()
load_processed_trades()
load_captcha_pause()

bot = telebot.TeleBot(BOT_TOKEN)

def get_auth_for_user(chat_id: int) -> MangaBuffAuth:
    chat_str = str(chat_id)
    if chat_str in sessions:
        data = sessions[chat_str]
        cookies = data.get('cookies', [])
        impersonate = data.get('impersonate', 'chrome133')
        auth = MangaBuffAuth(impersonate=impersonate)
        if cookies:
            auth.load_cookies(cookies, impersonate=impersonate)
        return auth
    return MangaBuffAuth()

def save_user_session(chat_id: int, user_id: str, cookies: list, impersonate: str):
    sessions[str(chat_id)] = {
        'user_id': user_id,
        'cookies': cookies,
        'impersonate': impersonate
    }
    save_sessions()

def clear_user_session(chat_id: int):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

# ---------- уведомление о капче ----------
def notify_captcha_operator():
    global captcha_notified
    if captcha_notified:
        return
    for chat_id_str in sessions.keys():
        try:
            chat_id = int(chat_id_str)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Прошёл — продолжить", callback_data="captcha_resolved"))
            bot.send_message(
                chat_id,
                "⚠️ **Сайт mangabuff.ru показывает капчу!**\n"
                "Пожалуйста, зайди на сайт и пройди проверку вручную.\n"
                "После этого нажми кнопку ниже, чтобы бот продолжил работу.",
                reply_markup=markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление о капче в чат {chat_id}: {e}")
    captcha_notified = True
    save_captcha_pause()

@bot.callback_query_handler(func=lambda call: call.data == "captcha_resolved")
def handle_captcha_resolved(call):
    global captcha_paused, captcha_notified, ws_auth, ws_chat_id
    chat_id = call.message.chat.id

    auth = get_auth_for_user(chat_id)
    if not auth:
        bot.answer_callback_query(call.id, "❌ Нет сессии. Войдите заново.")
        return

    resp = auth.get(f"{auth.BASE_URL}/")
    if resp is None or "page-captcha" in str(resp.url):
        bot.answer_callback_query(call.id, "❌ Капча всё ещё активна. Пройдите её вручную и нажмите снова.")
        return

    ws_auth = auth
    ws_chat_id = chat_id

    if ws_running:
        logger.info("🔄 Перезапускаем WebSocket с обновлённой сессией")
        stop_websocket()
        time.sleep(2)
        start_websocket(chat_id, auth)

    captcha_paused = False
    captcha_notified = False
    save_captcha_pause()
    bot.answer_callback_query(call.id, "✅ Пауза снята! Бот продолжает работу.")
    bot.send_message(chat_id, "✅ Капча пройдена. WebSocket переподключен с новой сессией.")

# ---------- ОБРАБОТКА ОБМЕНА ----------
def process_trade(trade_id, auth, chat_id):
    logger.info(f"▶️ Начинаем обработку обмена {trade_id}")
    try:
        details = get_trade_details(auth, trade_id)
        if not details:
            logger.warning(f"❌ Не удалось получить детали обмена {trade_id}")
            return False

        offered_count = len(details['offered_cards'])
        required_count = len(details['required_cards'])
        logger.info(f"📊 Обмен {trade_id}: предлагают {offered_count}, просят {required_count}")

        accept = (required_count == 1 and offered_count >= 1)
        result_msg = ""

        if accept:
            logger.info(f"✅ Условие выполнено, принимаем обмен {trade_id}")
            success, msg = accept_trade(auth, trade_id, max_retries=3)
            if success:
                result_msg = "✅ **Обмен автоматически ПРИНЯТ!**"
            else:
                result_msg = f"❌ **Не удалось принять обмен**: {msg}"
        else:
            if required_count != 1:
                reason = f"вы отдаёте {required_count} карт (нужно ровно 1)"
            elif offered_count < 1:
                reason = f"вам предлагают {offered_count} карт (нужно 1 и более)"
            else:
                reason = "неподходящие условия"
            result_msg = f"⏩ **Обмен проигнорирован** (получаете:{offered_count} / отдаёте:{required_count}) – {reason}"
            logger.info(f"⏩ Обмен {trade_id} проигнорирован: {reason}")

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
            logger.info(f"📨 Отправлено сообщение в чат {chat_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения: {e}")

        return success if accept else False
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в process_trade: {e}", exc_info=True)
        return False

# ---------- РЕЗЕРВНЫЙ ОПРОС ----------
def check_and_process_new_trades(auth, chat_id):
    global current_check_interval, last_trade_time

    logger.info("🔍 Резервный опрос /trades...")
    trades = get_trades(auth)
    if not trades:
        logger.info("ℹ️ Нет обменов в списке")
        # адаптивное увеличение интервала, но не выше MAX_CHECK_INTERVAL
        current_check_interval = min(current_check_interval + 5, MAX_CHECK_INTERVAL)
        return 0

    logger.info(f"📋 Найдено {len(trades)} обменов")
    new_trades = []
    for t in trades:
        if t['trade_id'] not in processed_trades:
            new_trades.append(t)
            processed_trades.add(t['trade_id'])
    save_processed_trades()

    if not new_trades:
        logger.info("ℹ️ Новых обменов нет")
        return 0

    logger.info(f"⚡ Найдено {len(new_trades)} новых обменов, обрабатываю...")
    current_check_interval = CHECK_INTERVAL
    last_trade_time = time.time()

    for trade in new_trades:
        process_trade(trade['trade_id'], auth, chat_id)
        time.sleep(0.5)

    return len(new_trades)

# ---------- WEBSOCKET ----------
def start_websocket(chat_id, auth):
    global ws_running, ws_thread
    if ws_running:
        logger.info("WS уже запущен")
        return

    ws_running = True
    ws_thread = threading.Thread(target=websocket_thread, args=(chat_id, auth), daemon=True)
    ws_thread.start()
    logger.info("🚀 WebSocket поток запущен")

def stop_websocket():
    global ws_running, ws_connection
    ws_running = False
    if ws_connection:
        ws_connection.close()
    logger.info("🛑 WebSocket остановлен")

def websocket_thread(chat_id, auth):
    global ws_connection, ws_connected, ws_running

    cookies = auth.get_cookies_dict()
    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    headers = {
        "Cookie": cookie_str,
        "Origin": "https://mangabuff.ru",
        "User-Agent": auth.session.headers.get("User-Agent", "Mozilla/5.0")
    }
    logger.info(f"🔗 Заголовки WebSocket: {headers}")

    while ws_running:
        try:
            logger.info("🔄 Подключаемся к WebSocket...")
            ws_connection = websocket.WebSocketApp(
                "wss://wss10.mangabuff.ru/socket.io/?EIO=4&transport=websocket",
                on_open=lambda ws: on_ws_open(ws, auth),
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
                header=headers
            )
            # ===== ИЗМЕНЕНО: добавлены ping_interval и ping_timeout =====
            ws_connection.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.error(f"❌ Ошибка WebSocket: {e}")
            time.sleep(5)
        if ws_running:
            time.sleep(5)

def on_ws_open(ws, auth):
    global ws_connected
    ws_connected = True
    logger.info("✅ WebSocket соединение установлено")

def on_ws_message(ws, message):
    global ws_connected, ws_running, ws_auth, ws_chat_id
    try:
        msg = str(message)
        logger.info(f"WS: получено сообщение: {msg[:200]}")

        if msg == '2':
            ws.send('3')
            return

        if msg.startswith('0'):
            ws.send('40')
            logger.debug("WS: отправлен '40' (connect)")
            return

        if msg.startswith('40'):
            if ws_auth:
                user_id = ws_auth.get_user_id()
                if user_id:
                    join_msg = f'42["joinRoom",{{"room":"/","userId":{user_id}}}]'
                    ws.send(join_msg)
                    logger.info(f"✅ WS: отправлен joinRoom для userId={user_id}")
                else:
                    logger.error("❌ Не удалось получить userId для joinRoom")
            else:
                logger.error("❌ ws_auth не установлен")
            return

        if msg.startswith('42') and 'new-sendNewTrade' in msg:
            logger.info("⚡ WebSocket: получено событие new-sendNewTrade")
            try:
                json_start = msg.find('[')
                if json_start == -1:
                    return
                json_part = msg[json_start:]
                data = json.loads(json_part)
                payload = data[1] if len(data) > 1 else {}
                trade_id = payload.get('tradeId')
                if not trade_id:
                    html_msg = payload.get('message', '')
                    match = re.search(r'/trades/(\d+)', html_msg)
                    if match:
                        trade_id = match.group(1)
                        logger.info(f"📩 Извлекли tradeId из HTML: {trade_id}")
                    else:
                        logger.warning(f"❌ Не удалось извлечь tradeId из payload: {payload}")
                        return

                if trade_id:
                    logger.info(f"📩 Получен tradeId: {trade_id}")
                    if trade_id in processed_trades:
                        logger.info(f"ℹ️ Обмен {trade_id} уже обработан, игнорируем")
                        return

                    logger.info(f"➕ Добавляем {trade_id} в processed_trades")
                    processed_trades.add(trade_id)
                    save_processed_trades()

                    if ws_auth is None or ws_chat_id is None:
                        logger.error("❌ ws_auth или ws_chat_id не установлены! Обработка невозможна.")
                        return

                    logger.info(f"🔄 Запускаем process_trade для {trade_id}")
                    process_trade(trade_id, ws_auth, ws_chat_id)
                    logger.info(f"✅ process_trade завершён для {trade_id}")
                else:
                    logger.warning(f"❌ Не удалось извлечь tradeId из payload: {payload}")
            except json.JSONDecodeError as e:
                logger.error(f"❌ Ошибка парсинга JSON в WS: {e} | message: {msg[:200]}")
            except Exception as e:
                logger.error(f"❌ Ошибка обработки события WS: {e}", exc_info=True)
            return

        logger.debug(f"WS: получено другое сообщение: {msg[:100]}")

    except Exception as e:
        logger.error(f"❌ Ошибка в on_ws_message: {e}", exc_info=True)

def on_ws_error(ws, error):
    logger.error(f"❌ WebSocket ошибка: {error}")

def on_ws_close(ws, close_status_code, close_msg):
    global ws_connected
    ws_connected = False
    logger.warning(f"⚠️ WebSocket закрыт: {close_status_code} - {close_msg}")

# ---------- МОНИТОРИНГ ----------
def monitoring_loop(chat_id):
    global monitoring_active, current_check_interval, last_trade_time, ws_auth, ws_chat_id

    logger.info(f"[MONITOR] Запуск для чата {chat_id}")
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        monitoring_active = False
        return

    ws_auth = auth
    ws_chat_id = chat_id
    start_websocket(chat_id, auth)

    bot.send_message(chat_id, f"🔁 Мониторинг обменов запущен. WebSocket активен, резервный опрос каждые ~{CHECK_INTERVAL} сек.\nПринимаются обмены, где вы отдаёте ровно 1 карту и получаете 1 или более (1:1, 2:1, 3:1, ...).")

    # ===== НОВОЕ: keep-alive (проверка сессии каждые 2 минуты) =====
    last_keepalive = time.time()
    KEEPALIVE_INTERVAL = 120  # 2 минуты

    while monitoring_active:
        if captcha_paused:
            time.sleep(30)
            continue

        # ---- адаптация интервала (но максимум 60 сек) ----
        if last_trade_time and (time.time() - last_trade_time) > 120:
            current_check_interval = min(current_check_interval + 5, MAX_CHECK_INTERVAL)
        else:
            current_check_interval = CHECK_INTERVAL

        # ---- keep-alive (проверка сессии) ----
        if time.time() - last_keepalive > KEEPALIVE_INTERVAL:
            last_keepalive = time.time()
            if not auth.is_authenticated():
                bot.send_message(
                    chat_id,
                    "⚠️ **Сессия потеряна!**\nПожалуйста, выполните повторный вход командой /login."
                )
                monitoring_active = False
                break

        try:
            new_count = check_and_process_new_trades(auth, chat_id)
            if new_count > 0:
                last_trade_time = time.time()
                current_check_interval = CHECK_INTERVAL
        except Exception as e:
            logger.error(f"[MONITOR] Ошибка опроса: {e}")

        # ---- сон с проверкой флага ----
        for _ in range(int(current_check_interval)):
            if not monitoring_active:
                break
            time.sleep(1)

    stop_websocket()
    bot.send_message(chat_id, "🔕 Мониторинг обменов остановлен.")

# ==================== КОМАНДЫ БОТА ====================
def get_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(
        types.KeyboardButton("🔁 Мониторинг обменов"),
        types.KeyboardButton("📊 Статус"),
    )
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Бот для автоматического обмена картами на mangabuff.ru\n\n"
        "Команды:\n"
        "/login email password – войти в аккаунт\n"
        "/logout – выйти\n"
        "/status – проверить авторизацию\n"
        "/monitor_start – запустить мониторинг обменов (автопринятие, если вы отдаёте 1 карту, а получаете 1+)\n"
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
        impersonate = result.get('impersonate', 'chrome133')
        save_user_session(chat_id, user_id, result['cookies'], impersonate)
        bot.send_message(chat_id, f"✅ Успешный вход!\nВаш user_id: {user_id}\nСессия сохранена.")
        bot.send_message(chat_id, "✅ Сессия подтверждена. Можно запускать мониторинг.")
    else:
        bot.send_message(chat_id, f"❌ Ошибка входа: {result}")

@bot.message_handler(commands=['logout'])
def cmd_logout(message):
    chat_id = message.chat.id
    clear_user_session(chat_id)
    global monitoring_active
    if monitoring_active:
        monitoring_active = False
    bot.send_message(chat_id, "👋 Вы вышли. Сессия очищена.")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    auth = get_auth_for_user(chat_id)
    if auth.is_authenticated():
        user_id = auth.get_user_id()
        ws_status = "✅" if ws_connected else "❌"
        captcha_status = "⏸️ пауза" if captcha_paused else "✅ активно"
        bot.send_message(
            chat_id,
            f"🟢 Вы авторизованы\nUser ID: {user_id}\n"
            f"WebSocket: {ws_status}\n"
            f"Капча: {captcha_status}\n"
            f"Мониторинг: {'✅' if monitoring_active else '❌'}"
        )
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
    if captcha_paused:
        bot.send_message(chat_id, "⏸️ Бот на паузе из-за капчи. Сначала пройдите капчу и нажмите кнопку.")
        return
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, args=(chat_id,), daemon=True)
    monitoring_thread.start()

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

def run_bot():
    while True:
        try:
            logger.info("✅ Торговый бот запущен. Нажмите Ctrl+C для остановки.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error(f"❌ Ошибка соединения: {e}. Переподключение через 10 секунд...")
            time.sleep(10)

if __name__ == '__main__':
    run_bot()