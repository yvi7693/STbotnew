import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo, ForceReply
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from pydantic import BaseModel

import settings
from split_client import SplitClient

import math
import json
import uuid
import httpx

# локальные импорты
# (в реальном проекте разнесите по папкам)

class Store(BaseModel):
    user_price_per_star_rub: float = float(getattr(settings, "USER_PRICE_PER_STAR_RUB", 3.50))
    cost_per_star_rub: float = float(getattr(settings, "COST_PER_STAR_RUB", 3.10))

store = Store()
store.user_price_per_star_rub = 1.5

pending_qty: dict[int, int] = {}
ask_custom: dict[int, bool] = {}
rub_balance: dict[int, int] = {}# баланс в копейках (RUB*100)

# --- Персистентный баланс ---
BALANCE_FILE = getattr(settings, "BALANCE_FILE", "balances.json")

def load_balances() -> None:
    """Загружает баланс пользователей из JSON-файла в rub_balance.
    Формат: {"123": 1500, ...} — ключи как строки (user_id), значения в копейках.
    """
    import os, json as _json
    if not os.path.exists(BALANCE_FILE):
        return
    try:
        with open(BALANCE_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            rub_balance.clear()
            for k, v in data.items():
                try:
                    uid = int(k)
                    rub_balance[uid] = int(v)
                except Exception:
                    continue
    except Exception:
        # игнорируем ошибку чтения, чтобы бот всё равно запустился
        pass

def save_balances() -> None:
    """Сохраняет rub_balance в JSON-файл атомарно."""
    import json as _json, os, tempfile
    try:
        tmp_dir = os.path.dirname(BALANCE_FILE) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="balances_", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump({str(k): int(v) for k, v in rub_balance.items()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, BALANCE_FILE)
    except Exception:
        # не валим бота при ошибке записи
        pass

# --- Персистентная статистика по суммарным пополнениям ---
STATS_FILE = getattr(settings, "STATS_FILE", "stats.json")
# суммарно пополнено (в копейках)
total_deposits: dict[int, int] = {}
total_stars: dict[int, int] = {}

def load_stats() -> None:
    import os, json as _json
    if not os.path.exists(STATS_FILE):
        return
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            for k, v in (data.get("deposits") or {}).items():
                try:
                    total_deposits[int(k)] = int(v)
                except Exception:
                    pass
            for k, v in (data.get("stars") or {}).items():
                try:
                    total_stars[int(k)] = int(v)
                except Exception:
                    pass
    except Exception:
        # не валим бота при ошибке чтения
        pass

def save_stats() -> None:
    import json as _json, os, tempfile
    try:
        payload = {
            "deposits": {str(k): int(v) for k, v in total_deposits.items()},
            "stars":    {str(k): int(v) for k, v in total_stars.items()},
        }
        tmp_dir = os.path.dirname(STATS_FILE) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="stats_", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATS_FILE)
    except Exception:
        # не валим бота при ошибке записи
        pass

# ожидаемые пополнения через Crypto Pay: user_id -> {topup_id, amount_rub, invoice_id}

pending_topups: dict[int, dict] = {}

# ожидаемые заявки на покупку звёзд вручную админом: order_id -> {user_id, qty, price_kopecks, username}
pending_orders: dict[str, dict] = {}

# ожидаемые оплаты по СБП (ручное подтверждение админом): sbp_id -> {user_id, amount_rub}
pending_sbp: dict[str, dict] = {}

# ожидание ввода изменённой суммы для СБП: (chat_id, admin_id) -> sbp_id
sbp_change_wait: dict[tuple[int, int], str] = {}

# использованные коды (чтобы не повторялись за время работы бота)
used_sbp_ids: set[str] = set()
used_order_ids: set[str] = set()

# --- Персистентные очереди заявок (без истечения срока) ---
PENDING_ORDERS_FILE = getattr(settings, "PENDING_ORDERS_FILE", "pending_orders.json")
PENDING_SBP_FILE = getattr(settings, "PENDING_SBP_FILE", "pending_sbp.json")

def _atomic_dump_json(path: str, payload: dict) -> None:
    import json as _json, os, tempfile
    try:
        tmp_dir = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="tmp_", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        # не валим бота при ошибке записи
        pass

def load_pending() -> None:
    """Загружаем очереди pending_orders и pending_sbp из файлов.
    Нужна для подтверждения заявок в любое время, даже после рестартов бота.
    """
    import os, json as _json
    # orders
    if os.path.exists(PENDING_ORDERS_FILE):
        try:
            with open(PENDING_ORDERS_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, dict):
                pending_orders.clear()
                for k, v in data.items():
                    if isinstance(v, dict):
                        pending_orders[str(k)] = {
                            "user_id": int(v.get("user_id", 0)),
                            "qty": int(v.get("qty", 0)),
                            "price_kopecks": int(v.get("price_kopecks", 0)),
                            "username": str(v.get("username", "")),
                        }
        except Exception:
            pass
    # sbp
    if os.path.exists(PENDING_SBP_FILE):
        try:
            with open(PENDING_SBP_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, dict):
                pending_sbp.clear()
                for k, v in data.items():
                    if isinstance(v, dict):
                        pending_sbp[str(k)] = {
                            "user_id": int(v.get("user_id", 0)),
                            "amount_rub": int(v.get("amount_rub", 0)),
                        }
        except Exception:
            pass
    # восстановим множества использованных кодов, чтобы избежать коллизий
    try:
        used_order_ids.update(pending_orders.keys())
        used_sbp_ids.update(pending_sbp.keys())
    except Exception:
        pass

def save_pending_orders() -> None:
    _atomic_dump_json(PENDING_ORDERS_FILE, {str(k): v for k, v in pending_orders.items()})

def save_pending_sbp() -> None:
    _atomic_dump_json(PENDING_SBP_FILE, {str(k): v for k, v in pending_sbp.items()})

def _gen_unique_code(used: set[str], also_check: set[str] | dict | None = None, length: int = 12) -> str:
    """Генерирует уникальный код и проверяет, что его ещё нет в used и also_check."""
    while True:
        code = uuid.uuid4().hex[:length]
        if code in used:
            continue
        if also_check is not None:
            if isinstance(also_check, dict):
                if code in also_check:
                    continue
            elif isinstance(also_check, set):
                if code in also_check:
                    continue
        used.add(code)
        return code

def gen_sbp_id() -> str:
    return _gen_unique_code(used_sbp_ids, also_check=set(pending_sbp.keys()), length=12)

def gen_order_id() -> str:
    return _gen_unique_code(used_order_ids, also_check=set(pending_orders.keys()), length=12)

# флаг ожидания пользовательского ввода суммы пополнения в ₽
ask_custom_topup: dict[int, bool] = {}


bot = Bot(settings.BOT_TOKEN)
dp = Dispatcher()

# --- Обязательная подписка на канал ---
REQUIRED_CHANNEL = getattr(settings, "REQUIRED_CHANNEL", None)  # например: "@my_channel" или -1001234567890
REQUIRED_CHANNEL_URL = getattr(settings, "REQUIRED_CHANNEL_URL", "")  # если канал без @username, укажите URL вручную

async def _is_subscribed(user_id: int) -> bool:
    # Если канал не настроен — пропускаем
    if not REQUIRED_CHANNEL:
        return True

    chat_ref = REQUIRED_CHANNEL
    # Разрешим указать ссылку вида https://t.me/username
    try:
        if isinstance(chat_ref, str) and chat_ref.startswith("https://t.me/"):
            tail = chat_ref.split("https://t.me/", 1)[1].split("?", 1)[0].strip("/")
            # Для обычного публичного username (не инвайт-ссылки) можно конвертировать в @username
            if tail and not tail.startswith("+") and not tail.startswith("joinchat/"):
                chat_ref = tail if tail.startswith("@") else f"@{tail}"
    except Exception:
        pass

    try:
        member = await bot.get_chat_member(chat_ref, user_id)
        status = getattr(member, "status", None)
        return status in ("member", "administrator", "creator")
    except Exception:
        # Если бот не админ приватного канала или чат не найден — считаем, что не подписан
        return False

def _channel_url() -> str:
    if not REQUIRED_CHANNEL:
        return REQUIRED_CHANNEL_URL or ""
    ch = str(REQUIRED_CHANNEL)
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    # если numeric id — используем заданный URL, иначе вернуть пусто
    return REQUIRED_CHANNEL_URL or ""

# Внутренний помощник: вернуть реальное значение chat_ref, с которым идёт проверка
def _resolve_chat_ref():
    chat_ref = REQUIRED_CHANNEL
    try:
        if isinstance(chat_ref, str) and chat_ref.startswith("https://t.me/"):
            tail = chat_ref.split("https://t.me/", 1)[1].split("?", 1)[0].strip("/")
            if tail and not tail.startswith("+") and not tail.startswith("joinchat/"):
                chat_ref = tail if tail.startswith("@") else f"@{tail}"
    except Exception:
        pass
    return chat_ref

from aiogram import BaseMiddleware
from typing import Callable, Dict, Any, Awaitable

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        # если канал не настроен — пропускаем
        if not REQUIRED_CHANNEL:
            return await handler(event, data)
        # у события должен быть from_user
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        # Разрешаем обработчик проверки подписки всегда доходить (чтобы показать меню после саба)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        ok = await _is_subscribed(user.id)
        if ok:
            return await handler(event, data)
        # Если не подписан — показываем экран подписки и блокируем дальнейшую обработку
        kb = InlineKeyboardBuilder()
        url = _channel_url()
        if url:
            kb.button(text="📣 Подписаться на канал", url=url)
        kb.button(text="✅ Проверить подписку", callback_data="check_sub")
        kb.adjust(1)
        try:
            # Используем .answer для Message и .message.answer для CallbackQuery
            if isinstance(event, Message):
                await event.answer(
                    "<b>Доступ к боту только для подписчиков.</b>\n\nПодпишитесь на канал, затем нажмите «Проверить подписку».",
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
            elif isinstance(event, CallbackQuery) and event.message:
                await event.message.answer(
                    "<b>Доступ к боту только для подписчиков.</b>\n\nПодпишитесь на канал, затем нажмите «Проверить подписку».",
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
        except Exception:
            pass
        return  # не пропускаем дальше, пока не подпишется

# Подключаем middleware на все входящие сообщения и коллбэки
dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cq: CallbackQuery):
    await cq.answer()
    if await _is_subscribed(cq.from_user.id):
        # Подписка подтверждена — показываем главное меню
        try:
            await cq.message.edit_text("Спасибо за подписку!")
        except TelegramBadRequest as e:
            # Игнорируем ошибку, если текст не изменился
            if "message is not modified" not in str(e):
                raise
        await bot.send_message(cq.from_user.id, make_welcome_text_for(cq), reply_markup=make_main_menu_kb(cq.from_user.id))
        return

    # Всё ещё не подписан — повторно покажем экран подписки
    kb = InlineKeyboardBuilder()
    url = _channel_url()
    if url:
        kb.button(text="📣 Подписаться на канал", url=url)
    kb.button(text="✅ Проверить подписку", callback_data="check_sub")
    kb.adjust(1)
    text = (
        "Пока не вижу подписку. Пожалуйста, подпишитесь и нажмите «Проверить подписку»."
    )
    try:
        await cq.message.edit_text(text, reply_markup=kb.as_markup())
    except TelegramBadRequest as e:
        # Если совсем тот же текст/клавиатура — просто покажем алерт
        if "message is not modified" in str(e):
            await cq.answer("Подписка пока не подтверждена. Проверьте, что вы подписались тем же аккаунтом.", show_alert=True)
        else:
            raise

# --- Админы ---
def get_admin_ids() -> list[int]:
    ids = getattr(settings, "ADMIN_IDS", [])
    # допускаем: список/кортеж чисел, строк, а также строку с запятой
    if isinstance(ids, (list, tuple, set)):
        out = []
        for v in ids:
            try:
                out.append(int(v))
            except Exception:
                continue
        return out
    if isinstance(ids, str):
        out = []
        for part in ids.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except Exception:
                continue
        return out
    return []

# --- Группа админов ---
def get_admin_group_id() -> int | None:
    gid = getattr(settings, "ADMIN_GROUP_ID", None)
    if gid is None:
        return None
    try:
        return int(str(gid).strip())
    except Exception:
        return None

# ========= Вспомогательные =========

def calc_total_price_rub_kopecks(qty: int) -> int:
    """Цена для клиента в копейках: RUB * 100."""
    price = store.user_price_per_star_rub * qty
    return int(math.floor(price * 100))


def calc_profit_rub(qty: int) -> float:
    return (store.user_price_per_star_rub - store.cost_per_star_rub) * qty


# ========= Команды =========

@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    bal_rub = rub_balance.get(m.from_user.id, 0) / 100
    kb.button(text=f"💰 Баланс: {bal_rub:.2f} ₽", callback_data="balance_info")
    kb.button(text="➕ Пополнить баланс", callback_data="balance")
    kb.button(text="⭐ Купить звёзды", callback_data="buy_menu")
    kb.button(text="🆘 Поддержка", callback_data="support")
    kb.adjust(2, 1, 1)

    user_name = m.from_user.first_name or (f"@{m.from_user.username}" if m.from_user.username else str(m.from_user.id))
    text = (
        f"Добро пожаловать, {user_name}! 🎉\n\n"
        "- Покупай ⭐ Telegram Stars со скидкой до 40%\n"
        "- Следи за акциями и выгодными предложениями\n"
        "- Делай подарки друзьям легко и быстро\n"
        "- Пополняй баланс удобным способом\n\n"
        "Погнали! 🚀"
    )
    await m.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data == "balance")
async def cb_balance(cq: CallbackQuery):
    await cq.answer()
    # Разрешаем свободный ввод суммы пополнения (без лишних кнопок)
    ask_custom_topup[cq.from_user.id] = True
    balance_kopecks = rub_balance.get(cq.from_user.id, 0)
    balance_rub = balance_kopecks / 100
    kb = InlineKeyboardBuilder()
    # Ряд 1
    for amt in [25, 50, 100]:
        kb.button(text=f"{amt}₽", callback_data=f"topup_amount:{amt}")
    # Ряд 2
    for amt in [200, 300, 500]:
        kb.button(text=f"{amt}₽", callback_data=f"topup_amount:{amt}")
    # Ряд 3
    for amt in [1000, 3000, 5000]:
        kb.button(text=f"{amt}₽", callback_data=f"topup_amount:{amt}")
    # Ряд 4 — широкий 10000₽
    kb.button(text="10000₽", callback_data="topup_amount:10000")
    # Ряд 5 — широкий Назад (исправлено)
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(3, 3, 3, 1, 1)
    await cq.message.edit_text(
        f"Ваш баланс: {balance_rub:.2f} ₽\n\n"
        "Выберите сумму пополнения или просто отправьте её числом в чат (от 25 ₽ до 100000 ₽).",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("topup_amount:"))
async def cb_topup_amount(cq: CallbackQuery):
    await cq.answer()
    amt_rub = int(cq.data.split(":")[1])
    # Сохраним выбранную сумму во временное состояние
    pending_qty[cq.from_user.id] = amt_rub  # переиспользуем словарь для простоты
    # Пользователь выбрал фиксированную сумму — выходим из режима свободного ввода
    ask_custom_topup[cq.from_user.id] = False
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Картой РФ", callback_data="pay_sbp")
    kb.button(text="🌐 TONCOIN [CryptoBot]", callback_data="pay_ton")
    kb.button(text="🌐 USDT [CryptoBot]", callback_data="pay_usdt")
    kb.button(text="⬅️ Назад", callback_data="balance")
    kb.adjust(1)
    await cq.message.edit_text(
        f"Пополнение на {amt_rub} ₽ — выберите способ оплаты:",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "topup_custom")
async def cb_topup_custom(cq: CallbackQuery):
    await cq.answer()
    ask_custom_topup[cq.from_user.id] = True
    await cq.message.edit_text(
        "Введите сумму пополнения в рублях (целое число). От 25 до 100000.\nНапример: 750"
    )


def _payment_instructions_text(method: str, amt_rub: int) -> str:
    if method == "sbp":
        return f"Сумма к оплате: {amt_rub} ₽\n\n{settings.SBP_INSTRUCTION}"
    if method == "ton":
        return f"Сумма к оплате: {amt_rub} ₽\nПерейдите: {settings.CRYPTO_TON_LINK}"
    if method == "usdt":
        return f"Сумма к оплате: {amt_rub} ₽\nПерейдите: {settings.CRYPTO_USDT_LINK}"
    return "Инструкции недоступны"

@dp.callback_query(F.data.in_({"pay_sbp", "pay_ton", "pay_usdt"}))
async def cb_pay_method(cq: CallbackQuery):
    await cq.answer()
    amt_rub = int(pending_qty.get(cq.from_user.id, 0) or 0)
    method = "sbp" if cq.data == "pay_sbp" else ("ton" if cq.data == "pay_ton" else "usdt")

    if method == "sbp":
        sbp_id = gen_sbp_id()
        pending_sbp[sbp_id] = {"user_id": cq.from_user.id, "amount_rub": amt_rub}
        save_pending_sbp()
        kb = InlineKeyboardBuilder()
        kb.button(text="➡️ Далее", callback_data=f"sbp_next:{sbp_id}")
        kb.button(text="⬅️ Назад", callback_data="balance")
        kb.adjust(1)
        await cq.message.edit_text(
            (
                "⚠️ <b>Пожалуйста, ознакомьтесь перед оплатой</b>:\n\n"
                "<b>Важно:</b> перед тем как оплатить, оставьте <b>КОД ЗАЯВКИ</b> в комментарии к переводу. "
                "Без этого админ не сможет подтвердить ваш заказ.\n\n"
                "Код заявки вы увидите далее 👇"
            ),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        return

    # TON / USDT — создаём инвойс в Crypto Pay на RUB (fiat) с ограничением на актив
    if method in {"ton", "usdt"}:
        asset = "TON" if method == "ton" else "USDT"
        payload = {
            "type": "topup",
            "user_id": cq.from_user.id,
            "topup_id": str(uuid.uuid4()),
            "amount_rub": amt_rub,
        }
        body = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(amt_rub),
            "accepted_assets": asset,
            "description": f"Пополнение {amt_rub} ₽ для user {cq.from_user.id}",
            "payload": json.dumps(payload),
            "allow_anonymous": True,
            "allow_comments": False,
            "expires_in": 1800,
        }
        headers = {
            "Crypto-Pay-API-Token": settings.CRYPTOPAY_API_TOKEN,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(f"{settings.CRYPTOPAY_API_URL}/createInvoice", headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
            if not data.get("ok"):
                await cq.message.edit_text(f"Ошибка Crypto Pay: {data.get('error','unknown')}")
                return
            inv = data.get("result", {})
            url = inv.get("mini_app_invoice_url") or inv.get("bot_invoice_url") or inv.get("pay_url")
            if not url or not isinstance(url, str) or not url.startswith("http"):
                await cq.message.edit_text("Crypto Pay вернул счёт без корректной ссылки для оплаты. Попробуйте позже.")
                return
            invoice_id = inv.get("invoice_id")
            pending_topups[cq.from_user.id] = {"topup_id": payload["topup_id"], "amount_rub": amt_rub, "invoice_id": invoice_id}
            kb = InlineKeyboardBuilder()
            # ВАЖНО: bot_invoice_url — это t.me deep link для mini-app; его нужно передавать как обычный URL-кнопки,
            # а не как web_app, иначе Telegram вернёт BUTTON_URL_INVALID
            kb.button(text="Оплатить в Crypto Bot (mini-app)", url=url)
            kb.button(text="Проверить оплату", callback_data="check_crypto")
            kb.button(text="⬅️ Назад", callback_data="balance")
            kb.adjust(1)
            await cq.message.edit_text(
                f"Выставлен счёт в Crypto Bot на {amt_rub} ₽ (актив: {asset}). Откроется мини‑апп CryptoBot.",
                reply_markup=kb.as_markup(),
            )
            return
        except Exception as e:
            await cq.message.edit_text(f"Не удалось создать счёт в Crypto Pay: {e}")
            return

    # СБП — показываем инструкцию (без API), зачисление по кнопке «Я оплатил»
    kb = InlineKeyboardBuilder()
    kb.button(text="Я оплатил", callback_data=f"paid:{method}")
    kb.button(text="⬅️ Назад", callback_data="balance")
    kb.adjust(1)
    await cq.message.edit_text(_payment_instructions_text(method, amt_rub), reply_markup=kb.as_markup())


# --- Новый обработчик: пользователь нажал "Я оплатил" для СБП ---
@dp.callback_query(F.data.startswith("sbp_paid:"))
async def cb_sbp_paid(cq: CallbackQuery):
    await cq.answer()
    sbp_id = cq.data.split(":", 1)[1]
    rec = pending_sbp.get(sbp_id)
    if not rec:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ В меню", callback_data="menu")
        kb.adjust(1)
        await cq.message.edit_text(
            "Заявка не найдена. Попробуйте выбрать сумму пополнения заново.",
            reply_markup=kb.as_markup(),
        )
        return
    amt_rub = rec.get("amount_rub", 0)
    # Формируем username_text для уведомления
    username_text = f"@{cq.from_user.username}" if cq.from_user.username else f"id={cq.from_user.id}"
    # Уведомляем админов для ручной проверки
    admin_group_id = get_admin_group_id()
    if not admin_group_id:
        await cq.message.edit_text(
            "Спасибо! Мы получили уведомление. (Внимание: группа админов не настроена — задайте ADMIN_GROUP_ID в settings.py)")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить оплату", callback_data=f"sbp_approve:{sbp_id}")
    kb.button(text="❌ Отклонить", callback_data=f"sbp_reject:{sbp_id}")
    kb.button(text="✏️ Изменить сумму", callback_data=f"sbp_change:{sbp_id}")
    kb.adjust(2, 1)
    try:
        await bot.send_message(
            admin_group_id,
            (
                "Заявка на пополнение по Карте РФ:\n"
                f"Код: {sbp_id}\n"
                f"Пользователь: {username_text}\n"
                f"Сумма: {amt_rub} ₽\n\n"
                "Проверьте перевод на карту и подтвердите."
            ),
            reply_markup=kb.as_markup(),
        )
    except Exception:
        try:
            await cq.message.answer("Не удалось отправить сообщение в группу админов. Проверьте, что бот добавлен в группу и имеет право писать.")
        except Exception:
            pass

    await cq.message.edit_text(
        (
            "Спасибо! Мы получили уведомление. Администратор проверит перевод и зачислит средства в ближайшее время.\n"
            f"Код заявки: <code>{sbp_id}</code>."
        ),
        parse_mode="HTML",
    )
    # Сразу отправляем главное меню отдельным сообщением
    await bot.send_message(cq.from_user.id, make_welcome_text_for(cq), reply_markup=make_main_menu_kb(cq.from_user.id))
# --- Новый обработчик: админ меняет сумму для СБП ---
@dp.callback_query(F.data.startswith("sbp_change:"))
async def cb_sbp_change(cq: CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in get_admin_ids():
        await cq.message.edit_text("Недостаточно прав.")
        return
    sbp_id = cq.data.split(":", 1)[1]
    rec = pending_sbp.get(sbp_id)
    if not rec:
        await cq.message.edit_text("Заявка уже обработана или не найдена.")
        return
    current_amt = int(rec.get("amount_rub", 0))
    chat_id = cq.message.chat.id
    sbp_change_wait[(chat_id, cq.from_user.id)] = sbp_id
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"sbp_back:{sbp_id}")
    kb.adjust(1)
    await cq.message.edit_text(
        f"Введите новую сумму (₽) целым числом. Текущая: {current_amt} ₽",
        reply_markup=kb.as_markup(),
    )
    # Отправляем ForceReply, чтобы админ ответил прямо в группе и бот гарантированно получил сообщение
    try:
        await cq.message.answer(
            "Пожалуйста, ответьте на это сообщение числом — новой суммой в ₽",
            reply_markup=ForceReply(selective=False),
        )
    except Exception:
        pass

# --- Обработчик "назад" при изменении суммы СБП ---
@dp.callback_query(F.data.startswith("sbp_back:"))
async def cb_sbp_back(cq: CallbackQuery):
    await cq.answer()
    key = (cq.message.chat.id, cq.from_user.id)
    if key in sbp_change_wait:
        sbp_change_wait.pop(key, None)
    sbp_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить оплату", callback_data=f"sbp_approve:{sbp_id}")
    kb.button(text="❌ Отклонить", callback_data=f"sbp_reject:{sbp_id}")
    kb.button(text="✏️ Изменить сумму", callback_data=f"sbp_change:{sbp_id}")
    kb.adjust(2, 1)
    rec = pending_sbp.get(sbp_id)
    amt_rub = int(rec.get("amount_rub", 0)) if rec else 0
    await cq.message.edit_text(
        (
            "Заявка на пополнение по Карте РФ:\n"
            f"Код: {sbp_id}\n"
            f"Сумма: {amt_rub} ₽\n\n"
            "Проверьте перевод на карту и подтвердите."
        ),
        reply_markup=kb.as_markup(),
    )


# --- Новый обработчик: админ подтверждает оплату по СБП ---
@dp.callback_query(F.data.startswith("sbp_approve:"))
async def cb_sbp_approve(cq: CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in get_admin_ids():
        await cq.message.edit_text("Недостаточно прав.")
        return
    sbp_id = cq.data.split(":", 1)[1]
    # Сначала пробуем найти в памяти; при отсутствии — подгружаем из файла
    rec = pending_sbp.get(sbp_id)
    if not rec:
        try:
            load_pending()
            rec = pending_sbp.get(sbp_id)
        except Exception:
            rec = None
    if not rec:
        await cq.message.edit_text("Заявка уже обработана или не найдена.")
        return
    # Теперь безопасно удаляем и сохраняем
    pending_sbp.pop(sbp_id, None)
    save_pending_sbp()
    user_id = rec.get("user_id")
    # Всегда берём актуальную (возможно отредактированную) сумму из заявки
    try:
        amt_rub = int(str(rec.get("amount_rub", 0)).strip())
    except Exception:
        amt_rub = 0
    if amt_rub <= 0:
        await cq.message.edit_text("Ошибка: сумма пополнения некорректна. Отредактируйте сумму перед подтверждением.")
        return
    rub_balance[user_id] = rub_balance.get(user_id, 0) + amt_rub * 100
    save_balances()
    # обновляем статистику суммарных пополнений
    total_deposits[user_id] = total_deposits.get(user_id, 0) + amt_rub * 100
    save_stats()
    # Сообщаем пользователю и админу
    try:
        kb_user = InlineKeyboardBuilder()
        kb_user.button(text="⬅️ В меню", callback_data="menu")
        kb_user.adjust(1)
        await bot.send_message(
            user_id,
            f"Оплата по Карте РФ подтверждена. Баланс пополнен на {amt_rub} ₽.",
            reply_markup=kb_user.as_markup(),
        )
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await cq.message.edit_text(
        f"Готово. Баланс пользователя {user_id} пополнен на {amt_rub} ₽.",
        reply_markup=kb.as_markup(),
    )


# --- Новый обработчик: админ отклоняет оплату по СБП ---
@dp.callback_query(F.data.startswith("sbp_reject:"))
async def cb_sbp_reject(cq: CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in get_admin_ids():
        await cq.message.edit_text("Недостаточно прав.")
        return
    sbp_id = cq.data.split(":", 1)[1]
    rec = pending_sbp.get(sbp_id)
    if not rec:
        try:
            load_pending()
            rec = pending_sbp.get(sbp_id)
        except Exception:
            rec = None
    if not rec:
        await cq.message.edit_text("Заявка уже обработана или не найдена.")
        return
    pending_sbp.pop(sbp_id, None)
    save_pending_sbp()
    user_id = rec.get("user_id")
    amt_rub = int(rec.get("amount_rub", 0))
    # Уведомляем пользователя об отказе
    try:
        await bot.send_message(user_id, (
            "Оплата по Карте РФ не подтверждена. Если вы перевели средства, ответьте в чат с квитанцией, и мы проверим повторно."))
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await cq.message.edit_text(f"Заявка {sbp_id} отклонена.", reply_markup=kb.as_markup())


 # --- Новый обработчик: админ подтверждает заявку на покупку звёзд ---
@dp.callback_query(F.data.startswith("star_approve:"))
async def cb_star_approve(cq: CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in get_admin_ids():
        await cq.message.edit_text("Недостаточно прав.")
        return
    order_id = cq.data.split(":", 1)[1]
    rec = pending_orders.get(order_id)
    if not rec:
        try:
            load_pending()
            rec = pending_orders.get(order_id)
        except Exception:
            rec = None
    if not rec:
        await cq.message.edit_text("Заявка не найдена или уже обработана.")
        return
    user_id = rec["user_id"]
    qty = rec["qty"]
    price_kopecks = rec["price_kopecks"]
    username = rec["username"]
    # повторная проверка наличия средств на момент подтверждения
    if rub_balance.get(user_id, 0) < price_kopecks:
        await cq.message.edit_text("Недостаточно средств на балансе пользователя для списания. Попросите пополнить баланс.")
        return
    # списание и уведомления
    rub_balance[user_id] = rub_balance.get(user_id, 0) - price_kopecks
    save_balances()
    total_stars[user_id] = total_stars.get(user_id, 0) + qty
    save_stats()
    pending_orders.pop(order_id, None)
    save_pending_orders()
    try:
        kb_user = InlineKeyboardBuilder()
        kb_user.button(text="⬅️ В меню", callback_data="menu")
        kb_user.adjust(1)
        await bot.send_message(
            user_id,
            (
                f"Администратор подтвердил покупку {qty} ⭐ для {username}. "
                f"Списано {price_kopecks/100:.2f} ₽. Спасибо!"
            ),
            reply_markup=kb_user.as_markup(),
        )
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await cq.message.edit_text(
        f"Готово. Покупка {qty} ⭐ подтверждена, списано {price_kopecks/100:.2f} ₽.",
        reply_markup=kb.as_markup(),
    )

# --- Новый обработчик: админ отклоняет заявку на покупку звёзд ---
@dp.callback_query(F.data.startswith("star_reject:"))
async def cb_star_reject(cq: CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in get_admin_ids():
        await cq.message.edit_text("Недостаточно прав.")
        return
    order_id = cq.data.split(":", 1)[1]
    rec = pending_orders.get(order_id)
    if not rec:
        try:
            load_pending()
            rec = pending_orders.get(order_id)
        except Exception:
            rec = None
    if not rec:
        await cq.message.edit_text("Заявка не найдена или уже обработана.")
        return
    pending_orders.pop(order_id, None)
    save_pending_orders()
    user_id = rec["user_id"]
    qty = rec["qty"]
    price_kopecks = rec["price_kopecks"]
    try:
        await bot.send_message(user_id, (
            f"Заявка на покупку {qty} ⭐ отклонена администратором. Средства не списаны."))
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await cq.message.edit_text("Заявка отклонена.", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "check_crypto")
async def cb_check_crypto(cq: CallbackQuery):
    await cq.answer()
    topup = pending_topups.get(cq.from_user.id)
    if not topup:
        await cq.message.edit_text("Нет ожидающих пополнений для проверки.")
        return

    headers = {"Crypto-Pay-API-Token": settings.CRYPTOPAY_API_TOKEN}
    params = {"status": "paid", "fiat": "RUB"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{settings.CRYPTOPAY_API_URL}/getInvoices", headers=headers, params=params)
        if r.status_code != 200:
            await cq.message.edit_text(f"Crypto Pay HTTP {r.status_code}: {r.text}")
            return
        # Пытаемся распарсить JSON; если вернулась строка/HTML — покажем как есть
        try:
            data = r.json()
        except Exception:
            await cq.message.edit_text(f"Crypto Pay вернул не JSON:\n{r.text}")
            return
        if not isinstance(data, dict):
            await cq.message.edit_text(f"Crypto Pay ответил неожиданно:\n{data}")
            return
        if not data.get("ok"):
            err = data.get("error") or data
            await cq.message.edit_text(f"Ошибка Crypto Pay: {err}")
            return
        result = data.get("result")
        if isinstance(result, list):
            invoices = result
        elif isinstance(result, dict) and "items" in result:
            invoices = result["items"]
        else:
            await cq.message.edit_text(f"Неверный формат invoices: {result}")
            return
        if not isinstance(invoices, list):
            await cq.message.edit_text(f"Неверный формат invoices: {invoices}")
            return
        found = None
        for inv in invoices:
            p_raw = inv.get("payload")
            if not p_raw:
                continue
            try:
                p = json.loads(p_raw) if isinstance(p_raw, str) else p_raw
            except Exception:
                p = {}
            if p.get("topup_id") == topup.get("topup_id") and int(p.get("user_id", 0)) == cq.from_user.id:
                found = inv
                break
        if not found:
            await cq.message.edit_text("Платёж пока не виден как оплаченный. Попробуйте позже.")
            return
        # Зачисляем баланс
        amt_rub = int(topup.get("amount_rub", 0))
        rub_balance[cq.from_user.id] = rub_balance.get(cq.from_user.id, 0) + amt_rub * 100
        save_balances()
        # обновляем статистику суммарных пополнений
        total_deposits[cq.from_user.id] = total_deposits.get(cq.from_user.id, 0) + amt_rub * 100
        save_stats()
        pending_topups.pop(cq.from_user.id, None)
        balance_rub = rub_balance[cq.from_user.id] / 100
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ В главное меню", callback_data="menu")
        kb.adjust(1)
        await cq.message.edit_text(
            f"Оплата найдена и подтверждена Crypto Bot. Баланс пополнен на {amt_rub} ₽. Текущий баланс: {balance_rub:.2f} ₽",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await cq.message.edit_text(f"Ошибка проверки платежа: {e}")


 # ======== Helpers: main menu & welcome text ========
from typing import Optional

def make_main_menu_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    bal_rub = rub_balance.get(user_id, 0) / 100
    kb.button(text=f"💰 Мой баланс: {bal_rub:.2f} ₽", callback_data="balance_info")
    kb.button(text="➕ Пополнить баланс", callback_data="balance")
    kb.button(text="⭐ Купить звёзды", callback_data="buy_menu")
    kb.button(text="🆘 Поддержка", callback_data="support")
    kb.adjust(2, 1, 1)
    return kb.as_markup()

def make_welcome_text_for(obj: Message | CallbackQuery) -> str:
    u = obj.from_user
    name = u.first_name or (f"@{u.username}" if u.username else str(u.id))
    return (
        f"Добро пожаловать, {name}! 🎉\n\n"
        "- Покупай ⭐ Telegram Stars со скидкой до 40%\n"
        "- Следи за акциями и выгодными предложениями\n"
        "- Делай подарки друзьям легко и быстро\n"
        "- Пополняй баланс удобным способом\n\n"
        "Погнали! 🚀"
    )

@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardBuilder()
    for qty in [25, 50, 100, 200, 300, 500, 1000, 3000, 5000, 10000, 25000, 50000, 100000]:
        kb.button(text=f"{qty} ⭐", callback_data=f"buy:{qty}")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(3, 3, 3, 3, 1, 1)
    price = store.user_price_per_star_rub
    await cq.message.edit_text(
        (
            f"Цена 1 ⭐ = {price:.2f} ₽.\n"
            "Лимит покупки: от 25 до 1 000 000 ⭐.\n\n"
            "Выберите количество ⭐ или просто отправьте числом нужное количество в чат."
        ),
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardBuilder()
    bal_rub = rub_balance.get(cq.from_user.id, 0) / 100
    kb.button(text=f"💰 Мой баланс: {bal_rub:.2f} ₽", callback_data="balance_info")
    kb.button(text="➕ Пополнить баланс", callback_data="balance")
    kb.button(text="⭐ Купить звёзды", callback_data="buy_menu")
    kb.button(text="🆘 Поддержка", callback_data="support")
    kb.adjust(2, 1, 1)
    user_name = cq.from_user.first_name or (f"@{cq.from_user.username}" if cq.from_user.username else str(cq.from_user.id))
    text = (
        f"Добро пожаловать, {user_name}! 🎉\n\n"
        "- Покупай ⭐ Telegram Stars со скидкой до 40%\n"
        "- Следи за акциями и выгодными предложениями\n"
        "- Делай подарки друзьям легко и быстро\n"
        "- Пополняй баланс удобным способом\n\n"
        "Погнали! 🚀"
    )
    await cq.message.edit_text(text, reply_markup=kb.as_markup())
# --- Новый обработчик: показать баланс и сумму пополнений ---
@dp.callback_query(F.data == "balance_info")
async def cb_balance_info(cq: CallbackQuery):
    await cq.answer()
    bal = rub_balance.get(cq.from_user.id, 0) / 100
    dep = total_deposits.get(cq.from_user.id, 0) / 100
    stars = total_stars.get(cq.from_user.id, 0)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пополнить баланс", callback_data="balance")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(1)
    await cq.message.edit_text(
        (
            f"💰 <b>Баланс:</b> {bal:.2f} ₽\n\n"
            f"📥 <b>Пополнено за всё время:</b> {dep:.2f} ₽\n\n"
            f"⭐ <b>Куплено звёзд за всё время:</b> {stars} ⭐"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

# --- Новый обработчик: поддержка ---
@dp.callback_query(F.data == "support")
async def cb_support(cq: CallbackQuery):
    await cq.answer()
    # URLs from settings (optional)
    faq_url = getattr(settings, "FAQ_URL", "").strip()
    # Жёстко указываем контакт поддержки на @BloonesAkk
    support_url = "https://t.me/BloonesAkk"

    kb = InlineKeyboardBuilder()
    # FAQ button: URL if provided, otherwise callback to in-bot FAQ
    if faq_url:
        kb.button(text="❓ Часто задаваемые вопросы", url=faq_url)
    else:
        kb.button(text="❓ Часто задаваемые вопросы", callback_data="support_faq")
    # Contact button: всегда URL на @BloonesAkk
    if support_url:
        kb.button(text="📞 Связь", url=support_url)
    else:
        kb.button(text="📞 Связь", callback_data="support_contact")
    # Back button
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(1)

    await cq.message.edit_text(
        "Вы в разделе поддержки. Выберите действие:",
        reply_markup=kb.as_markup(),
    )


# --- FAQ fallback handler ---
@dp.callback_query(F.data == "support_faq")
async def cb_support_faq(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="support")
    kb.adjust(1)
    await cq.message.edit_text(
        (
            "❓ Часто задаваемые вопросы:\n\n"
            "<b>Как происходит сделка?</b>\n\n"
            "→ Первое время всё в ручную, но мы работаем над обновлением\n\n"
            "<b>Безопасно ли это?</b>\n\n"
            "→ Да. Мы используем проверенные методы, никаких скрытых условий.\n\n"
            "<b>Сколько занимает времени?</b>\n\n"
            "→ Оплата проходит мгновенно, но бывают промежутки времени, обычно с 00:00 - 07:00 по Московскому времени, когда транзакция обрабатывается дольше обычного.\n\n"
            "Остались вопросы? Свяжитесь с нами"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


# --- Contact fallback handler ---
@dp.callback_query(F.data == "support_contact")
async def cb_support_contact(cq: CallbackQuery):
    await cq.answer()
    url = "https://t.me/BloonesAkk"
    kb = InlineKeyboardBuilder()
    if url:
        kb.button(text="Открыть чат поддержки", url=url)
    kb.button(text="⬅️ Назад", callback_data="support")
    kb.adjust(1)
    await cq.message.edit_text(
        (
            "Связь с поддержкой:\n"
            + (f"Напишите нам: {url}" if url else "Контакт поддержки не настроен. Обратитесь к администратору.")
        ),
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("buy:"))
async def cq_buy(cq: CallbackQuery):
    qty = int(cq.data.split(":")[1])
    # Проверяем лимиты
    if qty < 25 or qty > 1_000_000:
        await cq.message.edit_text("Допустимый диапазон: от 25 до 1 000 000 ⭐. Введите число в чате или выберите из списка.")
        return
    await cq.answer()
    username_text = f"@{cq.from_user.username}" if cq.from_user.username else f"id={cq.from_user.id}"
    pending_qty[cq.from_user.id] = qty
    username = f"@{cq.from_user.username}" if cq.from_user.username else str(cq.from_user.id)
    price_kopecks = calc_total_price_rub_kopecks(qty)
    current_balance = rub_balance.get(cq.from_user.id, 0)
    if current_balance < price_kopecks:
        need = (price_kopecks - current_balance) / 100
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ В меню", callback_data="menu")
        kb.adjust(1)
        await cq.message.edit_text(
            f"Стоимость {qty} ⭐: {price_kopecks/100:.2f} ₽. Недостаточно средств. Пополните ещё {need:.2f} ₽ через Пополнить Баланс.",
            reply_markup=kb.as_markup(),
        )
        return

    # достаточно средств — формируем заявку админам, списание при подтверждении
    order_id = gen_order_id()
    pending_orders[order_id] = {
        "user_id": cq.from_user.id,
        "qty": qty,
        "price_kopecks": price_kopecks,
        "username": username,
    }
    save_pending_orders()

    # отправка админам
    admin_group_id = get_admin_group_id()
    if not admin_group_id:
        await cq.message.edit_text("Не удалось отправить заявку: группа админов не настроена. Установите ADMIN_GROUP_ID в settings.py.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"star_approve:{order_id}")
    kb.button(text="❌ Отклонить", callback_data=f"star_reject:{order_id}")
    kb.adjust(2)
    try:
        await bot.send_message(
            admin_group_id,
            (
                "Заявка на покупку ⭐ вручную:\n"
                f"Код: {order_id}\n"
                f"Пользователь: {username_text}\n"
                f"Количество: {qty} ⭐\n"
                f"К списанию: {price_kopecks/100:.2f} ₽\n\n"
                "После оплаты звёзд вручную подтвердите заявку."
            ),
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await cq.message.edit_text("Не удалось отправить сообщение в группу админов. Проверьте, что бот добавлен в группу и может писать.")
        return

    await cq.message.edit_text(
        (
            "Заявка отправлена администратору. Как только админ купит звёзды и подтвердит — с баланса спишется нужная сумма, а вы получите уведомление.\n"
            f"Код заявки: <code>{order_id}</code>."
        ),
        parse_mode="HTML",
    )
    # Сразу отправляем главное меню отдельным сообщением
    await bot.send_message(cq.from_user.id, make_welcome_text_for(cq), reply_markup=make_main_menu_kb(cq.from_user.id))
    return


@dp.callback_query(F.data == "custom")
async def cq_custom(cq: CallbackQuery):
    await cq.answer()
    ask_custom[cq.from_user.id] = True


@dp.message()
async def handle_text(m: Message):
    # Админ вводит новую сумму для СБП (можно писать прямо в админ-группе)
    key_change = (m.chat.id, m.from_user.id)
    if key_change in sbp_change_wait and m.text and m.text.isdigit():
        sbp_id = sbp_change_wait.pop(key_change)
        rec = pending_sbp.get(sbp_id)
        if not rec:
            await m.answer("Заявка не найдена или уже обработана.")
            return
        try:
            new_amt = int(str(m.text).strip())
        except Exception:
            new_amt = 0
        if new_amt <= 0:
            await m.answer("Сумма должна быть положительным числом. Попробуйте ещё раз.")
            sbp_change_wait[key_change] = sbp_id
            return
        rec["amount_rub"] = int(new_amt)
        save_pending_sbp()
        await m.answer(f"OK. Новая сумма для заявки {sbp_id}: {new_amt} ₽. При подтверждении будет зачислена именно эта сумма.")
        return
    # Пользователь ввёл свою сумму пополнения (₽)
    if ask_custom_topup.get(m.from_user.id) and m.text and m.text.isdigit():
        amt_rub = int(m.text)
        if amt_rub < 25 or amt_rub > 100000:
            await m.answer("Сумма вне допустимого диапазона. Введите от 25 до 100000 ₽.")
            return
        ask_custom_topup[m.from_user.id] = False
        pending_qty[m.from_user.id] = amt_rub
        kb = InlineKeyboardBuilder()
        kb.button(text="🌐 TONCOIN [CryptoBot]", callback_data="pay_ton")
        kb.button(text="🌐 USDT [CryptoBot]", callback_data="pay_usdt")
        kb.button(text="💳 Картой РФ", callback_data="pay_sbp")
        kb.button(text="⬅️ Назад", callback_data="balance")
        kb.adjust(1)
        await m.answer(
            f"Пополнение на {amt_rub} ₽ — вы"
            f"берите способ оплаты:",
            reply_markup=kb.as_markup(),
        )
        return

    # Админ-команды текстом (быстро и просто)
    if m.from_user.id in settings.ADMIN_IDS and m.text:
        if m.text.startswith("/set_price"):
            try:
                store.user_price_per_star_rub = float(m.text.split()[1])
                await m.answer(f"OK. Новая цена для клиента: {store.user_price_per_star_rub:.2f} ₽ за 1 ⭐")
            except Exception:
                await m.answer("Использование: /set_price 3.50")
            return
        if m.text.startswith("/set_cost"):
            try:
                store.cost_per_star_rub = float(m.text.split()[1])
                await m.answer(f"OK. Новая себестоимость: {store.cost_per_star_rub:.2f} ₽ за 1 ⭐")
            except Exception:
                await m.answer("Использование: /set_cost 3.10")
            return
        if m.text.startswith("/stats"):
            await m.answer("В этой демо-версии статистика хранится только в логах / памяти. Подключите БД для прод.")
            return

    # Свободный ввод количества ⭐ без нажатия кнопок (если это не ввод суммы пополнения)
    if not ask_custom_topup.get(m.from_user.id) and m.text and m.text.isdigit():
        qty = max(25, min(1_000_000, int(m.text)))
        pending_qty[m.from_user.id] = qty
        username = f"@{m.from_user.username}" if m.from_user.username else str(m.from_user.id)
        username_text = f"@{m.from_user.username}" if m.from_user.username else f"id={m.from_user.id}"
        price_kopecks = calc_total_price_rub_kopecks(qty)
        current_balance = rub_balance.get(m.from_user.id, 0)
        if current_balance < price_kopecks:
            need = (price_kopecks - current_balance) / 100
            kb = InlineKeyboardBuilder()
            kb.button(text="⬅️ В меню", callback_data="menu")
            kb.adjust(1)
            await m.answer(
                f"Стоимость {qty} ⭐: {price_kopecks/100:.2f} ₽. Недостаточно средств. Пополните ещё {need:.2f} ₽ через Баланс.",
                reply_markup=kb.as_markup(),
            )
            return

        order_id = gen_order_id()
        pending_orders[order_id] = {
            "user_id": m.from_user.id,
            "qty": qty,
            "price_kopecks": price_kopecks,
            "username": username,
        }
        save_pending_orders()

        admin_group_id = get_admin_group_id()
        if not admin_group_id:
            await m.answer("Не удалось отправить заявку: группа админов не настроена. Установите ADMIN_GROUP_ID в settings.py.")
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить", callback_data=f"star_approve:{order_id}")
        kb.button(text="❌ Отклонить", callback_data=f"star_reject:{order_id}")
        kb.adjust(2)
        try:
            await bot.send_message(
                admin_group_id,
                (
                    "Заявка на покупку ⭐ вручную:\n"
                    f"Код: {order_id}\n"
                    f"Пользователь: {username_text}\n"
                    f"Количество: {qty} ⭐\n"
                    f"К списанию: {price_kopecks/100:.2f} ₽\n\n"
                    "После оплаты звёзд вручную подтвердите заявку."
                ),
                reply_markup=kb.as_markup(),
            )
        except Exception:
            await m.answer("Не удалось отправить сообщение в группу админов. Проверьте, что бот добавлен в группу и может писать.")
            return

        await m.answer(
            (
                "Заявка отправлена администратору. Как только админ отправит звёзды и подтвердит — с баланса спишется нужная сумма, а вы получите уведомление.\n"
                f"Код заявки: <code>{order_id}</code>."
            ),
            parse_mode="HTML",
        )
        # Сразу отправляем главное меню отдельным сообщением
        await m.answer(make_welcome_text_for(m), reply_markup=make_main_menu_kb(m.from_user.id))
        return

    # Кастомное кол-во
    user_ask_custom = ask_custom.get(m.from_user.id)
    if user_ask_custom and m.text and m.text.isdigit():
        qty = max(25, min(1_000_000, int(m.text)))
        ask_custom[m.from_user.id] = False
        pending_qty[m.from_user.id] = qty
        username = f"@{m.from_user.username}" if m.from_user.username else str(m.from_user.id)
        price_kopecks = calc_total_price_rub_kopecks(qty)
        current_balance = rub_balance.get(m.from_user.id, 0)
        if current_balance < price_kopecks:
            need = (price_kopecks - current_balance) / 100
            kb = InlineKeyboardBuilder()
            kb.button(text="⬅️ В меню", callback_data="menu")
            kb.adjust(1)
            await m.answer(
                f"Стоимость {qty} ⭐: {price_kopecks/100:.2f} ₽. Недостаточно средств. Пополните ещё {need:.2f} ₽ через Баланс.",
                reply_markup=kb.as_markup(),
            )
            return

        # достаточно средств — формируем заявку админам, списание при подтверждении
        order_id = gen_order_id()
        pending_orders[order_id] = {
            "user_id": m.from_user.id,
            "qty": qty,
            "price_kopecks": price_kopecks,
            "username": username,
        }
        save_pending_orders()

        admin_group_id = get_admin_group_id()
        if not admin_group_id:
            await m.answer("Не удалось отправить заявку: группа админов не настроена. Установите ADMIN_GROUP_ID в settings.py.")
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить", callback_data=f"star_approve:{order_id}")
        kb.button(text="❌ Отклонить", callback_data=f"star_reject:{order_id}")
        kb.adjust(2)
        username_text = f"@{m.from_user.username}" if m.from_user.username else f"id={m.from_user.id}"
        try:
            await bot.send_message(
                admin_group_id,
                (
                    "Заявка на покупку ⭐ вручную:\n"
                    f"Код: {order_id}\n"
                    f"Пользователь: {username_text}\n"
                    f"Количество: {qty} ⭐\n"
                    f"К списанию: {price_kopecks/100:.2f} ₽\n\n"
                    "После оплаты звёзд вручную подтвердите заявку."
                ),
                reply_markup=kb.as_markup(),
            )
        except Exception:
            await m.answer("Не удалось отправить сообщение в группу админов. Проверьте, что бот добавлен в группу и может писать.")
            return

        await m.answer(
            (
                "Заявка отправлена администратору. Как только админ купит звёзды и подтвердит — с баланса спишется нужная сумма, а вы получите уведомление.\n"
                f"Код заявки: <code>{order_id}</code>."
            ),
            parse_mode="HTML",
        )
        # Сразу отправляем главное меню отдельным сообщением
        await m.answer(make_welcome_text_for(m), reply_markup=make_main_menu_kb(m.from_user.id))
        return


@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    await m.answer("Получен платёж через встроенные инвойсы, но текущая логика использует внешние способы оплаты. Платёж не обработан.")

# --- Новый обработчик: копирование кода заказа/пополнения ---
@dp.callback_query(F.data.startswith("copy_code:"))
async def cb_copy_code(cq: CallbackQuery):
    await cq.answer("Код отправлен ниже", show_alert=False)
    code = cq.data.split(":", 1)[1]
    try:
        await cq.message.answer(f"КОД ЗАКАЗА:\n<code>{code}</code>", parse_mode="HTML")
    except Exception:
        pass
# --- Новый обработчик: пользователь нажал "Далее" для СБП ---
@dp.callback_query(F.data.startswith("sbp_next:"))
async def cb_sbp_next(cq: CallbackQuery):
    await cq.answer()
    sbp_id = cq.data.split(":", 1)[1]
    rec = pending_sbp.get(sbp_id)
    if not rec:
        await cq.message.edit_text("Заявка не найдена. Попробуйте выбрать сумму пополнения заново.")
        return
    amt_rub = int(rec.get("amount_rub", 0))
    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить с помощью карты РФ", url="https://www.tinkoff.ru/rm/r_BsEDfioFGw.TeFxbzJZVa/HaZwn92444")
    kb.button(text="Я оплатил", callback_data=f"sbp_paid:{sbp_id}")
    kb.button(text="⬅️ Назад", callback_data="balance")
    kb.adjust(1)
    await cq.message.edit_text(
        (
            "Ссылка для оплаты через карту РФ\n\n"
            "<b>Пожалуйста, перед тем как оплатить оставьте КОД ЗАЯВКИ в комментарии к оплате.</b> "
            "Без этого админ не сможет подтвердить ваш заказ.\n\n"
            f"КОД ЗАЯВКИ: <code>{sbp_id}</code>\n\n"
            f"Сумма к оплате: <b>{amt_rub} ₽</b>\n\n"
            "После перевода нажмите ‘Я оплатил’.\n"
            "Админ проверит поступление и зачислит средства."
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

if __name__ == "__main__":
    if not settings.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    load_balances()
    load_stats()
    load_pending()
    asyncio.run(dp.start_polling(bot))


# --- Админ-команда: диагностика подписки ---
@dp.message(Command("subdebug"))
async def cmd_subdebug(m: Message):
    # Только для администраторов
    if m.from_user.id not in get_admin_ids():
        await m.answer("Эта команда доступна только администраторам.")
        return

    chat_ref = _resolve_chat_ref()
    url = _channel_url()

    lines = [
        "🔎 Диагностика подписки",
        f"REQUIRED_CHANNEL: <code>{str(REQUIRED_CHANNEL)}</code>",
        f"REQUIRED_CHANNEL_URL: <code>{url or '-'}" + "</code>",
        f"resolved chat_ref: <code>{str(chat_ref)}</code>",
    ]

    # Проверим getChat
    try:
        chat = await bot.get_chat(chat_ref)
        lines.append(f"getChat: OK — title=\"{chat.title}\", id={chat.id}")
    except Exception as e:
        lines.append(f"getChat: ERROR — {e}")

    # Проверим текущего пользователя (админа)
    try:
        member = await bot.get_chat_member(chat_ref, m.from_user.id)
        lines.append(f"getChatMember(you): status=\"{getattr(member, 'status', 'unknown')}\"")
    except Exception as e:
        lines.append(f"getChatMember(you): ERROR — {e}")

    await m.answer("\n".join(lines), parse_mode="HTML")
