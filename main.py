import logging
import re
import json
import requests
import random
import string
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from dotenv import load_dotenv
import os

load_dotenv()

# ============ CONFIGURATION ============
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
QUERY_PRICE = int(os.getenv('QUERY_PRICE', 1))
MAX_DAILY_QUERIES = int(os.getenv('MAX_DAILY_QUERIES', 50))

# ============ DATABASE ============
import sqlite3

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('senzo.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.init_tables()
    
    def init_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                join_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                points INTEGER DEFAULT 0,
                total_queries INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_count INTEGER DEFAULT 0,
                is_blocked BOOLEAN DEFAULT 0,
                last_active DATETIME,
                redeemed_codes TEXT DEFAULT '[]',
                daily_queries INTEGER DEFAULT 0,
                last_query_date DATE
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code_id TEXT PRIMARY KEY,
                code TEXT UNIQUE,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                points INTEGER,
                created_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                expiry_date DATETIME,
                created_by INTEGER,
                is_active BOOLEAN DEFAULT 1
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query_type TEXT,
                query_input TEXT,
                api_used TEXT,
                response_data TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                points_deducted INTEGER
            )
        ''')
        self.conn.commit()
    
    # ---------- User Methods ----------
    def create_user(self, user_id, username, full_name, referral_code=None):
        if not referral_code:
            referral_code = self.generate_referral_code()
        
        try:
            self.cursor.execute(
                'INSERT INTO users (user_id, username, full_name, referral_code) VALUES (?, ?, ?, ?)',
                (user_id, username, full_name, referral_code)
            )
            self.conn.commit()
        except:
            pass
    
    def get_user(self, user_id):
        self.cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()
    
    def generate_referral_code(self):
        while True:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            self.cursor.execute('SELECT referral_code FROM users WHERE referral_code = ?', (code,))
            if not self.cursor.fetchone():
                return code
    
    def get_user_by_referral_code(self, code):
        self.cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (code,))
        result = self.cursor.fetchone()
        return result[0] if result else None
    
    def increment_referral(self, user_id):
        self.cursor.execute('UPDATE users SET referral_count = referral_count + 1, points = points + 1 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def add_points(self, user_id, points):
        self.cursor.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (points, user_id))
        self.conn.commit()
    
    def deduct_points(self, user_id, points):
        self.cursor.execute('UPDATE users SET points = points - ? WHERE user_id = ? AND points >= ?', (points, user_id, points))
        affected = self.cursor.rowcount
        self.conn.commit()
        return affected > 0
    
    def get_user_balance(self, user_id):
        self.cursor.execute('SELECT points FROM users WHERE user_id = ?', (user_id,))
        result = self.cursor.fetchone()
        return result[0] if result else 0
    
    def increment_queries(self, user_id):
        self.cursor.execute('''
            UPDATE users 
            SET total_queries = total_queries + 1,
                daily_queries = CASE 
                    WHEN date(last_query_date) = date('now') THEN daily_queries + 1 
                    ELSE 1 
                END,
                last_query_date = date('now')
            WHERE user_id = ?
        ''', (user_id,))
        self.conn.commit()
    
    def get_daily_queries(self, user_id):
        self.cursor.execute('''
            SELECT daily_queries FROM users 
            WHERE user_id = ? AND date(last_query_date) = date('now')
        ''', (user_id,))
        result = self.cursor.fetchone()
        return result[0] if result else 0
    
    def block_user(self, user_id):
        self.cursor.execute('UPDATE users SET is_blocked = 1 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def unblock_user(self, user_id):
        self.cursor.execute('UPDATE users SET is_blocked = 0 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def get_total_users(self):
        self.cursor.execute('SELECT COUNT(*) FROM users')
        return self.cursor.fetchone()[0]
    
    def get_active_users(self):
        self.cursor.execute('SELECT COUNT(*) FROM users WHERE last_active > datetime("now", "-7 days")')
        return self.cursor.fetchone()[0]
    
    def get_total_queries_today(self):
        self.cursor.execute('SELECT COUNT(*) FROM query_log WHERE date(timestamp) = date("now")')
        return self.cursor.fetchone()[0]
    
    def update_last_active(self, user_id):
        self.cursor.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    # ---------- Redeem Methods ----------
    def create_redeem_code(self, max_uses, points, days, created_by):
        code = 'SENZO_' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        expiry_date = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
        
        self.cursor.execute(
            'INSERT INTO redeem_codes (code_id, code, max_uses, points, expiry_date, created_by) VALUES (?, ?, ?, ?, ?, ?)',
            (code, code, max_uses, points, expiry_date, created_by)
        )
        self.conn.commit()
        return code
    
    def redeem_code(self, code, user_id):
        self.cursor.execute('SELECT code_id, max_uses, used_count, points, expiry_date, is_active FROM redeem_codes WHERE code = ?', (code,))
        result = self.cursor.fetchone()
        
        if not result:
            return None, "CODE_NOT_FOUND"
        
        code_id, max_uses, used_count, points, expiry_date, is_active = result
        
        if not is_active:
            return None, "CODE_INACTIVE"
        if expiry_date and datetime.now() > datetime.fromisoformat(expiry_date):
            return None, "CODE_EXPIRED"
        if used_count >= max_uses:
            return None, "CODE_EXHAUSTED"
        
        # Check if user already redeemed
        self.cursor.execute('SELECT redeemed_codes FROM users WHERE user_id = ?', (user_id,))
        user_data = self.cursor.fetchone()
        if user_data:
            redeemed = json.loads(user_data[0])
            if code_id in redeemed:
                return None, "CODE_ALREADY_REDEEMED"
        
        # Update code usage
        self.cursor.execute('UPDATE redeem_codes SET used_count = used_count + 1 WHERE code_id = ?', (code_id,))
        
        # Award points
        self.cursor.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (points, user_id))
        
        # Add to user's redeemed codes
        redeemed.append(code_id)
        self.cursor.execute('UPDATE users SET redeemed_codes = ? WHERE user_id = ?', (json.dumps(redeemed), user_id))
        
        self.conn.commit()
        return points, "SUCCESS"
    
    def add_query_log(self, user_id, query_type, query_input, api_used, response_data, points_deducted):
        self.cursor.execute(
            'INSERT INTO query_log (user_id, query_type, query_input, api_used, response_data, points_deducted) VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, query_type, query_input, api_used, response_data, points_deducted)
        )
        self.conn.commit()

db = Database()

# ============ API HANDLER ============
def fetch_pakistan_data(query):
    """Fetch from Pakistan API"""
    try:
        # Build URL - query can be number or CNIC
        url = f"https://sim-info-api.wasif-ali.workers.dev/?search={query}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            # Handle different response formats
            if isinstance(data, dict):
                return data, "primary"
            elif isinstance(data, list) and len(data) > 0:
                return data[0], "primary"
        return None, None
    except Exception as e:
        logging.error(f"Pakistan API error: {e}")
        return None, None

def fetch_india_data(query):
    """Fetch from India API"""
    try:
        url = f"https://wasifali-indian-number-info.vercel.app/api?number={query}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and not data.get('error'):
                return data, "primary"
        return None, None
    except Exception as e:
        logging.error(f"India API error: {e}")
        return None, None

# ============ TELEGRAM HANDLERS ============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or ''
    full_name = user.full_name or ''
    
    # Check referral
    referral_code = None
    if context.args and context.args[0].startswith('REF_'):
        referral_code = context.args[0][4:]
        referrer_id = db.get_user_by_referral_code(referral_code)
        if referrer_id and referrer_id != user_id:
            db.increment_referral(referrer_id)
            await update.message.reply_text(
                f"✅ REFERRAL SUCCESSFUL!\n"
                f"You referred @{username} to SENZO SIM Database\n"
                f"💰 +1 Point added to your balance"
            )
    
    # Create user if new
    if not db.get_user(user_id):
        db.create_user(user_id, username, full_name, referral_code)
    
    db.update_last_active(user_id)
    
    # Build keyboard
    keyboard = [
        [InlineKeyboardButton("🇵🇰 Pakistan Database", callback_data='pakistan')],
        [InlineKeyboardButton("🇮🇳 India Database", callback_data='india')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')],
        [InlineKeyboardButton("🔗 Referral System", callback_data='referral')],
        [InlineKeyboardButton("💳 Redeem Code", callback_data='redeem')]
    ]
    
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🚀 Welcome to SENZO SIM Database Bot\n"
        "🔍 Your trusted source for SIM & CNIC information\n"
        "💫 Professional | Fast | Secure\n\n"
        "Use the buttons below to get started!",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data == 'pakistan':
        await query.edit_message_text(
            "🇵🇰 PAKISTAN SIM DATABASE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please enter a Pakistani mobile number or CNIC:\n"
            "📱 Format: 03XX-XXXXXXX\n"
            "🪪 CNIC: XXXXX-XXXXXXX-X\n\n"
            "💡 You can also send the number directly."
        )
        context.user_data['query_type'] = 'pakistan'
    
    elif data == 'india':
        await query.edit_message_text(
            "🇮🇳 INDIA SIM DATABASE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please enter a 10-digit Indian mobile number:\n"
            "📱 Format: XXXXXXXXXX"
        )
        context.user_data['query_type'] = 'india'
    
    elif data == 'profile':
        await show_profile(query, user_id)
    
    elif data == 'referral':
        await show_referral(query, user_id)
    
    elif data == 'redeem':
        await query.edit_message_text(
            "💳 REDEEM PROMOTIONAL CODE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please send the code in this format:\n"
            "`/redeem SENZO_XXXXXX`\n\n"
            "Example: `/redeem SENZO_ABC123`"
        )
    
    elif data == 'admin' and user_id in ADMIN_IDS:
        await show_admin_panel(query, user_id)
    
    elif data == 'back_to_menu':
        await start(update, context)

async def show_profile(query, user_id):
    user = db.get_user(user_id)
    if not user:
        await query.edit_message_text("❌ User not found. Please use /start.")
        return
    
    profile = (
        "👤 YOUR PROFILE - SENZO\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 User ID: {user[0]}\n"
        f"👤 Username: @{user[1] or 'N/A'}\n"
        f"📛 Name: {user[2] or 'N/A'}\n"
        f"📅 Joined: {user[3]}\n"
        f"💰 Points Balance: {user[4]}\n"
        f"📊 Total Queries: {user[5]}\n"
        f"🎯 Referrals: {user[7]}\n"
        f"🔗 Referral Code: {user[6]}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_menu')]]
    await query.edit_message_text(profile, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_referral(query, user_id):
    user = db.get_user(user_id)
    if not user:
        await query.edit_message_text("❌ User not found.")
        return
    
    referral_code = user[6]
    referral_link = f"https://t.me/SENZO_SIM_DB_Bot?start=REF_{referral_code}"
    
    message = (
        "🔗 REFERRAL SYSTEM - SENZO\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Your Referral Code: `{referral_code}`\n"
        f"📊 Total Referrals: {user[7]}\n"
        f"💰 Points Earned: {user[7]} points\n\n"
        "📤 Share your referral link:\n"
        f"{referral_link}\n\n"
        "💡 You earn 1 point for every referral!"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_menu')]]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_panel(query, user_id):
    total_users = db.get_total_users()
    active_users = db.get_active_users()
    queries_today = db.get_total_queries_today()
    
    panel = (
        "⚙️ SENZO ADMIN PANEL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total Users: {total_users}\n"
        f"🟢 Active Users (7d): {active_users}\n"
        f"📱 Queries Today: {queries_today}\n"
        f"💰 Query Price: {QUERY_PRICE} points\n\n"
        "🛠️ Commands:\n"
        "• /createredeem [uses] [points] [days]\n"
        "• /stats - View bot stats\n"
        "• /block [user_id] - Block user\n"
        "• /unblock [user_id] - Unblock user"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_menu')]]
    await query.edit_message_text(panel, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()
    
    # Check if blocked
    user_data = db.get_user(user_id)
    if user_data and user_data[8]:  # is_blocked
        await update.message.reply_text("⚠️ Your account is blocked. Contact admin.")
        return
    
    query_type = context.user_data.get('query_type')
    
    # Pakistan query
    if query_type == 'pakistan' or re.match(r'^03\d{2}-\d{7}$', text) or re.match(r'^\d{5}-\d{7}-\d$', text):
        if re.match(r'^03\d{2}-\d{7}$', text) or re.match(r'^\d{5}-\d{7}-\d$', text):
            await process_query(update, user_id, text, 'pakistan')
        else:
            await update.message.reply_text("❌ Invalid format. Use: 03XX-XXXXXXX or XXXXX-XXXXXXX-X")
    
    # India query
    elif query_type == 'india' or re.match(r'^\d{10}$', text):
        if re.match(r'^\d{10}$', text):
            await process_query(update, user_id, text, 'india')
        else:
            await update.message.reply_text("❌ Invalid format. Use 10-digit number: XXXXXXXXXX")
    
    else:
        await update.message.reply_text(
            "❌ Unknown input.\n"
            "Use /start to see options.\n"
            "Or send a number directly:\n"
            "📱 Pakistan: 03XX-XXXXXXX\n"
            "🪪 CNIC: XXXXX-XXXXXXX-X\n"
            "📱 India: XXXXXXXXXX"
        )

async def process_query(update, user_id, query, country):
    # Check points
    balance = db.get_user_balance(user_id)
    if balance < QUERY_PRICE:
        await update.message.reply_text(
            f"⚠️ INSUFFICIENT POINTS\n"
            f"Balance: {balance} | Cost: {QUERY_PRICE}\n"
            f"Earn points via referrals or redeem codes."
        )
        return
    
    # Check daily limit
    daily = db.get_daily_queries(user_id)
    if daily >= MAX_DAILY_QUERIES:
        await update.message.reply_text(f"⚠️ Daily limit reached ({MAX_DAILY_QUERIES}). Try tomorrow.")
        return
    
    # Deduct points
    if not db.deduct_points(user_id, QUERY_PRICE):
        await update.message.reply_text("❌ Error deducting points.")
        return
    
    await update.message.reply_text("⏳ Processing your query...")
    
    # Fetch data
    if country == 'pakistan':
        result, api_used = fetch_pakistan_data(query)
    else:
        result, api_used = fetch_india_data(query)
    
    if result:
        # Format response
        response = f"✅ DATABASE RESULT - SENZO\n━━━━━━━━━━━━━━━━━━\n"
        response += f"📱 Input: {query}\n"
        
        # Display fields
        for key, value in result.items():
            if value and key not in ['status', 'message']:
                response += f"• {key.capitalize()}: {value}\n"
        
        response += f"\n💰 Points Used: {QUERY_PRICE}"
        
        await update.message.reply_text(response)
        db.increment_queries(user_id)
        db.add_query_log(user_id, country, query, api_used or 'unknown', str(result), QUERY_PRICE)
    else:
        await update.message.reply_text(
            "❌ No data found or API error.\n"
            "Please check the number and try again."
        )
        db.add_points(user_id, QUERY_PRICE)  # Refund

# ============ ADMIN COMMANDS ============
async def admin_create_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("Usage: /createredeem [max_uses] [points] [days]")
        return
    
    try:
        max_uses, points, days = map(int, args)
        code = db.create_redeem_code(max_uses, points, days, user_id)
        await update.message.reply_text(
            f"✅ Code created: `{code}`\n"
            f"Uses: {max_uses} | Points: {points} | Days: {days}"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    await update.message.reply_text(
        f"📊 SENZO STATS\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {db.get_total_users()}\n"
        f"🟢 Active (7d): {db.get_active_users()}\n"
        f"📱 Queries Today: {db.get_total_queries_today()}\n"
        f"💰 Query Price: {QUERY_PRICE}"
    )

async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /block [user_id]")
        return
    
    try:
        target_id = int(args[0])
        db.block_user(target_id)
        await update.message.reply_text(f"✅ User {target_id} blocked.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def admin_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /unblock [user_id]")
        return
    
    try:
        target_id = int(args[0])
        db.unblock_user(target_id)
        await update.message.reply_text(f"✅ User {target_id} unblocked.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def admin_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        await update.message.reply_text("Usage: /redeem SENZO_XXXXXX")
        return
    
    code = args[0]
    result, status = db.redeem_code(code, user_id)
    
    if status == "SUCCESS":
        await update.message.reply_text(
            f"✅ Redeemed successfully!\n"
            f"💰 +{result} points added.\n"
            f"New balance: {db.get_user_balance(user_id)} points"
        )
    elif status == "CODE_NOT_FOUND":
        await update.message.reply_text("❌ Code not found.")
    elif status == "CODE_EXPIRED":
        await update.message.reply_text("❌ Code expired.")
    elif status == "CODE_EXHAUSTED":
        await update.message.reply_text("❌ Code reached max uses.")
    elif status == "CODE_ALREADY_REDEEMED":
        await update.message.reply_text("❌ You already used this code.")
    else:
        await update.message.reply_text("❌ Invalid code.")

# ============ ERROR HANDLER ============
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An error occurred. Please try again later."
        )

# ============ MAIN ============
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redeem", admin_redeem))
    
    # Admin commands
    app.add_handler(CommandHandler("createredeem", admin_create_redeem))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("block", admin_block))
    app.add_handler(CommandHandler("unblock", admin_unblock))
    
    # Handlers
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    logger.info("SENZO Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
