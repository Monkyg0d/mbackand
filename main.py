import os
import asyncio
import bcrypt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
from typing import Optional
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, LabeledPrice, PreCheckoutQuery

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME")

DB_DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# --- Models ---
class UserProfile(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    name: str
    age: int
    gender: str
    orientation: str
    country: str
    city: str
    goal: str
    photo: Optional[str] = None
    bio: Optional[str] = None
    is_premium: bool = False

class LikeRequest(BaseModel):
    from_user: int
    to_user: int

class AdminLogin(BaseModel):
    email: str
    password: str

class CreateInvoiceRequest(BaseModel):
    telegram_id: int

# --- Bot Setup ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Shared DB Pool Container ---
class DBContainer:
    pool = None

db = DBContainer()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Найти пару", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        "Привет! Добро пожаловать в Dating App.\nНажми кнопку ниже, чтобы начать знакомства!", 
        reply_markup=kb
    )

# --- PAYMENT HANDLERS ---
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    if not db.pool:
        return
    
    user_id = message.from_user.id
    total_amount = message.successful_payment.total_amount // 100
    payload = message.successful_payment.invoice_payload

    if payload == "premium_upgrade":
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_premium = TRUE WHERE telegram_id = $1", user_id)
        await message.answer("🎉 Поздравляем! Ваш Premium активирован.")

# --- FastAPI Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await asyncpg.create_pool(DB_DSN)
        db.pool = pool
        app.state.pool = pool
        print("✅ DB Connected")

        async with app.state.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    name TEXT,
                    age INT,
                    gender TEXT,
                    orientation TEXT,
                    country TEXT,
                    city TEXT,
                    goal TEXT,
                    photo TEXT,
                    bio TEXT,
                    is_premium BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS likes (
                    from_user BIGINT,
                    to_user BIGINT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (from_user, to_user)
                );
                CREATE TABLE IF NOT EXISTS matches (
                    user_1 BIGINT,
                    user_2 BIGINT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (user_1, user_2)
                );
                CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE,
                    password_hash TEXT
                );
            """)

            # Default admin
            default_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            await conn.execute("""
                INSERT INTO admins (email, password_hash) 
                VALUES ($1, $2) 
                ON CONFLICT (email) DO NOTHING
            """, ADMIN_EMAIL, default_hash)

    except Exception as e:
        print(f"❌ DB Connection Error: {e}")

    # Webhook setup for Railway
    # Railway предоставляет динамический порт через os.environ["PORT"]
    RAILWAY_PORT = int(os.getenv("PORT", 8000))
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # добавь в env: https://<твой_project>.railway.app/webhook

    await bot.delete_webhook()
    await bot.set_webhook(WEBHOOK_URL)

    yield

    if hasattr(app.state, 'pool'):
        await app.state.pool.close()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Endpoints ---
@app.post("/register")
async def register(user: UserProfile):
    query = """
    INSERT INTO users (telegram_id, username, first_name, name, age, gender, orientation, country, city, goal, photo, bio, is_premium)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (telegram_id) DO UPDATE 
    SET name=$4, age=$5, gender=$6, orientation=$7, city=$9, goal=$10, photo=$11, bio=$12
    RETURNING telegram_id
    """
    async with app.state.pool.acquire() as conn:
        await conn.execute(query, user.telegram_id, user.username, user.first_name, user.name, user.age, 
                           user.gender, user.orientation, user.country, user.city, user.goal, user.photo, user.bio, user.is_premium)
    return {"status": "ok"}

@app.get("/me")
async def get_me(telegram_id: int):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(row)

@app.post("/create_invoice")
async def create_invoice(req: CreateInvoiceRequest):
    prices = [LabeledPrice(label="Premium Подписка", amount=590 * 100)]
    invoice_link = await bot.create_invoice_link(
        title="Amigo Premium",
        description="Доступ к фильтрам и VIP функциям",
        payload="premium_upgrade",
        provider_token=PAYMENT_TOKEN,
        currency="KZT",
        prices=prices
    )
    return {"invoice_link": invoice_link}

@app.post("/webhook")
async def webhook(update: dict):
    from aiogram.types import Update
    await dp.process_update(Update(**update))
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
