import asyncio
import uvicorn
import bcrypt
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
from typing import Optional, List
from contextlib import asynccontextmanager
import logging
from datetime import datetime, timedelta

# ... existing imports ...
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, LabeledPrice, PreCheckoutQuery
from dotenv import load_dotenv
import os

load_dotenv()

# ... existing config ...
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1234")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME", "amigo")
DB_DSN = os.getenv("DATABASE_URL")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://21074928.mynewapp-1ph.pages.dev")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL_FULL = WEBHOOK_URL + WEBHOOK_PATH

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ... existing models ...
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

# ... bot setup ...
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

class DBContainer:
    pool = None

db = DBContainer()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="❤️ Найти пару",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )]
        ]
    )
    await message.answer(
        "Привет! Добро пожаловать в Dating App.\nНажми кнопку ниже 👇",
        reply_markup=kb
    )

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    logger.info(f"Pre-checkout query from {pre_checkout_query.from_user.id}")
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    if not db.pool:
        logger.error("DB pool not available")
        return
    
    user_id = message.from_user.id
    payment = message.successful_payment
    
    total_amount = payment.total_amount
    currency = payment.currency
    payload = payment.invoice_payload
    
    logger.info(f"💰 Payment: user_id={user_id}, amount={total_amount}, currency={currency}, payload={payload}")
    
    # Только XTR (звёзды)
    if currency == "XTR" and total_amount == 100:
        try:
            async with db.pool.acquire() as conn:
                premium_until = datetime.utcnow() + timedelta(days=30)
                await conn.execute(
                    "UPDATE users SET is_premium = TRUE, is_premium_until = $2 WHERE telegram_id = $1", 
                    user_id, premium_until
                )
            logger.info(f"✅ Premium activated for user {user_id} until {premium_until}")
            await message.answer("🎉 Поздравляем! Ваш Premium активирован на 30 дней. Перезагрузите приложение, чтобы увидеть изменения.")
        except Exception as e:
            logger.error(f"Error updating premium: {e}")
            await message.answer("❌ Ошибка активации. Пожалуйста, свяжитесь с поддержкой.")
    else:
        logger.warning(f"Invalid payment: currency={currency}, amount={total_amount}")
        await message.answer("❌ Некорректный платёж")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await asyncpg.create_pool(DB_DSN)
        db.pool = pool
        app.state.pool = pool
        logger.info("✅ DB Connected")

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
                    is_premium_until TIMESTAMP,
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
                logger.info("🔹 Migration: Added is_premium column")
            except asyncpg.exceptions.DuplicateColumnError:
                pass
            
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN is_premium_until TIMESTAMP")
                logger.info("🔹 Migration: Added is_premium_until column")
            except asyncpg.exceptions.DuplicateColumnError:
                pass
            
            default_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode('utf-8')
            await conn.execute("""
                INSERT INTO admins (email, password_hash) 
                VALUES ($1, $2) 
                ON CONFLICT (email) DO NOTHING
            """, ADMIN_EMAIL, default_hash)

    except Exception as e:
        logger.error(f"❌ DB Connection Error: {e}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL_FULL)
        logger.info(f"🌐 Webhook set to: {WEBHOOK_URL_FULL}")
    except Exception as e:
        logger.error(f"❌ Webhook Error: {e}")

    yield
    
    if hasattr(app.state, 'pool'):
        await app.state.pool.close()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_URL, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Backend is running"}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: dict):
    telegram_update = types.Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}

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
        result = dict(row)
        result['is_premium_active'] = await is_premium_active(telegram_id, conn)
        return result

@app.post("/create_invoice")
async def create_stars_invoice(req: CreateInvoiceRequest):
    """Создать инвойс для оплаты 100 звёзд"""
    try:
        # Важно: amount в копейках для XTR это сами звёзды (100 звёзд = 100)
        prices = [LabeledPrice(label="Premium Подписка 100 звёзд", amount=100)]
        
        invoice_link = await bot.create_invoice_link(
            title="Amigo Premium",
            description="100 Telegram Stars за премиум доступ",
            payload="premium_upgrade_stars",
            provider_token="",  # Пусто для звёзд!
            currency="XTR",     # Только XTR для звёзд
            prices=prices,
            photo_url="https://cdn-icons-png.flaticon.com/512/1458/1458260.png",
            photo_width=512,
            photo_height=512
        )
        logger.info(f"Invoice created for user {req.telegram_id}: {invoice_link}")
        return {"invoice_link": invoice_link}
    except Exception as e:
        logger.error(f"Stars Invoice Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/candidates")
async def get_candidates(
    telegram_id: int, 
    city: Optional[str] = None, 
    min_age: int = 18, 
    max_age: int = 99,
    goal: Optional[str] = None
):
    async with app.state.pool.acquire() as conn:
        requester = await conn.fetchrow("SELECT gender, orientation, is_premium, is_premium_until FROM users WHERE telegram_id = $1", telegram_id)
        if not requester:
            raise HTTPException(status_code=404, detail="User not found")
        
        requester_gender = requester['gender']
        requester_orientation = requester['orientation']
        is_premium = await is_premium_active(telegram_id, conn)

        sql = """
            SELECT * FROM users 
            WHERE telegram_id != $1 
            AND telegram_id NOT IN (SELECT to_user FROM likes WHERE from_user = $1)
        """
        params = [telegram_id]
        param_idx = 2

        if requester_orientation == 'hetero':
            opposite_gender = 'female' if requester_gender == 'male' else 'male'
            sql += f" AND gender = ${param_idx}"
            params.append(opposite_gender)
            param_idx += 1
        elif requester_orientation == 'gay':
            sql += f" AND gender = ${param_idx}"
            params.append(requester_gender)
            param_idx += 1

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
            if goal and goal != 'all':
                sql += f" AND goal = ${param_idx}"
                params.append(goal)
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
        admin = await conn.fetchrow("SELECT password_hash FROM admins WHERE email = $1", creds.email)
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

@app.delete("/admin/delete_user")
async def delete_user(telegram_id: int):
    async with app.state.pool.acquire() as conn:
        await conn.execute("DELETE FROM likes WHERE from_user = $1 OR to_user = $1", telegram_id)
        await conn.execute("DELETE FROM matches WHERE user_1 = $1 OR user_2 = $1", telegram_id)
        await conn.execute("DELETE FROM users WHERE telegram_id = $1", telegram_id)
    return {"status": "deleted", "telegram_id": telegram_id}

@app.put("/me")
async def update_profile(user: UserProfile):
    query = """
    UPDATE users 
    SET name=$2, age=$3, gender=$4, orientation=$5, city=$6, goal=$7, photo=$8, bio=$9
    WHERE telegram_id = $1
    RETURNING *
    """
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(query, user.telegram_id, user.name, user.age, 
                                  user.gender, user.orientation, user.city, user.goal, user.photo, user.bio)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(row)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
