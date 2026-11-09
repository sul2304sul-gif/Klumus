"""
Бот для мини-аппа «Кошелёк». Запускается на Railway (или любом сервере): python main.py

Переменные (в Railway: вкладка Variables):
  BOT_TOKEN  — токен от @BotFather
  ADMIN_ID   — твой Telegram ID (от @userinfobot)
  WEBAPP_URL — ссылка на приложение, например https://sul2304sul-gif.github.io/Klumus/

Открывать мини-апп нужно кнопкой «💰 Открыть кошелёк», которую бот присылает на /start.
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wallet-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])   # твой Telegram ID — сюда летят заявки
WEBAPP_URL = os.environ["WEBAPP_URL"]    # обязательно https
APP_VERSION = "5"                      # меняй при каждой заливке index.html — сбивает кэш Telegram

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- общая статистика (файл stats.json рядом с ботом) ----------
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")
EMPTY_STATS = {
    "dep_usdt": 0.0, "dep_count": 0,
    "cashout_rub": 0.0, "cashout_usdt": 0.0, "cashout_count": 0,
    "withdraw_usdt": 0.0, "withdraw_count": 0,
}


def load_stats() -> dict:
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {**EMPTY_STATS, **data}


def save_stats(s: dict) -> None:
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATS_FILE)  # атомарно, чтобы файл не побился при сбое


def to_num(v) -> float:
    try:
        n = float(str(v).replace(",", ".").replace(" ", ""))
        return n if n > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def stats_text(s: dict) -> str:
    return (
        "📊 Общая статистика\n\n"
        f"Внесено всего: {s['dep_usdt']:.2f} USDT ({s['dep_count']} пополн.)\n"
        f"Выведено рублями: {s['cashout_rub']:,.0f} ₽ — {s['cashout_usdt']:.2f} USDT ({s['cashout_count']} сделок)\n"
        f"Выведено в USDT: {s['withdraw_usdt']:.2f} USDT ({s['withdraw_count']} заявок)\n\n"
        "Суммы — со слов клиентов. Сверяй с реальными поступлениями на Bybit."
    ).replace(",", " ")


def webapp_url_for(user) -> str:
    """Вшиваем в ссылку, кто открывает приложение: с кнопки клавиатуры Telegram сам это не передаёт."""
    params = {
        "uid": user.id,
        "fn": (user.first_name or "")[:64],
        "un": user.username or "",
        "adm": 1 if user.id == ADMIN_ID else 0,
        "v": APP_VERSION,  # новая ссылка = Telegram не берёт старую страницу из кэша
    }
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + urlencode(params)


async def notify_admin(text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:  # например, админ ещё не нажал /start у бота
        log.error("Не удалось отправить уведомление админу: %s", e)


@dp.message(CommandStart())
async def start(m: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="💰 Открыть кошелёк", web_app=WebAppInfo(url=webapp_url_for(m.from_user)))]],
        resize_keyboard=True,
        is_persistent=True,
    )
    await m.answer("Жмите кнопку «💰 Открыть кошелёк» под полем ввода 👇", reply_markup=kb)


@dp.message(Command("stats"))
async def stats_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(stats_text(load_stats()))


@dp.message(F.web_app_data)
async def on_webapp(m: Message):
    try:
        d = json.loads(m.web_app_data.data)
        if not isinstance(d, dict):
            return
    except json.JSONDecodeError:
        return

    u = m.from_user
    who = f"{u.full_name} (@{u.username or '—'}, id {u.id})"
    t = d.get("type")

    if t == "deposit":
        amount = to_num(d.get("amount"))
        s = load_stats()
        s["dep_usdt"] += amount
        s["dep_count"] += 1
        save_stats(s)
        await m.answer(f"✅ Заявка на пополнение {amount:.2f} USDT принята. Проверим транзакцию и подтвердим.")
        await notify_admin(
            "🔔 Новое пополнение\n"
            f"От: {who}\n"
            f"Сеть: {d.get('coin')} / {d.get('chain')}\n"
            f"Сумма: {amount:.2f} USDT\n"
            f"TXID: {d.get('tx')}"
        )

    elif t == "withdraw":
        amount = to_num(d.get("amount"))
        s = load_stats()
        s["withdraw_usdt"] += amount
        s["withdraw_count"] += 1
        save_stats(s)
        await m.answer(f"✅ Заявка на вывод {amount:.2f} USDT принята. Оператор обработает её вручную.")
        await notify_admin(
            "💸 Заявка на вывод USDT\n"
            f"От: {who}\n"
            f"Сеть: {d.get('coin')} / {d.get('chain')}\n"
            f"Сумма: {amount:.2f} USDT\n"
            f"Адрес: {d.get('addr')}"
        )

    elif t == "cashout":
        amount = to_num(d.get("amount"))
        rub = to_num(d.get("rub"))
        s = load_stats()
        s["cashout_rub"] += rub
        s["cashout_usdt"] += amount
        s["cashout_count"] += 1
        save_stats(s)
        await m.answer(f"✅ Заявка на вывод {rub:.0f} ₽ принята. Переведём по СБП, когда оператор освободится.")
        await notify_admin(
            "🏦 Заявка на вывод в рублях\n"
            f"От: {who}\n"
            f"Сумма: {amount:.2f} USDT → {rub:.0f} ₽ (курс {d.get('rate')})\n"
            f"Банк: {d.get('bank') or '—'}\n"
            f"Телефон (СБП): {d.get('phone') or '—'}\n"
            f"Карта: {d.get('card') or '—'}\n"
            f"ФИО: {d.get('fio') or '—'}"
        )

    elif t == "stats":
        if u.id == ADMIN_ID:
            await m.answer(stats_text(load_stats()))


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
