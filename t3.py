# bot61.py
# Full bot with xRocket claim-link withdrawals (Option A)
# -------------------------------------------------------
# Requirements (Termux):
#   pip install pyrogram tgcrypto aiohttp
#
# Notes:
# - Payment mode "xrocket_link" generates a claim/check URL on approval.
# - Payment "channel" is used to post the pending withdrawal + admin buttons.
# - Admins can set base_url, api_key, and path for xRocket in Settings.
# - Tables are created/migrated automatically at startup.

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

import aiohttp

# =========================
# HARD-CODED TELEGRAM KEYS
# =========================
API_ID = 24582389
API_HASH = "040b8ff1d81d66650ed47a2810022f2d"
BOT_TOKEN = "8328948300:AAG9ZoZJAz_g5WdY5F8ezE12VwF1Yu6ogtU"

# =========================
# DB & DEFAULT SETTINGS
# =========================
DB_PATH = "bot.db"

DEFAULTS = {
    "currency": "USDT",
    "min_withdraw": "0.5",
    "payment_channel": "@your_payment_channel",   # set in Admin → Settings
    "payment_mode": "xrocket_link",               # "manual" or "xrocket_link"
    "owner_bypass": "1",
    # xRocket
    "xr_base_url": "https://pay.xrocket.tg/api",
    "xr_api_key": "",               # put actual key via Admin → Settings
    "xr_path": "/check/create",     # endpoint path (claim/check creation)
}

SEED_OWNER_ID = 6217495166

# =========================
# DB HELPERS
# =========================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_tables_and_settings():
    con = db(); cur = con.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        referred_by INTEGER
    )
    """)

    # balances
    cur.execute("""
    CREATE TABLE IF NOT EXISTS balances(
        user_id INTEGER PRIMARY KEY,
        balance REAL NOT NULL DEFAULT 0
    )
    """)

    # referrals
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals(
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL UNIQUE,
        credited INTEGER NOT NULL DEFAULT 0
    )
    """)

    # settings
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # required chats
    cur.execute("""
    CREATE TABLE IF NOT EXISTS required_chats(
        username TEXT NOT NULL UNIQUE
    )
    """)

    # admins
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        is_owner INTEGER NOT NULL DEFAULT 0
    )
    """)

    # withdrawals (with all fields we need)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        method TEXT,
        channel_chat_id TEXT,
        channel_message_id TEXT,
        external_id TEXT,
        external_link TEXT,
        extra_json TEXT
    )
    """)

    # defaults
    for k, v in DEFAULTS.items():
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))

    # seed owner
    cur.execute("SELECT COUNT(1) AS c FROM admins")
    c = cur.fetchone()["c"]
    if c == 0:
        cur.execute("INSERT OR IGNORE INTO admins(user_id, first_name, is_owner) VALUES(?,?,1)",
                    (SEED_OWNER_ID, None))

    con.commit(); con.close()

def get_setting(key: str, fallback: str = "") -> str:
    con = db(); cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row["value"] if row else fallback

def set_setting(key: str, value: str):
    con = db(); cur = con.cursor()
    cur.execute("""
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    con.commit(); con.close()

def normalize_username(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return x
    if x.startswith("https://t.me/"):
        x = x.split("https://t.me/")[-1]
    if x.startswith("@"):
        x = x[1:]
    return "@" + x

def get_required_chats():
    con = db(); cur = con.cursor()
    cur.execute("SELECT username FROM required_chats ORDER BY rowid ASC")
    rows = cur.fetchall()
    con.close()
    return [r["username"] for r in rows]

def add_required_chat(username: str) -> bool:
    u = normalize_username(username)
    if u == "@":
        return False
    con = db(); cur = con.cursor()
    try:
        cur.execute("INSERT INTO required_chats(username) VALUES(?)", (u,))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def remove_required_chat(username: str) -> bool:
    u = normalize_username(username)
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM required_chats WHERE username=?", (u,))
    changed = cur.rowcount > 0
    con.commit(); con.close()
    return changed

def is_admin(user_id: int) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id=? LIMIT 1", (user_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def is_owner(user_id: int) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("SELECT is_owner FROM admins WHERE user_id=? LIMIT 1", (user_id,))
    row = cur.fetchone()
    con.close()
    return bool(row and row["is_owner"] == 1)

def add_admin(user_id: int, first_name: str) -> bool:
    con = db(); cur = con.cursor()
    try:
        cur.execute("INSERT INTO admins(user_id, first_name, is_owner) VALUES(?,?,0)", (user_id, first_name))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def remove_admin(user_id: int) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM admins WHERE user_id=? AND is_owner=0", (user_id,))
    changed = cur.rowcount > 0
    con.commit(); con.close()
    return changed

def transfer_ownership(old_owner_id: int, new_owner_id: int, new_owner_name: str):
    con = db(); cur = con.cursor()
    # Demote old owner
    cur.execute("UPDATE admins SET is_owner=0 WHERE user_id=?", (old_owner_id,))
    # Add/Promote new owner
    cur.execute("""
        INSERT INTO admins(user_id, first_name, is_owner) VALUES(?,?,1)
        ON CONFLICT(user_id) DO UPDATE SET is_owner=1
    """, (new_owner_id, new_owner_name))
    con.commit(); con.close()

def ensure_user(user_id: int, first_name: str):
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id, first_name) VALUES(?, ?)", (user_id, first_name))
    cur.execute("INSERT OR IGNORE INTO balances(user_id, balance) VALUES(?, 0)", (user_id,))
    con.commit(); con.close()

def get_balance(user_id: int) -> float:
    con = db(); cur = con.cursor()
    cur.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return float(row["balance"]) if row else 0.0

def add_balance(user_id: int, amount: float):
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO balances(user_id, balance) VALUES(?, 0)", (user_id,))
    cur.execute("UPDATE balances SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    con.commit(); con.close()

def deduct_balance(user_id: int, amount: float) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row or float(row["balance"]) < amount:
        con.close()
        return False
    cur.execute("UPDATE balances SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    con.commit(); con.close()
    return True

def create_withdrawal(user_id: int, amount: float, method: str) -> int:
    con = db(); cur = con.cursor()
    cur.execute(
        "INSERT INTO withdrawals(user_id, amount, status, created_at, method) VALUES(?,?,?,?,?)",
        (user_id, amount, "pending", datetime.now(timezone.utc).isoformat(), method)
    )
    wd_id = cur.lastrowid
    con.commit(); con.close()
    return wd_id

def set_withdrawal_status(wd_id: int, status: str):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, wd_id))
    con.commit(); con.close()

def set_withdrawal_channel_message(wd_id: int, chat_id: int, message_id: int):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE withdrawals SET channel_chat_id=?, channel_message_id=? WHERE id=?",
                (str(chat_id), str(message_id), wd_id))
    con.commit(); con.close()

def set_withdrawal_external(wd_id: int, external_id: str, external_link: str, extra: dict):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE withdrawals SET external_id=?, external_link=?, extra_json=? WHERE id=?",
                (external_id, external_link, json.dumps(extra or {}, ensure_ascii=False), wd_id))
    con.commit(); con.close()

def get_withdrawal(wd_id: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,))
    row = cur.fetchone()
    con.close()
    return row

# =========================
# xROCKET HELPERS
# =========================
async def xr_create_claim_link(user_id: int, amount: float):
    """Creates a multi-cheque link on xRocket"""

    url = "https://pay.xrocket.tg/multi-cheque"
    headers = {
        "Content-Type": "application/json",
        "Rocket-Pay-Key": get_setting("xr_api_key", "")
    }

    # we treat amount as cheque per user, 1 user = 1 cheque
    payload = {
        "currency": "USDT",   # or "TONCOIN" if you are paying in TON
        "chequePerUser": amount,
        "usersNumber": 1,
        "description": f"Withdrawal for user {user_id}",
        "refProgram": 0,
        "sendNotifications": False,
        "enableCaptcha": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if data.get("success"):
                    return True, data.get("data", {})
                else:
                    return False, {"error": data.get("message", "xRocket error")}
    except Exception as e:
      return False, {"error": str(e)}

# =========================
# BOT
# =========================
app = Client("forcejoin-bot61", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

pending_admin_text = {}  # user_id -> dict(action=..., meta=...)
pending_owner_text = {} # user_id -> dict(action=..., meta=...)

def user_menu(is_admin_flag: bool, is_owner_flag: bool):
    """Generates the main menu keyboard, with Admin and Owner buttons."""
    rows = [
        [InlineKeyboardButton("💰 Balance", callback_data="m_balance")],
        [InlineKeyboardButton("👥 Refer & Earn", callback_data="m_refer")],
        [InlineKeyboardButton("📤 Withdraw", callback_data="m_withdraw")],
        [InlineKeyboardButton("ℹ Help", callback_data="m_help")]
    ]
    # Add Admin button if the user is an admin or owner
    if is_admin_flag or is_owner_flag:
        admin_button = InlineKeyboardButton("🛠 Admin", callback_data="ad_home")
        rows.append([admin_button])

    # Add Owner button only if the user is the owner
    if is_owner_flag:
        owner_button = InlineKeyboardButton("👑 Owner", callback_data="owner_home")
        # Find the row with the Admin button to add the Owner button next to it
        if rows[-1] and rows[-1][0].callback_data == 'ad_home':
            rows[-1].append(owner_button)
        else:
            rows.append([owner_button])

    return InlineKeyboardMarkup(rows)

def join_menu():
    btns = []
    for u in get_required_chats():
        uname = u[1:] if u.startswith("@") else u
        label = f"📢 Join {uname}"
        btns.append([InlineKeyboardButton(label, url=f"https://t.me/{uname}")])
    btns.append([InlineKeyboardButton("✅ I've Joined", callback_data="m_check_join")])
    return InlineKeyboardMarkup(btns)

def admin_home_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Required List", callback_data="ad_list")],
        [InlineKeyboardButton("➕ Add Required", callback_data="ad_add"),
         InlineKeyboardButton("➖ Remove Required", callback_data="ad_remove")],
        [InlineKeyboardButton("💳 Balances", callback_data="ad_balances")],
        [InlineKeyboardButton("⚙ Settings", callback_data="ad_settings")],
        [InlineKeyboardButton("⏪ Back to Menu", callback_data="ad_to_user")]
    ])

def owner_home_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin", callback_data="owner_add_admin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="owner_remove_admin")],
        [InlineKeyboardButton("↪️ Transfer Ownership", callback_data="owner_transfer_ownership")],
        [InlineKeyboardButton("⏪ Back to Menu", callback_data="owner_to_user")]
    ])

def admin_settings_menu():
    mode = get_setting("payment_mode", DEFAULTS["payment_mode"])
    pay_channel = get_setting("payment_channel", DEFAULTS["payment_channel"])
    currency = get_setting("currency", DEFAULTS["currency"])
    min_wd = get_setting("min_withdraw", DEFAULTS["min_withdraw"])
    xr_base = get_setting("xr_base_url", DEFAULTS["xr_base_url"])
    xr_key = get_setting("xr_api_key", "")
    xr_path = get_setting("xr_path", DEFAULTS["xr_path"])

    rows = [
        [InlineKeyboardButton(f"Payment Mode: {mode}", callback_data="noop")],
        [InlineKeyboardButton("🔁 Toggle Mode", callback_data="ad_toggle_mode")],
        [InlineKeyboardButton(f"Channel: {pay_channel}", callback_data="noop")],
        [InlineKeyboardButton("✏ Set Channel", callback_data="ad_set_channel")],
        [InlineKeyboardButton(f"Currency: {currency}", callback_data="noop"),
         InlineKeyboardButton(f"Min WD: {min_wd}", callback_data="noop")],
        [InlineKeyboardButton("✏ Set Currency", callback_data="ad_set_currency"),
         InlineKeyboardButton("✏ Set Min WD", callback_data="ad_set_minwd")],
        [InlineKeyboardButton("xR Base URL", callback_data="ad_set_xr_base"),
         InlineKeyboardButton("xR API Key", callback_data="ad_set_xr_key")],
        [InlineKeyboardButton("xR Path", callback_data="ad_set_xr_path")],
        [InlineKeyboardButton("⏪ Back", callback_data="ad_home")]
    ]
    return InlineKeyboardMarkup(rows)

async def is_joined_all(uid: int) -> bool:
    if get_setting("owner_bypass", DEFAULTS["owner_bypass"]) == "1" and is_owner(uid):
        return True
    chats = get_required_chats()
    if not chats:
        return True
    for u in chats:
        try:
            m = await app.get_chat_member(u, uid)
            if m.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
                return False
        except Exception:
            return False
    return True

# ============== COMMANDS ==============
@app.on_message(filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    uid = message.from_user.id
    first = message.from_user.first_name or "User"
    ensure_user(uid, first)

    # referral
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        referrer_id = int(parts[1])
        if referrer_id != uid:
            con = db(); cur = con.cursor()
            cur.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,))
            row = cur.fetchone()
            if not row or row["referred_by"] is None:
                cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, uid))
                cur.execute("INSERT OR IGNORE INTO referrals(referrer_id, referred_id, credited) VALUES(?,?,0)",
                            (referrer_id, uid))
                con.commit()
            con.close()

    if not await is_joined_all(uid):
        await message.reply_text("⚠️ You must join all channels/groups first:", reply_markup=join_menu())
        return

    await message.reply_text(f"🎉 Welcome, {first}!\nChoose an option:", reply_markup=user_menu(is_admin(uid), is_owner(uid)))

# ============== ADMIN & OWNER TEXT PROMPTS ==============
@app.on_message(filters.private & filters.text)
async def handle_text_prompts(client: Client, message: Message):
    uid = message.from_user.id
    txt = (message.text or "").strip()

    # Admin actions
    state = pending_admin_text.pop(uid, None)
    if state and is_admin(uid):
        action = state.get("action")
        # required add/remove
        if action == "ad_add_required":
            ok = add_required_chat(txt)
            await message.reply("✅ Added." if ok else "⚠️ Already exists or invalid.")
            return await message.reply("🛠 Admin Panel", reply_markup=admin_home_menu())
        if action == "ad_remove_required":
            ok = remove_required_chat(txt)
            await message.reply("✅ Removed." if ok else "⚠️ Not found.")
            return await message.reply("🛠 Admin Panel", reply_markup=admin_home_menu())
        # balances (credit/debit)
        if action in ("ad_credit", "ad_debit"):
            parts = txt.split()
            try:
                target = int(parts[0])
                amount = float(parts[1])
            except (ValueError, IndexError):
                return await message.reply("Send like: <user_id> <amount> (example: 123456789 0.5)")

            if action == "ad_credit":
                add_balance(target, amount)
                return await message.reply(f"✅ Credited {amount} to {target}.")
            else: # ad_debit
                if deduct_balance(target, amount):
                    return await message.reply(f"✅ Debited {amount} from {target}.")
                else:
                    return await message.reply("❌ Not enough balance.")
        # settings setters
        if action == "ad_set_channel":
            set_setting("payment_channel", normalize_username(txt))
            return await message.reply("✅ Channel set.", reply_markup=admin_settings_menu())
        if action == "ad_set_currency":
            set_setting("currency", txt.upper())
            return await message.reply("✅ Currency set.", reply_markup=admin_settings_menu())
        if action == "ad_set_minwd":
            try:
                val = float(txt)
                set_setting("min_withdraw", str(val))
                return await message.reply("✅ Min withdraw set.", reply_markup=admin_settings_menu())
            except ValueError:
                return await message.reply("Send a number, e.g. 0.5")
        if action == "ad_set_xr_base":
            set_setting("xr_base_url", txt.strip())
            return await message.reply("✅ xRocket base URL set.", reply_markup=admin_settings_menu())
        if action == "ad_set_xr_key":
            set_setting("xr_api_key", txt.strip())
            return await message.reply("✅ xRocket API key set.", reply_markup=admin_settings_menu())
        if action == "ad_set_xr_path":
            set_setting("xr_path", txt.strip())
            return await message.reply("✅ xRocket path set.", reply_markup=admin_settings_menu())
    
    # Owner actions
    state = pending_owner_text.pop(uid, None)
    if state and is_owner(uid):
        action = state.get("action")
        # Add Admins
        if action == "owner_add_admin":
            user_ids = txt.split()
            added_count = 0
            for user_id_str in user_ids:
                try:
                    user_id = int(user_id_str)
                    user_info = await client.get_users(user_id)
                    if add_admin(user_id, user_info.first_name):
                        added_count += 1
                        try:
                           await client.send_message(user_id, "🎉 You have been added as an admin!")
                        except: pass
                except (ValueError, IndexError):
                    await message.reply(f"Invalid user ID: {user_id_str}")
            await message.reply(f"✅ Successfully added {added_count} new admin(s).", reply_markup=owner_home_menu())
            return

        # Remove Admins
        if action == "owner_remove_admin":
            user_ids = txt.split()
            removed_count = 0
            for user_id_str in user_ids:
                try:
                    user_id = int(user_id_str)
                    if remove_admin(user_id):
                        removed_count += 1
                        try:
                           await client.send_message(user_id, "🚫 You have been removed as an admin.")
                        except: pass
                except (ValueError, IndexError):
                    await message.reply(f"Invalid user ID: {user_id_str}")
            await message.reply(f"✅ Successfully removed {removed_count} admin(s).", reply_markup=owner_home_menu())
            return

        # Transfer Ownership
        if action == "owner_transfer_ownership":
            try:
                new_owner_id = int(txt)
                if new_owner_id == uid:
                    await message.reply("You are already the owner.", reply_markup=owner_home_menu())
                    return
                
                user_info = await client.get_users(new_owner_id)
                transfer_ownership(uid, new_owner_id, user_info.first_name)
                
                await message.reply(f"✅ Ownership transferred to {user_info.first_name} ({new_owner_id}). You are now a regular admin.",
                                    reply_markup=user_menu(is_admin(uid), is_owner(uid)))
                try:
                    await client.send_message(new_owner_id, "🎉 You are now the new owner of this bot!")
                except: pass
            except (ValueError, IndexError):
                await message.reply("Invalid user ID. Please send a valid numeric user ID.", reply_markup=owner_home_menu())
            return
            
# ============== CALLBACKS ==============
@app.on_callback_query()
async def callbacks(client: Client, q: CallbackQuery):
    uid = q.from_user.id
    first = q.from_user.first_name or "User"
    data = q.data or ""

    admin_prefixes = ("ad_", "noop", "wd_approve_", "wd_reject_")
    owner_prefixes = ("owner_",)
    
    if not data.startswith(admin_prefixes) and not data.startswith(owner_prefixes) and data not in ("m_check_join",):
        if not await is_joined_all(uid):
            await q.message.edit_text("⚠️ You must join all channels/groups first:", reply_markup=join_menu())
            await q.answer()
            return
    
    # ===== USER MENU =====
    if data == "m_check_join":
        if await is_joined_all(uid):
            await q.message.reply_text(f"✅ Welcome {first}! Choose an option:",
                                      reply_markup=user_menu(is_admin(uid), is_owner(uid)))
        else:
            await q.answer("❌ You still need to join all channels.", show_alert=True)
            
    elif data == "m_balance":
        bal = get_balance(uid)
        cur = get_setting("currency", DEFAULTS["currency"])
        # avoid MESSAGE_NOT_MODIFIED by adding a tiny suffix that can change
        await q.message.edit_text(f"💰 Your Balance: {bal} {cur}", reply_markup=user_menu(is_admin(uid), is_owner(uid)))
        
    elif data == "m_refer":
        me = client.me or await client.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        await q.message.edit_text(f"👥 Invite & Earn\n\n🔗 {link}", reply_markup=user_menu(is_admin(uid), is_owner(uid)))
        
    elif data == "m_help":
        cur = get_setting("currency", DEFAULTS["currency"])
        mw = get_setting("min_withdraw", DEFAULTS["min_withdraw"])
        help_text = (
            "ℹ Help\n\n"
            "• Join all required channels/groups to access the bot.\n"
            "• Balance shows your earnings.\n"
            "• Refer friends with your link to earn rewards.\n"
            f"• Withdraw when you reach {mw} {cur}.\n\n"
            "Admin note: bot must be admin in required chats to verify membership."
        )
        await q.message.edit_text(help_text, reply_markup=user_menu(is_admin(uid), is_owner(uid)))
        
    elif data == "m_withdraw":
        cur = get_setting("currency", DEFAULTS["currency"])
        mw = float(get_setting("min_withdraw", DEFAULTS["min_withdraw"]))
        bal = get_balance(uid)
        if bal < mw:
            await q.message.edit_text(
                f"❌ Minimum withdraw is {mw} {cur}.\nYour balance: {bal} {cur}",
                reply_markup=user_menu(is_admin(uid), is_owner(uid))
            )
            return

        amount = bal
        if not deduct_balance(uid, amount):
            await q.answer("Balance changed, try again.", show_alert=True)
            return

        mode = get_setting("payment_mode", DEFAULTS["payment_mode"]).strip()
        wd_id = create_withdrawal(uid, amount, mode)

        pay_channel = get_setting("payment_channel", DEFAULTS["payment_channel"])
        text = (
            "💸 Withdrawal Request\n"
            f"ID: {wd_id}\nUser: {uid}\nAmount: {amount} {cur}\n"
            "Status: Pending"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{wd_id}")
        ]])
        try:
            sent = await client.send_message(pay_channel, text, reply_markup=kb, disable_web_page_preview=True)
            set_withdrawal_channel_message(wd_id, sent.chat.id, sent.id)
        except Exception:
            # Refund and mark failed if we cannot announce
            add_balance(uid, amount)
            set_withdrawal_status(wd_id, "failed")
            await q.message.edit_text("❌ Could not announce in payment channel. Try later.",
                                      reply_markup=user_menu(is_admin(uid), is_owner(uid)))
            return
        
        try:
            await q.message.edit_text(
                "⏳ Withdrawal request submitted. Wait for approval.",
                reply_markup=user_menu(is_admin(uid), is_owner(uid))
            )
        except Exception:
            # fallback: send as a new message instead of editing
            await q.message.reply_text(
                "⏳ Withdrawal request submitted. Wait for approval.",
                reply_markup=user_menu(is_admin(uid), is_owner(uid))
            )
        try:
            await client.send_message(uid, f"⏳ Your withdrawal of {amount} {cur} is pending.")
        except Exception:
            pass

    # ===== ADMIN MENUS =====
    elif data == "ad_home":
        if not is_admin(uid) and not is_owner(uid):
            await q.answer("Admins only.", show_alert=True); return
        await q.message.edit_text("🛠 Admin Panel", reply_markup=admin_home_menu())

    elif data == "ad_to_user":
        await q.message.edit_text("Back to menu:", reply_markup=user_menu(is_admin(uid), is_owner(uid)))

    elif data == "ad_list":
        if not is_admin(uid) and not is_owner(uid):
            await q.answer("Admins only.", show_alert=True); return
        chats = get_required_chats()
        if not chats:
            txt = "📋 Required List is empty."
        else:
            txt = "📋 Required List:\n" + "\n".join(f"• {u}" for u in chats)
        await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏪ Back", callback_data="ad_home")]
        ]))
        
    elif data == "ad_add":
        if not is_admin(uid) and not is_owner(uid):
            await q.answer("Admins only.", show_alert=True); return
        pending_admin_text[uid] = {"action": "ad_add_required"}
        await q.message.edit_text("Send the @username (or t.me link) of the channel/group to ADD.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_home")]
                                  ]))
        
    elif data == "ad_remove":
        if not is_admin(uid) and not is_owner(uid):
            await q.answer("Admins only.", show_alert=True); return
        pending_admin_text[uid] = {"action": "ad_remove_required"}
        await q.message.edit_text("Send the @username of the channel/group to REMOVE.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_home")]
                                  ]))

    elif data == "ad_balances":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        await q.message.edit_text("Balances:\n• Credit user\n• Debit user",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("➕ Credit", callback_data="ad_credit")],
                                      [InlineKeyboardButton("➖ Debit", callback_data="ad_debit")],
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_home")]
                                  ]))

    elif data == "ad_credit":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_credit"}
        await q.message.edit_text("Send: <user_id> <amount>\nExample: 123456789 0.5",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_balances")]
                                  ]))
        
    elif data == "ad_debit":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_debit"}
        await q.message.edit_text("Send: <user_id> <amount>",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_balances")]
                                  ]))
        
    elif data == "ad_settings":
        if not is_admin(uid) and not is_owner(uid):
            await q.answer("Admins only.", show_alert=True); return
        await q.message.edit_text("⚙ Settings", reply_markup=admin_settings_menu())

    elif data == "ad_toggle_mode":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        mode = get_setting("payment_mode", DEFAULTS["payment_mode"])
        new_mode = "manual" if mode == "xrocket_link" else "xrocket_link"
        set_setting("payment_mode", new_mode)
        await q.message.edit_text("⚙ Settings", reply_markup=admin_settings_menu())

    elif data == "ad_set_channel":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_channel"}
        await q.message.edit_text("Send @channel username or t.me link for the Payment Channel.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))
    
    elif data == "ad_set_currency":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_currency"}
        await q.message.edit_text("Send currency code, e.g., USDT",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))
        
    elif data == "ad_set_minwd":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_minwd"}
        await q.message.edit_text("Send minimum withdraw amount, e.g., 0.5",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))
        
    elif data == "ad_set_xr_base":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_xr_base"}
        await q.message.edit_text("Send xRocket Base URL, e.g. https://pay.xrocket.tg/api",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))
        
    elif data == "ad_set_xr_key":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_xr_key"}
        await q.message.edit_text("Send xRocket API Key",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))
        
    elif data == "ad_set_xr_path":
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)
        pending_admin_text[uid] = {"action": "ad_set_xr_path"}
        await q.message.edit_text("Send xRocket path (default: /check/create)",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="ad_settings")]
                                  ]))

    # ===== OWNER MENUS =====
    elif data == "owner_home":
        if not is_owner(uid):
            await q.answer("Owner only.", show_alert=True); return
        await q.message.edit_text("👑 Owner Panel", reply_markup=owner_home_menu())

    elif data == "owner_to_user":
        await q.message.edit_text("Back to menu:", reply_markup=user_menu(is_admin(uid), is_owner(uid)))

    elif data == "owner_add_admin":
        if not is_owner(uid):
            return await q.answer("Owner only.", show_alert=True)
        pending_owner_text[uid] = {"action": "owner_add_admin"}
        await q.message.edit_text("Please reply with the user ID(s) of the new admin(s), separated by spaces.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="owner_home")]
                                  ]))

    elif data == "owner_remove_admin":
        if not is_owner(uid):
            return await q.answer("Owner only.", show_alert=True)
        pending_owner_text[uid] = {"action": "owner_remove_admin"}
        await q.message.edit_text("Please reply with the user ID(s) of the admin(s) to remove, separated by spaces.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="owner_home")]
                                  ]))

    elif data == "owner_transfer_ownership":
        if not is_owner(uid):
            return await q.answer("Owner only.", show_alert=True)
        pending_owner_text[uid] = {"action": "owner_transfer_ownership"}
        await q.message.edit_text("Please reply with the user ID of the new owner.",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("⏪ Back", callback_data="owner_home")]
                                  ]))

    # ===== WITHDRAW APPROVE =====
    elif data.startswith("wd_approve_"):
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)

        wd_id = int(data.split("_")[-1])
        row = get_withdrawal(wd_id)
        if not row:
            return await q.answer("Not found.", show_alert=True)
        if row["status"] != "pending":
            return await q.answer("Already handled.", show_alert=True)

        user_id = row["user_id"]
        amount  = row["amount"]
        cur     = get_setting("currency", DEFAULTS["currency"])
        method  = (row["method"] or get_setting("payment_mode", DEFAULTS["payment_mode"])).strip()
        chan_id = row["channel_chat_id"]
        msg_id  = row["channel_message_id"]

        if method == "xrocket_link":
            ok, data_obj = await xr_create_claim_link(user_id, amount)
            if not ok:
                # refund
                add_balance(user_id, amount)
                set_withdrawal_status(wd_id, "failed")
                reason = data_obj.get("error", "xRocket error")
                fail_text = (
                    f"❌ FAILED (xRocket)\n"
                    f"ID: {wd_id}\nUser: {user_id}\nAmount: {amount} {cur}\n"
                    f"Reason: {reason}"
                )
                try:
                    await client.edit_message_text(int(chan_id), int(msg_id), fail_text)
                except Exception:
                    pass
                try:
                    await client.send_message(user_id, fail_text)
                except Exception:
                    pass
                return await q.answer("Failed, refunded.")
                
            # ✅ SUCCESS
            claim_link = data_obj.get("link", "")
            ext_id = str(data_obj.get("id", ""))

            set_withdrawal_external(wd_id, ext_id, claim_link, data_obj)
            set_withdrawal_status(wd_id, "approved")

            # Send safe update to channel (NO link)
            channel_text = (
              f"✅ Withdrawal approved!\n"
              f"ID: {wd_id}\nUser: {user_id}\nAmount: {amount} USDT"
           )
            try:
              await client.edit_message_text(int(chan_id), int(msg_id), channel_text)
            except Exception:
               pass

            # Send claim link privately to user
            try:
              await client.send_message(
                user_id,
                f"✅ Your withdrawal is approved!\nAmount: {amount} USDT\n\n🔗 Claim here: {claim_link}"
              )
            except Exception:
              pass
            return await q.answer("Approved with xRocket.")

        else:
            # Manual mode
            set_withdrawal_status(wd_id, "approved")
            new_text = (
                f"✅ APPROVED (Manual)\n"
                f"ID: {wd_id}\nUser: {user_id}\nAmount: {amount} {cur}\n"
                f"Admin will send manually."
            )
            try:
                await client.edit_message_text(int(chan_id), int(msg_id), new_text, parse_mode=None)
            except Exception:
                pass
            try:
                await client.send_message(user_id, "✅ Your withdrawal is approved and will be sent manually.")
            except Exception:
                pass
            return await q.answer("Approved (manual).")

    elif data.startswith("wd_reject_"):
        if not is_admin(uid) and not is_owner(uid):
            return await q.answer("Admins only.", show_alert=True)

        wd_id = int(data.split("_")[-1])
        row = get_withdrawal(wd_id)
        if not row:
            return await q.answer("Not found.", show_alert=True)
        if row["status"] != "pending":
            return await q.answer("Already handled.", show_alert=True)

        user_id = row["user_id"]
        amount  = row["amount"]
        cur     = get_setting("currency", DEFAULTS["currency"])
        chan_id = row["channel_chat_id"]
        msg_id  = row["channel_message_id"]

        add_balance(user_id, amount)
        set_withdrawal_status(wd_id, "rejected")

        new_text = (
            f"🚫 REJECTED\n"
            f"ID: {wd_id}\nUser: {user_id}\nAmount: {amount} {cur}\n"
            f"Refunded to user."
        )
        try:
            await client.edit_message_text(int(chan_id), int(msg_id), new_text, parse_mode=None)
        except Exception:
            pass
        try:
            await client.send_message(
                user_id,
                f"🚫 Your withdrawal {amount} {cur} was rejected. Amount refunded.",
                parse_mode=None
            )
        except Exception:
            pass
        return await q.answer("Rejected & refunded.", show_alert=False)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("✅ Bot is starting...")

    # make sure DB schema is created
    ensure_tables_and_settings()

    app.run()

