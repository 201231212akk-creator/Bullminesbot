import os
import json
import random
import sqlite3
import logging
import asyncio
import string
import secrets
import aiohttp
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from uvicorn import Config, Server
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ============================================================
#  ⚙️  НАСТРОЙКИ
# ============================================================

BOT_TOKEN = "8669805818:AAHz-_JGS0nH34SRdCIROqywv-RoHKLvU6c"
ADMIN_ID = 2062809093
WEBAPP_URL = "https://profound-gingersnap-3e19b9.netlify.app"
API_URL = "https://bullminesbot-production.up.railway.app"
DB_NAME = "bullmines.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BullMinesSecure")

ITEMS_DATABASE = {
    "ring_rainbow": {"name": "Радужный спектр", "type": "Обводка", "rarity": "Secret", "effect": "+1.25х"},
    "color_rainbow": {"name": "Радужный ник", "type": "Цвет", "rarity": "Secret", "effect": "+1.25х"},
    "hat_crown": {"name": "Корона", "type": "Шляпа", "rarity": "Secret", "effect": "+1.25х", "icon": "👑"}
}

def generate_unique_id():
    alphabet = string.ascii_uppercase + string.digits
    return "BM-" + "".join(secrets.choice(alphabet) for _ in range(8))

def setup_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance REAL DEFAULT 10000.0,
        balance_mc REAL DEFAULT 0.0,
        inventory TEXT DEFAULT '[]',
        equipped_border TEXT DEFAULT 'none',
        equipped_color TEXT DEFAULT 'none',
        equipped_head TEXT DEFAULT 'none',
        banned INTEGER DEFAULT 0,
        muted INTEGER DEFAULT 0,
        tg_avatar TEXT DEFAULT '',
        unique_id TEXT UNIQUE,
        premium INTEGER DEFAULT 0,
        premium_banner TEXT DEFAULT 'none',
        premium_until TEXT DEFAULT '',
        total_won REAL DEFAULT 0.0,
        last_seen REAL DEFAULT 0
    )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS admin_security (user_id INTEGER PRIMARY KEY, failed_attempts INTEGER DEFAULT 0, is_blocked INTEGER DEFAULT 0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS shop_stock (item_id TEXT PRIMARY KEY, quantity INTEGER DEFAULT 7)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS duels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT, creator_id INTEGER, opponent_id INTEGER,
        bet REAL, currency TEXT DEFAULT 'bc',
        status TEXT DEFAULT 'waiting', state TEXT DEFAULT '{}', created_at TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS arena (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT DEFAULT 'waiting', state TEXT DEFAULT '{}',
        timer_end REAL DEFAULT 0, currency TEXT DEFAULT 'bc', created_at TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS feed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, avatar TEXT,
        game TEXT, amount REAL, created_at REAL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version TEXT, title TEXT, body TEXT,
        created_at REAL, is_new INTEGER DEFAULT 1
    )
    """)

    configs = [
        ("admin_abuse_active", "0"), ("admin_news_ticker", ""),
        ("event_mult_end", "0"), ("event_mult_value", "1"),
        ("event_coin_end", "0"), ("event_god_end", "0"), ("event_fire_end", "0"),
        ("event_freeze_end", "0"), ("event_anon_end", "0"), ("event_color_chaos_end", "0"),
        ("event_speed_end", "0"), ("event_roulette_end", "0"),
        ("event_crown_end", "0"), ("event_crown_prize", "0"),
        ("event_crown_start", "0"), ("event_coin_collect_start", "0"),
        ("next_restock_time", str(datetime.now() + timedelta(minutes=5))),
    ]
    for key, val in configs:
        cursor.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)", (key, val))

    for item_id in ITEMS_DATABASE.keys():
        cursor.execute("INSERT OR IGNORE INTO shop_stock (item_id, quantity) VALUES (?, 7)", (item_id,))

    cursor.execute("SELECT COUNT(*) FROM updates")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO updates (version, title, body, created_at, is_new) VALUES (?, ?, ?, ?, 1)",
            ("1.0.0", "Первый релиз", "• Запуск BullMines\n• Игры: Crash, Mines, Coin, Tower, Upgrader\n• Дуэли и Арена\n• Магазин с 3 предметами", time.time())
        )
        cursor.execute(
            "INSERT INTO updates (version, title, body, created_at, is_new) VALUES (?, ?, ?, ?, 1)",
            ("1.0.1", "Фиксы и новинки", "• Добавлена лента\n• Обновлен дизайн\n• Улучшена стабильность", time.time())
        )

    conn.commit()
    conn.close()

setup_database()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.user_connections: Dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: int = 0):
        await websocket.accept()
        self.active_connections.add(websocket)
        if user_id:
            self.user_connections[user_id] = websocket

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        for uid, ws in list(self.user_connections.items()):
            if ws == websocket:
                del self.user_connections[uid]
                break

    async def broadcast(self, message: dict):
        dead = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for d in dead:
            self.active_connections.discard(d)

    def count(self):
        return len(self.active_connections)

ws_manager = ConnectionManager()

poll_online = {}   

def get_online_count(currency_filter=None):
    now = time.time()
    cnt = 0
    for uid, data in poll_online.items():
        if now - data.get("last_seen", 0) > 15:
            continue
        if currency_filter and data.get("currency", "bc") != currency_filter:
            continue
        cnt += 1
    return cnt

def get_online_by_game(currency_filter=None):
    now = time.time()
    games = {"crash": 0, "mines": 0, "coin": 0, "tower": 0, "slider": 0, "duels": 0, "arena": 0}
    for uid, data in poll_online.items():
        if now - data.get("last_seen", 0) > 15:
            continue
        if currency_filter and data.get("currency", "bc") != currency_filter:
            continue
        page = data.get("page", "")
        for g in games:
            if page == "game-" + g:
                games[g] += 1
    return games

async def poll_online_cleaner():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        to_remove = [uid for uid, data in poll_online.items() if now - data.get("last_seen", 0) > 30]
        for uid in to_remove:
            del poll_online[uid]

def get_config(key: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def set_config(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE system_config SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str = "Игрок"):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT unique_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        uid = generate_unique_id()
        cursor.execute("INSERT INTO users (user_id, username, unique_id) VALUES (?, ?, ?)", (user_id, username, uid))
        conn.commit()
    conn.close()

def add_feed(user_id, username, avatar, game, amount):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO feed (user_id, username, avatar, game, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, avatar or "", game, amount, time.time())
        )
        cursor.execute("DELETE FROM feed WHERE id NOT IN (SELECT id FROM feed ORDER BY id DESC LIMIT 30)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"feed error: {e}")

@app.get("/api/sync")
def api_sync(user_id: int = 0, currency: str = "bc"):
    if user_id:
        if user_id in poll_online:
            poll_online[user_id]["last_seen"] = time.time()
            poll_online[user_id]["currency"] = currency
        else:
            poll_online[user_id] = {"page": "", "last_seen": time.time(), "currency": currency}
    online_count = get_online_count(currency)
    games_online = get_online_by_game(currency)
    return {
        "abuse_active": get_config("admin_abuse_active") == "1",
        "news": get_config("admin_news_ticker"),
        "online": online_count,
        "games_online": games_online,
        "events": {
            "mult": {"end": float(get_config("event_mult_end") or 0), "value": float(get_config("event_mult_value") or 1)},
            "coins": {"end": float(get_config("event_coin_end") or 0)},
            "god": {"end": float(get_config("event_god_end") or 0)},
            "fire": {"end": float(get_config("event_fire_end") or 0)},
            "freeze": {"end": float(get_config("event_freeze_end") or 0)},
            "anon": {"end": float(get_config("event_anon_end") or 0)},
            "chaos": {"end": float(get_config("event_color_chaos_end") or 0)},
            "speed": {"end": float(get_config("event_speed_end") or 0)},
            "roulette": {"end": float(get_config("event_roulette_end") or 0)},
            "crown": {"end": float(get_config("event_crown_end") or 0)},
        }
    }

@app.post("/api/page_change")
def api_page_change(user_id: int, page: str, currency: str = "bc"):
    if user_id:
        if user_id in poll_online:
            poll_online[user_id]["page"] = page
            poll_online[user_id]["last_seen"] = time.time()
            poll_online[user_id]["currency"] = currency
        else:
            poll_online[user_id] = {"page": page, "last_seen": time.time(), "currency": currency}
    return {"status": "ok"}

@app.get("/api/admin_here")
def api_admin_here():
    now = time.time()
    if ADMIN_ID in poll_online and now - poll_online[ADMIN_ID].get("last_seen", 0) < 20:
        return {"page": poll_online[ADMIN_ID].get("page", ""), "user_id": ADMIN_ID, "active": True}
    return {"page": "", "user_id": 0, "active": False}

@app.get("/api/feed")
def api_feed():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, avatar, game, amount FROM feed ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    conn.close()
    return {"items": [{"user_id": r[0], "username": r[1], "avatar": r[2], "game": r[3], "amount": int(r[4])} for r in rows]}

@app.get("/api/updates")
def api_updates():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, version, title, body, created_at, is_new FROM updates ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    return {"items": [{"id": r[0], "version": r[1], "title": r[2], "body": r[3], "created_at": r[4], "is_new": bool(r[5])} for r in rows]}

@app.post("/api/updates/read")
def api_updates_read():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE updates SET is_new = 0")
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/updates/add")
def api_updates_add(user_id: int, version: str, title: str, body: str):
    if user_id != ADMIN_ID:
        return {"status": "denied"}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO updates (version, title, body, created_at, is_new) VALUES (?, ?, ?, ?, 1)",
        (version, title, body, time.time())
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket, user_id: int = 0):
    await ws_manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

@app.get("/api/user/{user_id}")
def user_info(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT username, balance, balance_mc, inventory, banned, tg_avatar, unique_id,
               premium, premium_banner, equipped_border, equipped_color, equipped_head, total_won
        FROM users WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"error": "not_found"}
    return {
        "username": row[0], "balance": int(row[1]), "balance_mc": int(row[2] or 0),
        "inventory": json.loads(row[3]),
        "banned": bool(row[4]), "avatar": row[5], "unique_id": row[6],
        "premium": bool(row[7]), "premium_banner": row[8],
        "equipped_border": row[9], "equipped_color": row[10], "equipped_head": row[11],
        "total_won": int(row[12] or 0)
    }

@app.post("/api/user/equip")
def user_equip(user_id: int, category: str, item_id: str):
    if category not in ["border", "color", "head"]:
        return {"status": "error"}
    col = {"border": "equipped_border", "color": "equipped_color", "head": "equipped_head"}[category]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE users SET {col} = ? WHERE user_id = ?", (item_id, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/user/search/{unique_id}")
def search_user(unique_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, balance, tg_avatar, premium, premium_banner FROM users WHERE unique_id = ?", (unique_id.upper(),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"error": "not_found"}
    return {"user_id": row[0], "username": row[1], "balance": int(row[2]), "avatar": row[3], "premium": bool(row[4]), "premium_banner": row[5]}

@app.get("/api/shop")
def get_shop():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT item_id, quantity FROM shop_stock")
    stocks = dict(cursor.fetchall())
    conn.close()
    return {"items": ITEMS_DATABASE, "stock": stocks, "restock": get_config("next_restock_time")}

@app.post("/api/shop/buy")
async def shop_buy(user_id: int, item_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT balance, inventory FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": "Юзер не найден"}
    balance, inventory = row
    inv = json.loads(inventory)
    if any(x.get("id") == item_id for x in inv):
        conn.close()
        return {"status": "error", "message": "Уже куплено"}
    cursor.execute("SELECT quantity FROM shop_stock WHERE item_id = ?", (item_id,))
    stock_row = cursor.fetchone()
    if not stock_row or stock_row[0] <= 0:
        conn.close()
        return {"status": "error", "message": "Распродано"}
    item = ITEMS_DATABASE.get(item_id)
    if not item:
        conn.close()
        return {"status": "error", "message": "Нет предмета"}
    price = 150000000
    if balance < price:
        conn.close()
        return {"status": "error", "message": "Мало B'c"}
    inv.append({"id": item_id, "name": item["name"], "category": item["type"]})
    cursor.execute("UPDATE users SET balance = balance - ?, inventory = ? WHERE user_id = ?", (price, json.dumps(inv, ensure_ascii=False), user_id))
    cursor.execute("UPDATE shop_stock SET quantity = quantity - 1 WHERE item_id = ?", (item_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/admin/toggle_abuse")
async def toggle_abuse(user_id: int, active: bool):
    if user_id != ADMIN_ID:
        return {"status": "denied"}
    set_config("admin_abuse_active", "1" if active else "0")
    return {"status": "ok"}

@app.post("/api/admin/broadcast")
async def admin_broadcast(user_id: int, text: str, seconds: int = 7):
    if user_id != ADMIN_ID:
        return {"status": "denied"}
    set_config("admin_news_ticker", text)
    return {"status": "ok"}

@app.post("/api/admin/give_all")
async def give_all(user_id: int, amount: float, currency: str = "bc"):
    if user_id != ADMIN_ID:
        return {"status": "denied"}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if currency == "mc":
        cursor.execute("UPDATE users SET balance_mc = balance_mc + ?", (amount,))
    else:
        cursor.execute("UPDATE users SET balance = balance + ?", (amount,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

def _event_end_time(seconds: int):
    return time.time() + seconds

@app.post("/api/admin/event/multiplier")
async def ev_mult(user_id: int, mult: float, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_mult_end", str(_event_end_time(seconds)))
    set_config("event_mult_value", str(mult))
    return {"status": "ok"}

@app.post("/api/admin/event/coins")
async def ev_coins(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_coin_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/god")
async def ev_god(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_god_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/fire")
async def ev_fire(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_fire_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/freeze")
async def ev_freeze(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_freeze_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/anon")
async def ev_anon(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_anon_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/chaos")
async def ev_chaos(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_color_chaos_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/speed")
async def ev_speed(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_speed_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/roulette")
async def ev_roulette(user_id: int, seconds: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_roulette_end", str(_event_end_time(seconds)))
    return {"status": "ok"}

@app.post("/api/admin/event/crown")
async def ev_crown(user_id: int, seconds: int, prize: int = 1000000):
    if user_id != ADMIN_ID: return {"status": "denied"}
    set_config("event_crown_end", str(_event_end_time(seconds)))
    set_config("event_crown_prize", str(prize))
    set_config("event_crown_start", str(time.time()))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET total_won = 0")
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/admin/event/jackpot")
async def ev_jackpot(user_id: int, target_id: int = 0):
    if user_id != ADMIN_ID: return {"status": "denied"}
    prize = random.randint(1000000, 3000000)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if target_id:
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (target_id,))
        row = cursor.fetchone()
        winner_name = row[0] if row else f"ID {target_id}"
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (prize, target_id))
    else:
        cursor.execute("SELECT user_id, username FROM users ORDER BY RANDOM() LIMIT 1")
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {"status": "no_players"}
        target_id, winner_name = row
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (prize, target_id))
    conn.commit()
    conn.close()
    add_feed(target_id, winner_name, "", "JACKPOT", prize)
    return {"status": "ok", "winner_name": winner_name, "prize": prize}

@app.post("/api/admin/event/stop")
async def ev_stop(user_id: int, event: str):
    if user_id != ADMIN_ID: return {"status": "denied"}
    valid = ["mult", "coin", "god", "fire", "freeze", "anon", "color_chaos", "speed", "roulette", "crown"]
    if event not in valid: return {"status": "invalid"}
    set_config(f"event_{event}_end", "0")
    return {"status": "ok"}

@app.post("/api/admin/restock")
async def admin_restock(user_id: int):
    if user_id != ADMIN_ID: return {"status": "denied"}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for item_id in ITEMS_DATABASE.keys():
        cursor.execute("UPDATE shop_stock SET quantity = 7 WHERE item_id = ?", (item_id,))
    conn.commit()
    conn.close()
    set_config("next_restock_time", (datetime.now() + timedelta(minutes=5)).isoformat())
    return {"status": "ok"}

@app.post("/api/game/win")
def game_win(user_id: int, game: str, amount: int, currency: str = "bc"):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, tg_avatar FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"status": "error"}
    username, avatar = row
    if currency == "bc":
        cursor.execute("UPDATE users SET total_won = total_won + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()
    if amount >= 100000:
        add_feed(user_id, username, avatar, game, amount)
    return {"status": "ok"}

@app.post("/api/user/{user_id}/avatar")
def set_avatar(user_id: int, url: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET tg_avatar = ? WHERE user_id = ?", (url, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/admin/verify")
def verify_admin_panel(user_id: int, pin: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT failed_attempts, is_blocked FROM admin_security WHERE user_id = ?", (user_id,))
    sec = cursor.fetchone()
    if not sec:
        cursor.execute("INSERT INTO admin_security (user_id) VALUES (?)", (user_id,))
        conn.commit()
        attempts, blocked = 0, 0
    else:
        attempts, blocked = sec
    if blocked == 1:
        conn.close()
        return {"status": "blocked", "message": "Заблокировано!"}
    if pin == "201231212":
        cursor.execute("UPDATE admin_security SET failed_attempts = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"status": "granted"}
    else:
        attempts += 1
        is_now_blocked = 1 if attempts >= 3 else 0
        cursor.execute("UPDATE admin_security SET failed_attempts = ?, is_blocked = ? WHERE user_id = ?", (attempts, is_now_blocked, user_id))
        conn.commit()
        conn.close()
        return {"status": "blocked" if is_now_blocked else "denied", "remaining": 3 - attempts}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class CheckBanMiddleware:
    async def __call__(self, handler, event, data):
        if not hasattr(event, 'from_user') or event.from_user is None:
            return await handler(event, data)
        user_id = event.from_user.id
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] == 1:
            return
        return await handler(event, data)

dp.message.middleware(CheckBanMiddleware())

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Игрок"
    get_or_create_user(user_id, username)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT unique_id FROM users WHERE user_id = ?", (user_id,))
    uid_row = cursor.fetchone()
    conn.close()
    unique_id = uid_row[0] if uid_row else "—"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Открыть BullMines", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="💱 Обмен валюты", callback_data="exchange_menu")]
    ])
    await message.reply(
        f"👋 Привет, везунчик!\n\n🆔 Твой ID: <code>{unique_id}</code>\n"
        f"Отправь его другу, чтобы тебя нашли.\n\nЖми играть 🚀",
        parse_mode="HTML", reply_markup=keyboard
    )

@dp.message(Command("id"))
async def id_cmd(message: types.Message):
    user_id = message.from_user.id
    get_or_create_user(user_id, message.from_user.username or "Игрок")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT unique_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        await message.reply(f"🆔 Твой ID: <code>{row[0]}</code>", parse_mode="HTML")

@dp.message(Command("resetattempts"))
async def reset_attempts(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    target = command.args
    if not target: return await message.reply("Напишите ID.")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE admin_security SET failed_attempts = 0, is_blocked = 0 WHERE user_id = ?", (int(target),))
    conn.commit()
    conn.close()
    await message.reply(f"🔒 Ошибки входа для {target} стёрты.")

@dp.callback_query(F.data == "exchange_menu")
async def exchange_menu(callback: types.CallbackQuery):
    text = (
        "💱 <b>ОБМЕН ВАЛЮТЫ</b>\n\n"
        "Обмен валюты временно недоступен. Мы работаем над этим!"
    )
    await callback.message.reply(text, parse_mode="HTML")
    await callback.answer()

async def run_server_and_bot():
    config = Config(app=app, host="0.0.0.0", port=8000, log_level="info")
    server = Server(config)

    async def scheduler():
        while True:
            await asyncio.sleep(5)
            now_ts = time.time()
            try:
                target = get_config("next_restock_time")
                target_dt = datetime.fromisoformat(target)
                if datetime.now() >= target_dt:
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    for item_id in ITEMS_DATABASE.keys():
                        cursor.execute("UPDATE shop_stock SET quantity = 7 WHERE item_id = ?", (item_id,))
                    conn.commit()
                    conn.close()
                    set_config("next_restock_time", (datetime.now() + timedelta(minutes=5)).isoformat())
            except Exception as e:
                logger.error(f"Restock error: {e}")
            for key in ["mult", "coin", "god", "fire", "freeze", "anon", "color_chaos", "speed", "roulette", "crown"]:
                end = float(get_config(f"event_{key}_end") or 0)
                if end > 0 and end < now_ts:
                    set_config(f"event_{key}_end", "0")
            try:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("SELECT id, status, state, timer_end, currency FROM arena ORDER BY id DESC LIMIT 1")
                arow = cursor.fetchone()
                if arow and arow[1] == "waiting" and arow[3] and arow[3] < now_ts:
                    state = json.loads(arow[2]) if arow[2] else {}
                    players = state.get("players", [])
                    if len(players) >= 2:
                        cards = []
                        for p in players:
                            weight = max(1, int(p["bet"] / 100))
                            cards.extend([p["user_id"]] * weight)
                        winner_id = random.choice(cards)
                        total = sum(p["bet"] for p in players)
                        if arow[4] == "mc":
                            cursor.execute("UPDATE users SET balance_mc = balance_mc + ? WHERE user_id = ?", (total, winner_id))
                        else:
                            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total, winner_id))
                        cursor.execute("SELECT username, tg_avatar FROM users WHERE user_id = ?", (winner_id,))
                        w = cursor.fetchone()
                        state["winner"] = winner_id
                        state["winner_name"] = w[0] if w else "?"
                        state["winner_avatar"] = w[1] if w else ""
                        state["prize"] = int(total)
                        state["cards"] = cards
                        cursor.execute("UPDATE arena SET status = 'finished', state = ? WHERE id = ?", (json.dumps(state), arow[0]))
                        if w:
                            add_feed(winner_id, w[0], w[1], "ARENA", total)
                    else:
                        for p in players:
                            if arow[4] == "mc":
                                cursor.execute("UPDATE users SET balance_mc = balance_mc + ? WHERE user_id = ?", (p["bet"], p["user_id"]))
                            else:
                                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (p["bet"], p["user_id"]))
                        cursor.execute("UPDATE arena SET status = 'finished' WHERE id = ?", (arow[0],))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Arena spin error: {e}")
            try:
                crown_end = float(get_config("event_crown_end") or 0)
                crown_start = float(get_config("event_crown_start") or 0)
                if crown_start > 0 and crown_end > 0 and crown_end < now_ts:
                    prize = int(get_config("event_crown_prize") or 1000000)
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    cursor.execute("SELECT user_id, username, tg_avatar FROM users WHERE total_won > 0 ORDER BY total_won DESC LIMIT 1")
                    top = cursor.fetchone()
                    if top:
                        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (prize, top[0]))
                        add_feed(top[0], top[1], top[2], "CROWN", prize)
                    conn.commit()
                    conn.close()
                    set_config("event_crown_end", "0")
                    set_config("event_crown_start", "0")
            except Exception as e:
                logger.error(f"Crown error: {e}")
            try:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("SELECT id, creator_id, opponent_id, bet, currency, status FROM duels WHERE status = 'playing'")
                active_duels = cursor.fetchall()
                for d in active_duels:
                    d_id, c_id, o_id, bet, cur, st = d
                    c_active = c_id in poll_online and now_ts - poll_online[c_id].get("last_seen", 0) < 30
                    o_active = o_id in poll_online and now_ts - poll_online[o_id].get("last_seen", 0) < 30
                    if not c_active and o_active:
                        if cur == "mc":
                            cursor.execute("UPDATE users SET balance_mc = balance_mc + ? WHERE user_id = ?", (int(bet*2), o_id))
                        else:
                            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (int(bet*2), o_id))
                        cursor.execute("UPDATE duels SET status = 'finished' WHERE id = ?", (d_id,))
                    elif not o_active and c_active:
                        if cur == "mc":
                            cursor.execute("UPDATE users SET balance_mc = balance_mc + ? WHERE user_id = ?", (int(bet*2), c_id))
                        else:
                            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (int(bet*2), c_id))
                        cursor.execute("UPDATE duels SET status = 'finished' WHERE id = ?", (d_id,))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Duels cleaner error: {e}")

    loop = asyncio.get_event_loop()
    loop.create_task(scheduler())
    loop.create_task(poll_online_cleaner())
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(run_server_and_bot())
