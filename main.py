import asyncio
import uvicorn
import bcrypt
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
from typing import Optional, List
from contextlib import asynccontextmanager


# --- AIOGRAM (Telegram Bot) IMPORTS ---
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, LabeledPrice, PreCheckoutQuery

# ================= CONFIGURATION =================
from dotenv import load_dotenv
import os

load_dotenv()  # Загружает переменные из .env

# --- DB Settings ---
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1234")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME", "amigo")

DB_DSN = os.getenv("DATABASE_URL")

# --- Telegram / Payment ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# --- WEBHOOK SETTINGS ---
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL_FULL = WEBHOOK_URL + WEBHOOK_PATH

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
router = Router()
dp.include_router(router)

# --- Shared DB Pool Container ---
class DBContainer:
    pool = None

db = DBContainer()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="❤️ Найти пару",
                web_app=WebAppInfo(url=os.getenv("WEBAPP_URL"))
            )]
        ]
    )
    await message.answer(
        "Привет! Добро пожаловать в Dating App.\nНажми кнопку ниже 👇",
        reply_markup=kb
    )
# --- PAYMENT HANDLERS (Aiogram) ---
@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    if not db.pool:
        return
    user_id = message.from_user.id
    total_amount = message.successful_payment.total_amount // 100
    currency = message.successful_payment.currency
    payload = message.successful_payment.invoice_payload
    if payload == "premium_upgrade":
        print(f"💰 Payment received from {user_id}: {total_amount} {currency}")
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_premium = TRUE WHERE telegram_id = $1", user_id)
        await message.answer("🎉 Поздравляем! Ваш Premium активирован. Перезагрузите приложение, чтобы увидеть изменения.")

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
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT FALSE")
                print("🔹 Migration: Added is_premium column")
            except asyncpg.exceptions.DuplicateColumnError:
                pass
            default_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode('utf-8')
            await conn.execute("""
                INSERT INTO admins (email, password_hash) 
                VALUES ($1, $2) 
                ON CONFLICT (email) DO NOTHING
            """, ADMIN_EMAIL, default_hash)

    except Exception as e:
        print(f"❌ DB Connection Error: {e}")

    # 2. Webhook Setup
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL_FULL)
        print(f"🌐 Webhook set to: {WEBHOOK_URL_FULL}")
    except Exception as e:
        print(f"❌ Webhook Error: {e}")

    yield
    
    # 3. Shutdown
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

# --- WEBHOOK ENDPOINT ---
@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: dict):
    telegram_update = types.Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}
# --- Остальные Endpoints оставляем без изменений ---
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
    try:
        prices = [LabeledPrice(label="Premium Подписка", amount=590 * 100)] 
        invoice_link = await bot.create_invoice_link(
            title="Amigo Premium",
            description="Доступ к фильтрам и VIP функциям",
            payload="premium_upgrade",
            provider_token=PAYMENT_TOKEN,
            currency="KZT",
            prices=prices,
            photo_url="https://cdn-icons-png.flaticon.com/512/1458/1458260.png",
            photo_width=512,
            photo_height=512
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        print(f"Invoice Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/candidates")
async def get_candidates(
    telegram_id: int, 
    city: Optional[str] = None, 
    min_age: int = 18, 
    max_age: int = 99
):
    async with app.state.pool.acquire() as conn:
        requester = await conn.fetchrow("SELECT is_premium FROM users WHERE telegram_id = $1", telegram_id)
        is_premium = requester['is_premium'] if requester else False

        sql = """
            SELECT * FROM users 
            WHERE telegram_id != $1 
            AND telegram_id NOT IN (SELECT to_user FROM likes WHERE from_user = $1)
        """
        params = [telegram_id]
        param_idx = 2

        if is_premium:
            if city and city != "all":
                sql += f" AND city = ${param_idx}"
                params.append(city)
                param_idx += 1
            if min_age > 18:
                sql += f" AND age >= ${param_idx}"
                params.append(min_age)
                param_idx += 1
            if max_age < 99:
                sql += f" AND age <= ${param_idx}"
                params.append(max_age)
                param_idx += 1
        
        sql += " LIMIT 20"
        rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]

@app.post("/like")
async def like_user(like: LikeRequest):
    async with app.state.pool.acquire() as conn:
        await conn.execute("INSERT INTO likes (from_user, to_user) VALUES ($1, $2) ON CONFLICT DO NOTHING", like.from_user, like.to_user)
        mutual = await conn.fetchrow("SELECT * FROM likes WHERE from_user = $1 AND to_user = $2", like.to_user, like.from_user)
        if mutual:
            await conn.execute("INSERT INTO matches (user_1, user_2) VALUES ($1, $2) ON CONFLICT DO NOTHING", like.from_user, like.to_user)
            return {"is_match": True}
    return {"is_match": False}

@app.get("/matches")
async def get_matches(telegram_id: int):
    query = """
    SELECT u.telegram_id as user_id, u.name, u.username, u.photo 
    FROM matches m
    JOIN users u ON (u.telegram_id = m.user_1 OR u.telegram_id = m.user_2)
    WHERE (m.user_1 = $1 OR m.user_2 = $1) AND u.telegram_id != $1
    """
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(query, telegram_id)
        return [dict(row) for row in rows]

@app.post("/admin/login")
async def admin_login(creds: AdminLogin):
    async with app.state.pool.acquire() as conn:
        admin = await conn.fetchrow("SELECT password_hash FROM admins WHERE email = 'admin@amigo.com'")
        if not admin:
            bcrypt.checkpw(b"fake", b"$2b$12$fakehash......................") 
            raise HTTPException(status_code=401)
        if not bcrypt.checkpw(creds.password.encode('utf-8'), admin['password_hash'].encode('utf-8')):
            raise HTTPException(status_code=401)
        return {"status": "authorized"}

@app.get("/admin/users")
async def get_all_users():
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users ORDER BY created_at DESC")
        return [dict(row) for row in rows]

if __name__ == "__main__":
    uvicorn.run(
    app,
    host="0.0.0.0",
    port=int(os.getenv("PORT", 8080))
)
