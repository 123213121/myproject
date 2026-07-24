import asyncio
import logging
import os
import sqlite3
import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, TON_WALLET, USDT_TRC20_WALLET, BTC_WALLET, ADMIN_ID
from vpn_api import vpn_manager

# Настройка логирования
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Тарифы
TARIFFS = {
    "1m": {"name": "1 Месяц", "days": 30, "usdt": 2.0, "ton": 0.5, "btc": 0.000035, "stars": 100},
    "3m": {"name": "3 Месяца", "days": 90, "usdt": 5.0, "ton": 1.3, "btc": 0.000085, "stars": 250},
    "1y": {"name": "1 Год", "days": 365, "usdt": 15.0, "ton": 4.0, "btc": 0.000250, "stars": 800},
}

# ==================== СОСТОЯНИЯ (FSM) ====================

class PaymentState(StatesGroup):
    waiting_for_tariff = State()
    waiting_for_hash = State()
    waiting_for_cs2_trade = State()
    waiting_for_steam_acc = State()

class AdminState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()

# ==================== БАЗА ДАННЫХ (SQLite) ====================

def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            days_left INTEGER DEFAULT 0,
            referrer_id INTEGER,
            referrals_count INTEGER DEFAULT 0,
            vpn_key TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str, referrer_id: int = None):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, days_left, referrals_count, vpn_key FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        valid_referrer = None
        if referrer_id and referrer_id != user_id:
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
            if cursor.fetchone():
                valid_referrer = referrer_id

        cursor.execute(
            "INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)",
            (user_id, username, valid_referrer)
        )
        
        if valid_referrer:
            cursor.execute("""
                UPDATE users 
                SET referrals_count = referrals_count + 1, 
                    days_left = days_left + 7 
                WHERE user_id = ?
            """, (valid_referrer,))
            
        conn.commit()
        conn.close()
        return {"days_left": 0, "referrals": 0, "vpn_key": None, "new_ref_id": valid_referrer}

    conn.close()
    return {"days_left": user[1], "referrals": user[2], "vpn_key": user[3], "new_ref_id": None}

def add_subscription_days(user_id: int, days: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET days_left = days_left + ? WHERE user_id = ?", (days, user_id))
    conn.commit()
    conn.close()

def save_vpn_key(user_id: int, vpn_key: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET vpn_key = ? WHERE user_id = ?", (vpn_key, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT days_left, referrals_count, vpn_key FROM users WHERE user_id = ?", (user_id,))
    data = cursor.fetchone()
    conn.close()
    if data:
        return {"days_left": data[0], "referrals": data[1], "vpn_key": data[2]}
    return {"days_left": 0, "referrals": 0, "vpn_key": None}

# ==================== ПРОВЕРКА BLOCKCHAIN HASH ====================

async def verify_crypto_hash(tx_hash: str, crypto_type: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if crypto_type == "usdt":
                res = await client.get(f"https://api.trongrid.io/v1/accounts/{USDT_TRC20_WALLET}/transactions/trc20")
                if res.status_code == 200:
                    data = res.json()
                    for tx in data.get("data", []):
                        if tx.get("transaction_id") == tx_hash:
                            return True
            elif crypto_type == "btc":
                res = await client.get(f"https://blockstream.info/api/tx/{tx_hash}")
                if res.status_code == 200 and res.json().get("status", {}).get("confirmed"):
                    return True
    except Exception as e:
        logging.error(f"Ошибка проверки хэша: {e}")
    return False

# ==================== КЛАВИАТУРЫ ====================

def main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡️ Подключить VPN", callback_data="my_vpn")
    builder.button(text="💳 Купить подписку", callback_data="buy_sub")
    builder.button(text="👥 Реферальная программа", callback_data="referral")
    builder.button(text="📖 Инструкция", callback_data="instructions")
    builder.button(text="💬 Поддержка", url="https://t.me/your_support")
    builder.adjust(1, 2, 2)
    return builder.as_markup()

def back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в меню", callback_data="main_menu")
    return builder.as_markup()

def tariffs_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🪙 Оплата Криптой / Stars", callback_data="select_period")
    builder.button(text="🔫 Скины CS2", callback_data="pay_cs2")
    builder.button(text="🎮 Аккаунт Steam", callback_data="pay_steam")
    builder.button(text="⬅️ Назад", callback_data="main_menu")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Выдать себе +30 дней", callback_data="admin_give_self")
    builder.button(text="👤 Выдать подписку другу/юзеру", callback_data="admin_give_other")
    builder.button(text="⬅️ Главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

# ==================== АДМИН-ПАНЕЛЬ ====================

@dp.message(F.text.lower() == "firdavsbest")
async def open_admin_panel(message: types.Message, state: FSMContext):
    # Проверка прав администратора
    if message.from_user.id != int(ADMIN_ID):
        return

    await state.clear()
    text = (
        "👑 **Админ-панель открыта!**\n\n"
        "Выбери требуемое действие ниже:"
    )
    await message.answer(text, reply_markup=admin_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_give_self")
async def admin_give_self_callback(call: types.CallbackQuery):
    if call.from_user.id != int(ADMIN_ID):
        return
    add_subscription_days(call.from_user.id, 30)
    await call.message.edit_text(
        "✅ **Успешно!** Тебе зачислено **+30 дней** подписки.",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_give_other")
async def admin_give_other_callback(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != int(ADMIN_ID):
        return
    await state.set_state(AdminState.waiting_for_user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="main_menu")
    await call.message.edit_text(
        "👤 **Введите Telegram ID пользователя**, которому нужно выдать подписку:\n\n"
        "*(ID можно узнать через ботов вроде @userinfobot)*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.message(AdminState.waiting_for_user_id)
async def process_admin_user_id(message: types.Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID):
        return
    if not message.text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуй еще раз:")
        return

    target_id = int(message.text.strip())
    await state.update_data(target_user_id=target_id)
    await state.set_state(AdminState.waiting_for_days)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="main_menu")
    await message.answer(
        f"⏳ На сколько **дней** выдать подписку пользователю `{target_id}`?\nВведите число (например: 30):",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.message(AdminState.waiting_for_days)
async def process_admin_days(message: types.Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID):
        return
    if not message.text.isdigit():
        await message.answer("⚠️ Количество дней должно быть числом. Попробуй еще раз:")
        return

    days = int(message.text.strip())
    data = await state.get_data()
    target_id = data.get("target_user_id")
    await state.clear()

    get_or_create_user(target_id, f"User_{target_id}")
    add_subscription_days(target_id, days)

    try:
        await bot.send_message(
            target_id,
            f"🎉 **Вам начислено +{days} дней VIP подписки от администратора!** 🚀\n"
            f"Перейдите в меню «⚡️ Подключить VPN», чтобы получить свой ключ."
        )
    except Exception:
        pass

    await message.answer(
        f"✅ **Успешно!** Пользователю `{target_id}` начислено **+{days} дней** подписки.",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown"
    )

# ==================== ОСНОВНЫЕ ХЭНДЛЕРЫ ====================

@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    args = message.text.split()[1:] if len(message.text.split()) > 1 else None
    referrer_id = int(args[0]) if args and args[0].isdigit() else None
    
    user = get_or_create_user(user_id, username, referrer_id)
    
    if user["new_ref_id"]:
        try:
            await bot.send_message(
                user["new_ref_id"], 
                f"🎉 **Новый реферал!**\nПользователь зарегистрировался по твоей ссылке.\n🎁 Тебе начислено **+7 дней** VPN!"
            )
        except Exception:
            pass

    stats = get_user_stats(user_id)
    text = (
        f"👋 **Привет, {message.from_user.first_name}!**\n\n"
        f"Добро пожаловать в **PRIME VPN** — быстрый и защищенный доступ в интернет.\n\n"
        f"📊 **Твой профиль:**\n"
        f"├ Статус: {'🟢 Активна' if stats['days_left'] > 0 else '🔴 Не активна'}\n"
        f"├ Осталось дней: `{stats['days_left']}`\n"
        f"└ Приглашено друзей: `{stats['referrals']}`\n\n"
        f"Выбери действие ниже 👇"
    )
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    stats = get_user_stats(call.from_user.id)
    text = (
        f"🏠 **Главное меню**\n\n"
        f"📊 **Твой профиль:**\n"
        f"├ Статус: {'🟢 Активна' if stats['days_left'] > 0 else '🔴 Не активна'}\n"
        f"├ Осталось дней: `{stats['days_left']}`\n"
        f"└ Приглашено друзей: `{stats['referrals']}`\n"
    )
    await call.message.edit_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "buy_sub")
async def buy_sub_callback(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "💳 **Выбери удобный способ оплаты:**\n\n"
        "🚀 **1 Месяц** — 2 USDT / 0.5 TON / 100 Stars\n"
        "🔥 **3 Месяца** — 5 USDT / 1.3 TON / 250 Stars *(Скидка 10%)*\n"
        "👑 **1 Год** — 15 USDT / 4 TON / 800 Stars *(Скидка 33%)*\n\n"
        "Принимаем **Криптовалюту**, **Telegram Stars**, **Скины CS2** и **Аккаунты Steam**!"
    )
    await call.message.edit_text(text, reply_markup=tariffs_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "my_vpn")
async def my_vpn_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    username = call.from_user.username or f"id{user_id}"
    stats = get_user_stats(user_id)
    
    if stats["days_left"] <= 0 and not stats["vpn_key"]:
        text = (
            "⚠️ **Подписка не активна!**\n\n"
            "У вас пока нет дней подписки. Оплатите подписку или пригласите друга по реферальной ссылке!"
        )
        await call.message.edit_text(text, reply_markup=tariffs_keyboard(), parse_mode="Markdown")
        return

    if not stats["vpn_key"]:
        await call.message.edit_text("⏳ **Генерируем ваш личный VPN ключ...**", parse_mode="Markdown")
        
        try:
            vpn_key = await vpn_manager.create_client_key(
                user_id=user_id,
                username=username,
                days=stats["days_left"]
            )
        except Exception as e:
            logging.error(f"Ошибка вызова vpn_manager: {e}")
            vpn_key = None

        if vpn_key:
            save_vpn_key(user_id, vpn_key)
            stats["vpn_key"] = vpn_key
        else:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ **Ошибка выдачи ключа!**\nПользователь `{user_id}` имеет `{stats['days_left']}` дней, но `vpn_manager` не вернул ключ."
                )
            except Exception:
                pass

            text = (
                "⚠️ **Ваш VPN включен и активен!**\n\n"
                f"⏳ Осталось дней: `{stats['days_left']}`\n\n"
                "Если ключ не отображается или не подключается, пожалуйста, обратитесь в **Службу Поддержки**."
            )
            await call.message.edit_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")
            return

    text = (
        f"⚡️ **Твой VPN доступ**\n\n"
        f"🔑 **Ключ (нажми, чтобы скопировать):**\n"
        f"`{stats['vpn_key']}`\n\n"
        f"⏳ Осталось дней: `{stats['days_left']}`"
    )
    await call.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

# --- ВЫБОР ПЕРИОДА И ОПЛАТА ---

@dp.callback_query(F.data == "select_period")
async def select_period_cmd(call: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 1 Месяц (30 дней)", callback_data="period_1m")
    builder.button(text="🔥 3 Месяца (90 дней)", callback_data="period_3m")
    builder.button(text="👑 1 Год (365 дней)", callback_data="period_1y")
    builder.button(text="⬅️ Назад", callback_data="buy_sub")
    builder.adjust(1)
    
    text = "🗓 **Выберите срок продления подписки:**"
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("period_"))
async def period_selected_cmd(call: types.CallbackQuery, state: FSMContext):
    period_key = call.data.split("_")[1]
    tariff = TARIFFS.get(period_key, TARIFFS["1m"])
    
    await state.update_data(selected_period=period_key, days=tariff["days"])
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🛡 Trust Wallet (USDT / BTC)", callback_data="crypto_trust")
    builder.button(text="💎 TON", callback_data="crypto_ton")
    builder.button(text="⭐️ Telegram Stars", callback_data="crypto_stars")
    builder.button(text="⬅️ Изменить период", callback_data="select_period")
    builder.adjust(1)
    
    text = (
        f"📅 **Выбран период:** {tariff['name']}\n\n"
        f"🪙 **Выбери удобный способ оплаты:**"
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "crypto_trust")
async def crypto_trust_cmd(call: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 USDT (TRC-20)", callback_data="trust_usdt")
    builder.button(text="₿ Bitcoin (BTC)", callback_data="trust_btc")
    builder.button(text="⬅️ Назад", callback_data="select_period")
    builder.adjust(1)
    
    text = "🛡 **Trust Wallet**\n\nВыбери монету для оплаты:"
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.in_({"trust_usdt", "trust_btc"}))
async def trust_coin_cmd(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    period_key = data.get("selected_period", "1m")
    tariff = TARIFFS.get(period_key, TARIFFS["1m"])
    
    is_usdt = call.data == "trust_usdt"
    coin = "USDT (TRC-20)" if is_usdt else "Bitcoin (BTC)"
    wallet = USDT_TRC20_WALLET if is_usdt else BTC_WALLET
    amount = f"{tariff['usdt']} USDT" if is_usdt else f"{tariff['btc']} BTC"
    
    await state.update_data(crypto_type="usdt" if is_usdt else "btc")
    await state.set_state(PaymentState.waiting_for_hash)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="buy_sub")
    
    text = (
        f"🌐 **Оплата через Trust Wallet — {coin}**\n"
        f"📌 Тариф: **{tariff['name']}**\n\n"
        f"💵 Сумма к оплате: `{amount}`\n"
        f"📫 Адрес кошелька (нажми для копирования):\n`{wallet}`\n\n"
        f"📌 **Инструкция:**\n"
        f"1. Переведи указанную сумму на кошелек.\n"
        f"2. Скопируй **Хэш транзакции (TxID)**.\n"
        f"3. Отправь Хэш сюда в чат 👇"
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(PaymentState.waiting_for_hash)
async def process_hash_input(message: types.Message, state: FSMContext):
    tx_hash = message.text.strip()
    data = await state.get_data()
    crypto_type = data.get("crypto_type", "usdt")
    days = data.get("days", 30)

    msg = await message.answer("🔍 Проверяю транзакцию в блокчейне... Пожалуйста, подождите.")
    is_valid = await verify_crypto_hash(tx_hash, crypto_type)

    if is_valid:
        add_subscription_days(message.from_user.id, days)
        await state.clear()
        await msg.edit_text(
            f"🎉 **Транзакция подтверждена!**\n\n"
            f"Вам начислено **+{days} дней VPN**! 🚀\n"
            f"Перейдите в «⚡️ Подключить VPN», чтобы получить ваш ключ.",
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Попробовать снова", callback_data="crypto_trust")
        builder.button(text="⬅️ В меню", callback_data="main_menu")
        builder.adjust(1)
        await msg.edit_text(
            "❌ **Транзакция не найдена или не проведена.**\n\n"
            "Убедитесь, что отправили верный Хэш (TxID), или подождите 1–2 минуты.",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

@dp.callback_query(F.data == "crypto_ton")
async def crypto_ton_cmd(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    period_key = data.get("selected_period", "1m")
    tariff = TARIFFS.get(period_key, TARIFFS["1m"])
    
    nanoton = int(tariff["ton"] * 1_000_000_000)

    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Оплатить в TON Hub / Tonkeeper", url=f"https://tonhub.com/transfer/{TON_WALLET}?amount={nanoton}")
    builder.button(text="⬅️ Назад", callback_data="select_period")
    builder.adjust(1)

    text = (
        f"💎 **Оплата через TON**\n"
        f"📌 Тариф: **{tariff['name']}**\n\n"
        f"💵 Сумма: `{tariff['ton']} TON`\n"
        f"📫 Кошелек:\n`{TON_WALLET}`"
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "crypto_stars")
async def crypto_stars_cmd(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    period_key = data.get("selected_period", "1m")
    tariff = TARIFFS.get(period_key, TARIFFS["1m"])

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"PRIME VPN — {tariff['name']}",
        description=f"Премиум подписка на высокоскоростной VPN на {tariff['days']} дней",
        payload=f"vpn_sub_{period_key}_stars",
        currency="XTR",
        prices=[types.LabeledPrice(label=f"Подписка ({tariff['name']})", amount=tariff["stars"])],
        start_parameter="vpn-stars-pay"
    )
    await call.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    days = 30
    if "3m" in payload:
        days = 90
    elif "1y" in payload:
        days = 365

    add_subscription_days(message.from_user.id, days)
    await message.answer(
        f"🎉 **Спасибо за оплату Telegram Stars!**\n\n"
        f"Вам зачислено **+{days} дней Премиум подписки**! 🚀",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )

# --- СКИНЫ CS2 И STEAM ---

@dp.callback_query(F.data == "pay_cs2")
async def pay_cs2_cmd(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(PaymentState.waiting_for_cs2_trade)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="buy_sub")
    text = (
        "🔫 **Оплата скинами CS2**\n\n"
        "Отправьте в чат вашу **Трейд-ссылку Steam**."
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(PaymentState.waiting_for_cs2_trade)
async def process_cs2_input(message: types.Message, state: FSMContext):
    trade_link = message.text.strip()
    await state.clear()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔫 **Заявка CS2!**\n👤 От: `{message.from_user.id}`\n🔗 Ссылка:\n`{trade_link}`"
        )
    except Exception:
        pass

    await message.answer("✅ **Заявка принята!** Проверим и активируем подписку в течение 10–15 минут.", reply_markup=main_keyboard())

@dp.callback_query(F.data == "pay_steam")
async def pay_steam_cmd(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(PaymentState.waiting_for_steam_acc)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="buy_sub")
    text = "🎮 **Обмен Аккаунтом Steam**\n\nНапишите ссылку на профиль и описание аккаунта 👇"
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(PaymentState.waiting_for_steam_acc)
async def process_steam_input(message: types.Message, state: FSMContext):
    acc_info = message.text.strip()
    await state.clear()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🎮 **Заявка Steam!**\n👤 От: `{message.from_user.id}`\n📜 Данные:\n`{acc_info}`"
        )
    except Exception:
        pass

    await message.answer("✅ **Заявка отправлена!** Свяжемся с вами в ближайшее время.", reply_markup=main_keyboard())

# --- РЕФЕРАЛЫ И ИНСТРУКЦИЯ ---

@dp.callback_query(F.data == "referral")
async def referral_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    stats = get_user_stats(user_id)

    text = (
        f"🤝 **Реферальная программа**\n\n"
        f"Приглашай друзей и получай **+7 дней** VPN за каждого реферала!\n\n"
        f"📊 **Твоя статистика:**\n"
        f"├ Приглашено: `{stats['referrals']}` чел.\n"
        f"└ Бонусов получено: `{stats['referrals'] * 7}` дней\n\n"
        f"🔗 **Твоя реферальная ссылка:**\n`{ref_link}`"
    )
    await call.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "instructions")
async def instructions_callback(call: types.CallbackQuery):
    text = (
        "📖 **Инструкция по настройке**\n\n"
        "Спасибо, что используете **PRIME VPN**!\n"
        "Если у вас возникли вопросы, обратитесь в службу поддержки."
    )
    await call.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

# ==================== ЗАПУСК ВЕБ-СЕРВЕРА И БОТА ====================

async def handle(request):
    return web.Response(text="Bot Prime VPN is running!")

async def main():
    init_db()

    # 1. Запуск веб-сервера (для Render)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # 2. Запуск бота (после всех объявленных хэндлеров)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
