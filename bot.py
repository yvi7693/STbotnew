import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

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
rub_balance: dict[int, int] = {}  # баланс в копейках (RUB*100)
# ожидаемые пополнения через Crypto Pay: user_id -> {topup_id, amount_rub, invoice_id}
pending_topups: dict[int, dict] = {}

# флаг ожидания пользовательского ввода суммы пополнения в ₽
ask_custom_topup: dict[int, bool] = {}

bot = Bot(settings.BOT_TOKEN)
dp = Dispatcher()

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
    kb.button(text="💰 Баланс (₽)", callback_data="balance")
    kb.button(text="⭐ Купить звёзды", callback_data="buy_menu")
    kb.adjust(2)

    text = (
        "Привет! Здесь мы научим и расскажем, как пополнить баланс в рублях и купить ⭐ Telegram Stars.\n\n"
        f"Текущая цена: {store.user_price_per_star_rub:.2f} ₽ за 1 ⭐"
    )
    await m.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data == "balance")
async def cb_balance(cq: CallbackQuery):
    await cq.answer()
    balance_kopecks = rub_balance.get(cq.from_user.id, 0)
    balance_rub = balance_kopecks / 100
    kb = InlineKeyboardBuilder()
    for amt in [300, 500, 1000, 2500, 5000]:
        kb.button(text=f"Пополнить +{amt} ₽", callback_data=f"topup_amount:{amt}")
    kb.button(text="Другая сумма (₽)", callback_data="topup_custom")
    if pending_topups.get(cq.from_user.id):
        kb.button(text="Проверить оплату", callback_data="check_crypto")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(2)
    await cq.message.edit_text(f"Ваш баланс: {balance_rub:.2f} ₽", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("topup_amount:"))
async def cb_topup_amount(cq: CallbackQuery):
    await cq.answer()
    amt_rub = int(cq.data.split(":")[1])
    # Сохраним выбранную сумму во временное состояние
    pending_qty[cq.from_user.id] = amt_rub  # переиспользуем словарь для простоты
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 СБП", callback_data="pay_sbp")
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
        "Введите сумму пополнения в рублях (целое число). Минимум 10, максимум 100000.\nНапример: 750"
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


@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardBuilder()
    for qty in [50, 100, 250, 500, 1000]:
        kb.button(text=f"Купить {qty} ⭐", callback_data=f"buy:{qty}")
    kb.button(text="Другая сумма", callback_data="custom")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(2)
    await cq.message.edit_text("Выберите количество звёзд для покупки:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Баланс (₽)", callback_data="balance")
    kb.button(text="⭐ Купить звёзды", callback_data="buy_menu")
    kb.adjust(2)
    await cq.message.edit_text("Главное меню:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("buy:"))
async def cq_buy(cq: CallbackQuery):
    qty = int(cq.data.split(":")[1])
    await cq.answer()
    pending_qty[cq.from_user.id] = qty
    username = f"@{cq.from_user.username}" if cq.from_user.username else str(cq.from_user.id)
    price_kopecks = calc_total_price_rub_kopecks(qty)
    current_balance = rub_balance.get(cq.from_user.id, 0)
    if current_balance < price_kopecks:
        need = (price_kopecks - current_balance) / 100
        await cq.message.edit_text(
            f"Стоимость {qty} ⭐: {price_kopecks/100:.2f} ₽. Недостаточно средств. Пополните ещё {need:.2f} ₽ через Баланс.")
        return

    # списываем и оформляем заказ
    rub_balance[cq.from_user.id] = current_balance - price_kopecks
    await cq.message.edit_text("Оплата получена. Оформляю покупку на split.tg…")
    try:
        client = SplitClient(
            settings.SPLIT_EMAIL,
            settings.SPLIT_PASSWORD,
            headless=False,    # показать окно браузера
            slow_mo=250,       # замедлить действия для наглядности
            record_video=True  # записать видео в папку videos/
        )
        order_id = await client.buy_stars(username, qty)
        # Если split вернул ссылку на оплату через CryptoBot, показываем её пользователю
        if isinstance(order_id, str) and order_id.startswith("PAYMENT_LINK::"):
            pay_url = order_id.split("::", 1)[1]
            if pay_url and isinstance(pay_url, str) and pay_url.startswith("http"):
                kb = InlineKeyboardBuilder()
                kb.button(text="Оплатить заказ в CryptoBot", url=pay_url)
                kb.button(text="⬅️ В главное меню", callback_data="menu")
                kb.adjust(1)
                await cq.message.answer(
                    "Для завершения заказа требуется оплата через CryptoBot (ссылка ниже). После оплаты split.tg обработает покупку автоматически.",
                    reply_markup=kb.as_markup(),
                )
            else:
                await cq.message.answer(
                    "Ссылка на оплату от split.tg недоступна. Попробуйте ещё раз или свяжитесь с поддержкой.")
            return
    except Exception:
        # Возврат средств в случае ошибки
        rub_balance[cq.from_user.id] += price_kopecks
        await cq.message.answer("Не удалось оформить покупку на split.tg. Средства возвращены на баланс.")
        raise

    await cq.message.answer(
        f"Готово! Заказ оформлен.\nПополнение: {qty} ⭐ на {username}\nСписано: {price_kopecks/100:.2f} ₽\nТекущий баланс: {rub_balance.get(cq.from_user.id,0)/100:.2f} ₽")


@dp.callback_query(F.data == "custom")
async def cq_custom(cq: CallbackQuery):
    await cq.answer()
    ask_custom[cq.from_user.id] = True


@dp.message()
async def handle_text(m: Message):
    # Пользователь ввёл свою сумму пополнения (₽)
    if ask_custom_topup.get(m.from_user.id) and m.text and m.text.isdigit():
        amt_rub = int(m.text)
        if amt_rub < 10 or amt_rub > 100000:
            await m.answer("Сумма вне допустимого диапазона. Введите от 10 до 100000 ₽.")
            return
        ask_custom_topup[m.from_user.id] = False
        pending_qty[m.from_user.id] = amt_rub
        kb = InlineKeyboardBuilder()
        kb.button(text="🌐 TONCOIN [CryptoBot]", callback_data="pay_ton")
        kb.button(text="🌐 USDT [CryptoBot]", callback_data="pay_usdt")
        kb.button(text="💳 СБП", callback_data="pay_sbp")
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

    # Кастомное кол-во
    user_ask_custom = ask_custom.get(m.from_user.id)
    if user_ask_custom and m.text and m.text.isdigit():
        qty = max(1, min(5000, int(m.text)))
        ask_custom[m.from_user.id] = False
        pending_qty[m.from_user.id] = qty
        username = f"@{m.from_user.username}" if m.from_user.username else str(m.from_user.id)
        price_kopecks = calc_total_price_rub_kopecks(qty)
        current_balance = rub_balance.get(m.from_user.id, 0)
        if current_balance < price_kopecks:
            need = (price_kopecks - current_balance) / 100
            await m.answer(
                f"Стоимость {qty} ⭐: {price_kopecks/100:.2f} ₽. Недостаточно средств. Пополните ещё {need:.2f} ₽ через Баланс.")
            return

        # списываем и оформляем заказ
        rub_balance[m.from_user.id] = current_balance - price_kopecks
        await m.answer("Оплата получена. Оформляю покупку на split.tg…")
        try:
            client = SplitClient(
                settings.SPLIT_EMAIL,
                settings.SPLIT_PASSWORD,
                headless=False,
                slow_mo=250,
                record_video=True
            )
            order_id = await client.buy_stars(username, qty)
            if isinstance(order_id, str) and order_id.startswith("PAYMENT_LINK::"):
                pay_url = order_id.split("::", 1)[1]
                kb = InlineKeyboardBuilder()
                kb.button(text="Оплатить заказ в CryptoBot", url=pay_url)
                kb.button(text="⬅️ В главное меню", callback_data="menu")
                kb.adjust(1)
                await m.answer(
                    "Для завершения заказа требуется оплата через CryptoBot (ссылка ниже). После оплаты split.tg обработает покупку автоматически.",
                    reply_markup=kb.as_markup(),
                )
                return
        except Exception:
            # Возврат средств в случае ошибки
            rub_balance[m.from_user.id] += price_kopecks
            await m.answer("Не удалось оформить покупку на split.tg. Средства возвращены на баланс.")
            return

        await m.answer(
            f"Готово! Заказ оформлен.\nПополнение: {qty} ⭐ на {username}\nСписано: {price_kopecks/100:.2f} ₽\nТекущий баланс: {rub_balance.get(m.from_user.id,0)/100:.2f} ₽")
        return


@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    await m.answer("Получен платёж через встроенные инвойсы, но текущая логика использует внешние способы оплаты. Платёж не обработан.")


if __name__ == "__main__":
    if not settings.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    asyncio.run(dp.start_polling(bot))