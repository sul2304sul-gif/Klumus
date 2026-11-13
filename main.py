"""
Бот для мини-аппа «Кошелёк». Запускается на Railway (или любом сервере): python main.py

Переменные (в Railway: вкладка Variables):
  BOT_TOKEN  — токен от @BotFather
  ADMIN_ID   — твой Telegram ID (от @userinfobot)
  WEBAPP_URL — ссылка на приложение, например https://sul2304sul-gif.github.io/Klumus/
  RUB_RATE   — (необязательно) курс для рекламы, по умолчанию 98
  AD_HOURS   — (необязательно) как часто слать рекламу, в часах, по умолчанию 24. 0 — выключить

Открывать мини-апп нужно кнопкой «💰 Открыть кошелёк», которую бот присылает на /start.
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wallet-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])   # твой Telegram ID — сюда летят заявки
WEBAPP_URL = os.environ["WEBAPP_URL"]    # обязательно https
RUB_RATE = os.environ.get("RUB_RATE", "98")
AD_HOURS = float(os.environ.get("AD_HOURS", "24") or 0)
APP_VERSION = "5"                      # меняй при каждой заливке index.html — сбивает кэш Telegram

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- где хранить данные ----------
# Если в Railway подключён Volume, Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH —
# тогда список пользователей и статистика не стираются при обновлении кода.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- общая статистика ----------
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
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



# ---------- пользователи (для рекламной рассылки) ----------
USERS_FILE = os.path.join(DATA_DIR, "users.json")


def load_users() -> dict:
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users(u: dict) -> None:
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


def remember_user(user) -> None:
    u = load_users()
    rec = u.get(str(user.id), {"ads": True})
    rec["name"] = user.full_name
    u[str(user.id)] = rec
    save_users(u)


def wallet_keyboard(user) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="💰 Открыть кошелёк", web_app=WebAppInfo(url=webapp_url_for(user)))]],
        resize_keyboard=True,
        is_persistent=True,
    )


AD_TEXT = (
    "💱 <b>Обмен USDT → рубли</b>\n\n"
    "Курс сегодня: <b>1 USDT = {rate} ₽</b>\n"
    "Выплата на карту по СБП. Без переписок — просто оставьте заявку в кошельке.\n\n"
    "Хотите обменять?"
)
AD_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Обменять", callback_data="ad:go"),
     InlineKeyboardButton(text="Не сейчас", callback_data="ad:skip")],
    [InlineKeyboardButton(text="🔕 Не присылать такие сообщения", callback_data="ad:off")],
])


async def send_ad(chat_id: int) -> bool:
    """Шлёт рекламу одному человеку. False — если бот заблокирован."""
    try:
        await bot.send_message(chat_id, AD_TEXT.format(rate=RUB_RATE), reply_markup=AD_KB, parse_mode="HTML")
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await send_ad(chat_id)
    except TelegramForbiddenError:
        return False
    except Exception as e:
        log.warning("Реклама не ушла %s: %s", chat_id, e)
        return True


async def broadcast_ads() -> int:
    users = load_users()
    sent = 0
    for uid, rec in list(users.items()):
        if not rec.get("ads", True) or int(uid) == ADMIN_ID:
            continue
        if await send_ad(int(uid)):
            sent += 1
        else:
            rec["ads"] = False  # заблокировал бота — больше не шлём
        await asyncio.sleep(0.07)  # не больше ~15 сообщений в секунду
    save_users(users)
    return sent


async def ads_loop():
    if AD_HOURS <= 0:
        log.info("Рекламная рассылка выключена (AD_HOURS=0)")
        return
    while True:
        await asyncio.sleep(AD_HOURS * 3600)
        try:
            n = await broadcast_ads()
            log.info("Реклама разослана: %s чел.", n)
        except Exception as e:
            log.error("Ошибка рассылки: %s", e)


@dp.callback_query(F.data.startswith("ad:"))
async def on_ad_button(c: CallbackQuery):
    action = c.data.split(":", 1)[1]
    if action == "go":
        await c.message.edit_reply_markup(reply_markup=None)
        await c.message.answer(
            "Нажмите <b>💰 Открыть кошелёк</b> под полем ввода → вкладка <b>Реквизиты</b> → "
            "укажите сумму, банк и телефон.",
            reply_markup=wallet_keyboard(c.from_user), parse_mode="HTML",
        )
    elif action == "skip":
        await c.message.delete()
    elif action == "off":
        u = load_users()
        rec = u.get(str(c.from_user.id), {})
        rec["ads"] = False
        u[str(c.from_user.id)] = rec
        save_users(u)
        await c.message.edit_text("🔕 Больше не будем присылать предложения. Включить обратно: /ads_on")
    await c.answer()


@dp.message(Command("ads_on"))
async def ads_on(m: Message):
    u = load_users()
    rec = u.get(str(m.from_user.id), {})
    rec["ads"] = True
    u[str(m.from_user.id)] = rec
    save_users(u)
    await m.answer("🔔 Предложения обмена снова включены.")


@dp.message(Command("ad_test"))
async def ad_test(m: Message):
    """Админ: посмотреть, как выглядит реклама."""
    if m.from_user.id == ADMIN_ID:
        await send_ad(m.chat.id)


@dp.message(Command("ad_send"))
async def ad_send_now(m: Message):
    """Админ: разослать рекламу всем прямо сейчас."""
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("Рассылаю…")
    n = await broadcast_ads()
    await m.answer(f"Готово, отправлено: {n} чел.")



# ---------- рассылка твоих сообщений: /post ----------
POST_WAIT = set()      # админ нажал /post и сейчас пришлёт сообщение
POST_DRAFT = {}        # chat_id -> message_id черновика

POST_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📤 Отправить всем", callback_data="post:send"),
     InlineKeyboardButton(text="✖️ Отмена", callback_data="post:cancel")],
])


@dp.message(Command("post"))
async def post_start(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    POST_WAIT.add(m.chat.id)
    await m.answer("✍️ Пришли сообщение для рассылки: текст, фото, видео — что угодно.\nОтмена: /cancel")


@dp.message(Command("cancel"))
async def post_cancel_cmd(m: Message):
    POST_WAIT.discard(m.chat.id)
    POST_DRAFT.pop(m.chat.id, None)
    await m.answer("Отменено.")


@dp.message(F.from_user.id == ADMIN_ID, F.func(lambda m: m.chat.id in POST_WAIT and not (m.text or "").startswith("/")))
async def post_receive(m: Message):
    POST_WAIT.discard(m.chat.id)
    POST_DRAFT[m.chat.id] = m.message_id
    n = sum(1 for uid in load_users() if int(uid) != ADMIN_ID)
    await m.answer(f"Так это увидят люди ☝️\nПолучателей: {n}. Отправить?", reply_markup=POST_KB)


@dp.callback_query(F.data.startswith("post:"))
async def post_button(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer()
    action = c.data.split(":", 1)[1]
    msg_id = POST_DRAFT.pop(c.message.chat.id, None)
    if action == "cancel" or msg_id is None:
        await c.message.edit_text("Рассылка отменена.")
        return await c.answer()
    await c.message.edit_text("📤 Рассылаю…")
    await c.answer()
    users = load_users()
    ok = fail = 0
    for uid in list(users):
        if int(uid) == ADMIN_ID:
            continue
        try:
            await bot.copy_message(int(uid), c.message.chat.id, msg_id)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(int(uid), c.message.chat.id, msg_id); ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.07)
    await c.message.edit_text(f"✅ Рассылка готова: доставлено {ok}, не доставлено {fail} (заблокировали бота).")


@dp.message(CommandStart())
async def start(m: Message):
    remember_user(m.from_user)
    await m.answer("Жмите кнопку «💰 Открыть кошелёк» под полем ввода 👇", reply_markup=wallet_keyboard(m.from_user))


@dp.message(Command("stats"))
async def stats_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    users = load_users()
    active = sum(1 for k, v in users.items() if v.get("ads", True) and int(k) != ADMIN_ID)
    disk = "постоянный диск ✓" if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") else "⚠️ без постоянного диска — список сотрётся при обновлении"
    await m.answer(stats_text(load_stats()) + f"\n\n👥 Пользователей: {len(users)} (получают рекламу: {active})\n💾 Хранение: {disk}")


@dp.message(F.web_app_data)
async def on_webapp(m: Message):
    try:
        d = json.loads(m.web_app_data.data)
        if not isinstance(d, dict):
            return
    except json.JSONDecodeError:
        return

    u = m.from_user
    remember_user(u)
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
    asyncio.create_task(ads_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
