"""
Insta Post Work - Telegram Bot
Stack : Python 3.10+, aiogram 3.x, SQLite
Setup : pip install aiogram
        export BOT_TOKEN="123:ABC"
        export ADMIN_IDS="111111,222222"
        python insta_post_work_bot.py
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaVideo, Message)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_TOKEN_HERE")
ADMINS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]

# ───────────────────────── Fonts (Small Caps) ─────────────────────────
SMALL = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))


def sc(text: str) -> str:
    """First letter of every word capital, rest in small caps."""
    out, prev = [], " "
    for ch in text:
        if ch.isascii() and ch.isalpha():
            if prev.isalnum() or prev in "'’":
                out.append(SMALL[ch.lower()])
            else:
                out.append(ch.upper())
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def B(text: str) -> str:
    return f"<b>{sc(text)}</b>"


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sc(t), callback_data=d) for t, d in row]
        for row in rows
    ])


def esc(x) -> str:
    return html.escape(str(x))


def money(x) -> str:
    return f"₹{float(x):g}"


RULES = (
    f"{B('Rules')}\n"
    f"⚠️ {B('Upload Any 12 [Given] Videos Out Of The Above 15-20 Videos In Every Account, Then Submit Done')}\n\n"
    f"⏰ {B('Approve / Reject Will Be Done Within 24 Hours')}\n"
    f"📌 {B('Bio, Story + Highlight Link, Caption And Cover Photo Are Mandatory')}\n"
    f"🔗 {B('Submit Only Your Profile Link (No Reel / Post / Story Link)')}\n"
    f"🚫 {B('Fake Or Wrong Submissions Can Lead To A Ban')}"
)

URL_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/\S+", re.I)
PROFILE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/(?:_u/)?([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$", re.I)
NOT_PROFILE = {"reel", "reels", "p", "tv", "stories", "story", "explore", "s", "share",
               "direct", "accounts", "about", "legal", "web", "developer", "challenge"}


def clean_profile_link(text: str):
    """Return canonical profile link, or None if it is not a profile link."""
    found = URL_RE.search(text or "")
    if not found:
        return None
    m = PROFILE_RE.match(found.group(0).rstrip(".,)"))
    if not m or m.group(1).lower() in NOT_PROFILE:
        return None
    return f"https://www.instagram.com/{m.group(1)}/"


# ───────────────────────── Database ─────────────────────────
DB_PATH = os.getenv("DB_PATH", "bot.db")
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, balance REAL DEFAULT 0,
    banned INTEGER DEFAULT 0, current_task INTEGER);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, bio TEXT, story_link TEXT,
    caption TEXT, cover TEXT, videos TEXT, reward REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS submissions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task_id INTEGER,
    screenshot TEXT, link TEXT, status TEXT DEFAULT 'pending', reason TEXT);
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
    qr TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS msgmap(chat_id INTEGER, message_id INTEGER, user_id INTEGER);
""")
db.commit()


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    return cur.fetchone() if one else cur.fetchall()


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))


def is_admin(uid):
    return uid in ADMINS


# ───────────────────────── States ─────────────────────────
class Submit(StatesGroup):
    screenshot = State()
    link = State()


class Withdraw(StatesGroup):
    qr = State()


class AddTask(StatesGroup):
    bio = State()
    story = State()
    caption = State()
    cover = State()
    videos = State()
    reward = State()


class Reject(StatesGroup):
    reason = State()


class Broadcast(StatesGroup):
    msg = State()


class Tutorial(StatesGroup):
    video = State()


router = Router()


class BanMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if u and not is_admin(u.id):
            r = q("SELECT banned FROM users WHERE id=?", (u.id,), one=True)
            if r and r["banned"]:
                return
        return await handler(event, data)


# ───────────────────────── Menu ─────────────────────────
def main_menu():
    return kb(
        [("📥 Get Task", "gettask")],
        [("💰 My Wallet", "wallet"), ("💸 Withdraw", "withdraw")],
        [("📄 My Submissions", "mysubs"), ("📜 Rules", "rules")],
    )


async def send_menu(m: Message, name=""):
    await m.answer(
        f"{B('Insta Post Work')}\n\n"
        f"{B('Welcome')} {esc(name)}\n"
        f"{sc('Take A Task, Post Videos, Submit Accounts And Earn')}",
        reply_markup=main_menu())


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    run("INSERT OR IGNORE INTO users(id,name) VALUES(?,?)", (u.id, u.full_name))
    run("UPDATE users SET name=? WHERE id=?", (u.full_name, u.id))
    await send_menu(m, u.full_name)


@router.callback_query(F.data == "rules")
async def rules_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(RULES, reply_markup=kb([("🔙 Back", "menu")]))


@router.callback_query(F.data == "menu")
async def menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await send_menu(cb.message, cb.from_user.full_name)


# ───────────────────────── Get Task ─────────────────────────
@router.callback_query(F.data == "gettask")
async def get_task(cb: CallbackQuery, bot: Bot):
    tasks = q("SELECT * FROM tasks WHERE active=1")
    if not tasks:
        return await cb.answer(sc("No Task Available Right Now"), show_alert=True)
    await cb.answer()
    t = random.choice(tasks)
    run("UPDATE users SET current_task=? WHERE id=?", (t["id"], cb.from_user.id))
    chat = cb.message.chat.id

    await bot.send_photo(chat, t["cover"], caption=B("Cover Photo"))
    vids = json.loads(t["videos"])
    for i in range(0, len(vids), 10):
        await bot.send_media_group(
            chat, [InputMediaVideo(media=v) for v in vids[i:i + 10]])

    text = (
        f"{B('Task')} #{t['id']}\n\n"
        f"{B('Bio')}\n<code>{esc(t['bio'])}</code>\n\n"
        f"{B('Story + Highlight Link')}\n{esc(t['story_link'])}\n\n"
        f"{B('Caption')}\n<code>{esc(t['caption'])}</code>\n\n"
        f"{B('Reward Per Account')}: {money(t['reward'])}\n\n"
        f"{RULES}"
    )
    await bot.send_message(chat, text, reply_markup=kb(
        [("🎬 Tutorial Video", "tutorial")],
        [("📤 Submit Account", "submit")],
        [("🔙 Back", "menu")],
    ))


@router.callback_query(F.data == "tutorial")
async def tutorial(cb: CallbackQuery, bot: Bot):
    fid = get_setting("tutorial")
    if not fid:
        return await cb.answer(sc("Tutorial Not Added Yet"), show_alert=True)
    await cb.answer()
    await bot.send_video(cb.message.chat.id, fid, caption=B("Tutorial Video"))


# ───────────────────────── Submit Accounts (Bulk) ─────────────────────────
def task_count(uid, task_id):
    r = q("SELECT COUNT(*) c FROM submissions WHERE user_id=? AND status='draft'",
          (uid,), one=True)
    return r["c"]


@router.callback_query(F.data == "submit")
async def submit_start(cb: CallbackQuery, state: FSMContext):
    u = q("SELECT current_task FROM users WHERE id=?", (cb.from_user.id,), one=True)
    if not u or not u["current_task"]:
        return await cb.answer(sc("Get A Task First"), show_alert=True)
    await cb.answer()
    await state.set_state(Submit.screenshot)
    await cb.message.answer(
        f"{B('Step 1/2')}\n"
        f"{sc('Send Profile Screenshot')}\n"
        f"{sc('All 12 Posts Must Be Clearly Visible')}",
        reply_markup=kb([("❌ Cancel", "menu")]))


@router.message(Submit.screenshot, F.photo)
async def got_screenshot(m: Message, state: FSMContext):
    await state.update_data(shot=m.photo[-1].file_id)
    await state.set_state(Submit.link)
    await m.answer(
        f"{B('Step 2/2')}\n{sc('Send Your Instagram Profile Link')}\n"
        f"{sc('Example')}: <code>https://instagram.com/username</code>",
        reply_markup=kb([("❌ Cancel", "menu")]))


@router.message(Submit.screenshot)
async def bad_screenshot(m: Message):
    await m.answer(sc("Please Send A Screenshot Photo"))


@router.message(Submit.link, F.text)
async def got_link(m: Message, state: FSMContext, bot: Bot):
    link = clean_profile_link(m.text)
    if not link:
        return await m.answer(
            f"❌ {B('Invalid Link')}\n\n"
            f"{sc('Please Send Only Your Instagram Profile Link.')}\n"
            f"{sc('Reel, Post, Story Or Any Other Link Is Not Accepted.')}\n\n"
            f"{sc('Example')}: <code>https://instagram.com/username</code>")
    dup = q("SELECT id FROM submissions WHERE LOWER(link)=LOWER(?) AND status!='rejected'",
            (link,), one=True)
    if dup:
        return await m.answer(sc("This Profile Link Is Already Submitted"))

    data = await state.get_data()
    uid = m.from_user.id
    task_id = q("SELECT current_task FROM users WHERE id=?", (uid,), one=True)["current_task"]
    cur = run("INSERT INTO submissions(user_id,task_id,screenshot,link,status) VALUES(?,?,?,?,'draft')",
              (uid, task_id, data["shot"], link))
    sub_id = cur.lastrowid
    await state.clear()

    n = task_count(uid, task_id)
    await m.answer(
        f"✅ {B('Account Added')}\n\n"
        f"{B('Accounts Counted')}: {n}\n\n"
        f"{sc('Tap Submit More To Add Another Account, Or Confirm Submit To Send All For Review')}",
        reply_markup=kb([("➕ Submit More", "submit"), ("✅ Confirm Submit", "confirm")]))


async def notify_admins(bot: Bot, s, user):
    reward = q("SELECT reward FROM tasks WHERE id=?", (s["task_id"],), one=True)["reward"]
    cap = (f"{B('New Submission')} #{s['id']}\n\n"
           f"{B('User')}: <a href='tg://user?id={user.id}'>{esc(user.full_name)}</a> "
           f"(<code>{user.id}</code>)\n"
           f"{B('Task')}: #{s['task_id']}\n"
           f"{B('Reward')}: {money(reward)}\n"
           f"{B('Profile Link')}: {esc(s['link'])}")
    for a in ADMINS:
        try:
            sent = await bot.send_photo(a, s["screenshot"], caption=cap, reply_markup=kb(
                [("✅ Accept", f"acc:{s['id']}"), ("❌ Reject", f"rej:{s['id']}")]))
            run("INSERT INTO msgmap VALUES(?,?,?)", (a, sent.message_id, user.id))
        except Exception as e:
            logging.warning("admin notify failed: %s", e)


@router.message(Submit.link)
async def bad_link(m: Message):
    await m.answer(f"❌ {sc('Please Send Your Profile Link As Text')}")


@router.callback_query(F.data == "confirm")
async def confirm_submit(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    drafts = q("SELECT * FROM submissions WHERE user_id=? AND status='draft'", (uid,))
    if not drafts:
        return await cb.answer(sc("No Accounts To Submit"), show_alert=True)
    await cb.answer()
    for d in drafts:
        run("UPDATE submissions SET status='pending' WHERE id=?", (d["id"],))
        await notify_admins(bot, d, cb.from_user)
    run("UPDATE users SET current_task=NULL WHERE id=?", (uid,))
    await cb.message.answer(
        f"🎉 {B('Submission Confirmed')}\n\n"
        f"{B('Total Accounts Sent')}: {len(drafts)}\n"
        f"{sc('Admin Will Review Them Shortly')}",
        reply_markup=main_menu())


# ───────────────────────── Admin Review ─────────────────────────
@router.callback_query(F.data.startswith("acc:"))
async def accept(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    sid = int(cb.data.split(":")[1])
    s = q("SELECT * FROM submissions WHERE id=?", (sid,), one=True)
    if not s or s["status"] != "pending":
        return await cb.answer(sc("Already Processed"), show_alert=True)
    reward = q("SELECT reward FROM tasks WHERE id=?", (s["task_id"],), one=True)["reward"]
    run("UPDATE submissions SET status='accepted' WHERE id=?", (sid,))
    run("UPDATE users SET balance=balance+? WHERE id=?", (reward, s["user_id"]))
    await cb.message.edit_caption(
        caption=cb.message.html_text + f"\n\n✅ {B('Accepted')}", reply_markup=None)
    await cb.answer()
    try:
        await bot.send_message(
            s["user_id"],
            f"✅ {B('Submission Accepted')} #{sid}\n{money(reward)} {sc('Added To Your Wallet')}")
    except Exception:
        pass


@router.callback_query(F.data.startswith("rej:"))
async def reject(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    sid = int(cb.data.split(":")[1])
    s = q("SELECT status FROM submissions WHERE id=?", (sid,), one=True)
    if not s or s["status"] != "pending":
        return await cb.answer(sc("Already Processed"), show_alert=True)
    await cb.answer()
    await state.set_state(Reject.reason)
    await state.update_data(sid=sid, chat=cb.message.chat.id, mid=cb.message.message_id,
                            old=cb.message.html_text)
    await cb.message.answer(sc("Send Rejection Reason"))


@router.message(Reject.reason, F.text)
async def reject_reason(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    sid, reason = d["sid"], m.text.strip()
    s = q("SELECT * FROM submissions WHERE id=?", (sid,), one=True)
    if not s or s["status"] != "pending":
        return await m.answer(sc("Already Processed"))
    run("UPDATE submissions SET status='rejected', reason=? WHERE id=?", (reason, sid))
    try:
        await bot.edit_message_caption(
            chat_id=d["chat"], message_id=d["mid"],
            caption=d["old"] + f"\n\n❌ {B('Rejected')}: {esc(reason)}", reply_markup=None)
    except Exception:
        pass
    await m.answer(f"❌ {sc('Rejected')} #{sid}")
    try:
        await bot.send_message(
            s["user_id"],
            f"❌ {B('Submission Rejected')} #{sid}\n\n{B('Reason')}: {esc(reason)}\n\n"
            f"{sc('You Can Submit Again')}",
            reply_markup=kb([("📤 Submit Again", "submit")]))
        run("UPDATE users SET current_task=? WHERE id=? AND current_task IS NULL",
            (s["task_id"], s["user_id"]))
    except Exception:
        pass


# ───────────────────────── Wallet / Withdraw ─────────────────────────
@router.callback_query(F.data == "wallet")
async def wallet(cb: CallbackQuery):
    uid = cb.from_user.id
    u = q("SELECT balance FROM users WHERE id=?", (uid,), one=True)

    def c(st):
        return q("SELECT COUNT(*) c FROM submissions WHERE user_id=? AND status=?",
                 (uid, st), one=True)["c"]

    await cb.answer()
    await cb.message.answer(
        f"💰 {B('My Wallet')}\n\n"
        f"{B('Balance')}: {money(u['balance'])}\n"
        f"{B('Accepted')}: {c('accepted')}\n"
        f"{B('Pending')}: {c('pending')}\n"
        f"{B('Rejected')}: {c('rejected')}",
        reply_markup=kb([("💸 Withdraw", "withdraw")], [("🔙 Back", "menu")]))


@router.callback_query(F.data == "withdraw")
async def withdraw(cb: CallbackQuery, state: FSMContext):
    u = q("SELECT balance FROM users WHERE id=?", (cb.from_user.id,), one=True)
    minw = float(get_setting("minw", 0))
    if not u or u["balance"] <= 0 or u["balance"] < minw:
        return await cb.answer(
            sc(f"Minimum Balance Needed: {minw:g}" if minw else "Your Balance Is Zero"),
            show_alert=True)
    await cb.answer()
    await state.set_state(Withdraw.qr)
    await cb.message.answer(
        f"{B('Withdraw')}\n\n"
        f"{B('Amount')}: {money(u['balance'])} ({sc('Full Balance')})\n"
        f"{sc('Send Your Upi Qr Code Photo')}",
        reply_markup=kb([("❌ Cancel", "menu")]))


@router.message(Withdraw.qr, F.photo)
async def withdraw_qr(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    u = q("SELECT balance FROM users WHERE id=?", (uid,), one=True)
    amount = u["balance"]
    if amount <= 0:
        await state.clear()
        return await m.answer(sc("Your Balance Is Zero"))
    qr = m.photo[-1].file_id
    run("UPDATE users SET balance=0 WHERE id=?", (uid,))
    wid = run("INSERT INTO withdrawals(user_id,amount,qr) VALUES(?,?,?)",
              (uid, amount, qr)).lastrowid
    await state.clear()
    cap = (f"{B('Withdraw Request')} #{wid}\n\n"
           f"{B('User')}: <a href='tg://user?id={uid}'>{esc(m.from_user.full_name)}</a> "
           f"(<code>{uid}</code>)\n{B('Amount')}: {money(amount)}")
    for a in ADMINS:
        try:
            sent = await bot.send_photo(a, qr, caption=cap, reply_markup=kb(
                [("✅ Paid", f"wpaid:{wid}"), ("❌ Reject", f"wrej:{wid}")]))
            run("INSERT INTO msgmap VALUES(?,?,?)", (a, sent.message_id, uid))
        except Exception as e:
            logging.warning("admin notify failed: %s", e)
    await m.answer(
        f"✅ {B('Withdraw Requested')}\n{B('Amount')}: {money(amount)}\n"
        f"{sc('You Will Be Paid Soon')}",
        reply_markup=main_menu())


@router.message(Withdraw.qr)
async def bad_qr(m: Message):
    await m.answer(sc("Please Send Qr Code As Photo"))


@router.callback_query(F.data.startswith(("wpaid:", "wrej:")))
async def withdraw_admin(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    act, wid = cb.data.split(":")
    w = q("SELECT * FROM withdrawals WHERE id=?", (int(wid),), one=True)
    if not w or w["status"] != "pending":
        return await cb.answer(sc("Already Processed"), show_alert=True)
    await cb.answer()
    if act == "wpaid":
        run("UPDATE withdrawals SET status='paid' WHERE id=?", (w["id"],))
        tag, msg = "✅ Paid", f"✅ {B('Withdraw Paid')} {money(w['amount'])}"
    else:
        run("UPDATE withdrawals SET status='rejected' WHERE id=?", (w["id"],))
        run("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
        tag = "❌ Rejected"
        msg = (f"❌ {B('Withdraw Rejected')}\n"
               f"{money(w['amount'])} {sc('Refunded To Your Wallet')}")
    await cb.message.edit_caption(
        caption=cb.message.html_text + f"\n\n{B(tag)}", reply_markup=None)
    try:
        await bot.send_message(w["user_id"], msg)
    except Exception:
        pass


@router.callback_query(F.data == "mysubs")
async def mysubs(cb: CallbackQuery):
    rows = q("SELECT * FROM submissions WHERE user_id=? ORDER BY id DESC LIMIT 10",
             (cb.from_user.id,))
    await cb.answer()
    if not rows:
        return await cb.message.answer(sc("No Submissions Yet"), reply_markup=kb([("🔙 Back", "menu")]))
    icon = {"draft": "📝", "pending": "⏳", "accepted": "✅", "rejected": "❌"}
    lines = []
    for r in rows:
        line = f"{icon[r['status']]} #{r['id']} - {sc(r['status'])}"
        if r["reason"]:
            line += f"\n   ↳ {esc(r['reason'])}"
        lines.append(line)
    await cb.message.answer(f"{B('My Submissions')}\n\n" + "\n".join(lines),
                            reply_markup=kb([("🔙 Back", "menu")]))


# ───────────────────────── Admin: Add Task ─────────────────────────
@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(sc("Cancelled"))


@router.message(Command("admin"))
async def admin_help(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        f"{B('Admin Panel')}\n\n"
        "/addtask\n/tasks\n/settutorial\n/setmin &lt;amount&gt;\n"
        "/stats\n/broadcast\n/ban &lt;id&gt;\n/unban &lt;id&gt;\n/cancel")


@router.message(Command("addtask"))
async def addtask(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.set_state(AddTask.bio)
    await m.answer(f"{B('Step 1')}: {sc('Send Bio')}")


@router.message(AddTask.bio, F.text)
async def at_bio(m: Message, state: FSMContext):
    await state.update_data(bio=m.text)
    await state.set_state(AddTask.story)
    await m.answer(f"{B('Step 2')}: {sc('Send Story + Highlight Link')}")


@router.message(AddTask.story, F.text)
async def at_story(m: Message, state: FSMContext):
    await state.update_data(story=m.text)
    await state.set_state(AddTask.caption)
    await m.answer(f"{B('Step 3')}: {sc('Send Caption')}")


@router.message(AddTask.caption, F.text)
async def at_caption(m: Message, state: FSMContext):
    await state.update_data(caption=m.text)
    await state.set_state(AddTask.cover)
    await m.answer(f"{B('Step 4')}: {sc('Send Cover Photo')}")


@router.message(AddTask.cover, F.photo)
async def at_cover(m: Message, state: FSMContext):
    await state.update_data(cover=m.photo[-1].file_id, videos=[])
    await state.set_state(AddTask.videos)
    await m.answer(f"{B('Step 5')}: {sc('Send 15-20 Videos, Then Tap Done')}",
                   reply_markup=kb([("✅ Done", "at_done")]))


@router.message(AddTask.videos, F.video)
async def at_video(m: Message, state: FSMContext):
    d = await state.get_data()
    vids = d["videos"] + [m.video.file_id]
    await state.update_data(videos=vids)
    await m.answer(f"{B('Videos Added')}: {len(vids)}",
                   reply_markup=kb([("✅ Done", "at_done")]))


@router.callback_query(AddTask.videos, F.data == "at_done")
async def at_done(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if len(d["videos"]) < 12:
        return await cb.answer(sc("Add At Least 12 Videos"), show_alert=True)
    await cb.answer()
    await state.set_state(AddTask.reward)
    await cb.message.answer(f"{B('Step 6')}: {sc('Send Reward Per Account (₹)')}")


@router.message(AddTask.reward, F.text)
async def at_reward(m: Message, state: FSMContext):
    try:
        reward = float(m.text)
    except ValueError:
        return await m.answer(sc("Send A Valid Number"))
    d = await state.get_data()
    tid = run("INSERT INTO tasks(bio,story_link,caption,cover,videos,reward) VALUES(?,?,?,?,?,?)",
              (d["bio"], d["story"], d["caption"], d["cover"],
               json.dumps(d["videos"]), reward)).lastrowid
    await state.clear()
    await m.answer(f"✅ {B('Task Added')} #{tid}")


@router.message(Command("tasks"))
async def tasks_list(m: Message):
    if not is_admin(m.from_user.id):
        return
    rows = q("SELECT id,reward,active,caption FROM tasks")
    if not rows:
        return await m.answer(sc("No Tasks"))
    for t in rows:
        st = "🟢" if t["active"] else "🔴"
        await m.answer(
            f"{st} {B('Task')} #{t['id']} - {money(t['reward'])}\n{esc(t['caption'][:60])}",
            reply_markup=kb([("🔁 Toggle", f"tt:{t['id']}"), ("🗑 Delete", f"td:{t['id']}")]))


@router.callback_query(F.data.startswith(("tt:", "td:")))
async def task_ctrl(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    act, tid = cb.data.split(":")
    if act == "tt":
        run("UPDATE tasks SET active=1-active WHERE id=?", (int(tid),))
    else:
        run("DELETE FROM tasks WHERE id=?", (int(tid),))
    await cb.answer(sc("Done"))


@router.message(Command("settutorial"))
async def set_tut(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.set_state(Tutorial.video)
    await m.answer(sc("Send Tutorial Video"))


@router.message(Tutorial.video, F.video)
async def tut_video(m: Message, state: FSMContext):
    set_setting("tutorial", m.video.file_id)
    await state.clear()
    await m.answer(f"✅ {B('Tutorial Saved')}")


@router.message(Command("setmin"))
async def setmin(m: Message):
    if not is_admin(m.from_user.id):
        return
    try:
        set_setting("minw", float(m.text.split()[1]))
        await m.answer(f"✅ {B('Minimum Withdraw Saved')}")
    except Exception:
        await m.answer("/setmin 100")


@router.message(Command("stats"))
async def stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    u = q("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b FROM users", one=True)
    p = q("SELECT COUNT(*) c FROM submissions WHERE status='pending'", one=True)["c"]
    w = q("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'", one=True)["c"]
    await m.answer(
        f"{B('Stats')}\n\n{B('Users')}: {u['c']}\n{B('Wallet Total')}: {money(u['b'])}\n"
        f"{B('Pending Submissions')}: {p}\n{B('Pending Withdrawals')}: {w}")


@router.message(Command("ban", "unban"))
@router.message(F.text.lower().in_({".ban", ".unban"}))
async def ban(m: Message, bot: Bot):
    """Reply to any submission / withdraw post with /ban or .ban (or /unban, .unban)."""
    if not is_admin(m.from_user.id):
        return
    cmd = m.text.split()[0].lstrip("/.").split("@")[0].lower()
    uid = None
    if m.reply_to_message:
        row = q("SELECT user_id FROM msgmap WHERE chat_id=? AND message_id=?",
                (m.chat.id, m.reply_to_message.message_id), one=True)
        uid = row["user_id"] if row else None
    elif len(m.text.split()) > 1 and m.text.split()[1].isdigit():
        uid = int(m.text.split()[1])
    if not uid:
        return await m.answer(
            f"{sc('Reply To A User Post With')} /ban {sc('Or')} .ban\n"
            f"{sc('Or Use')} /ban 123456")
    banned = 1 if cmd == "ban" else 0
    run("UPDATE users SET banned=? WHERE id=?", (banned, uid))
    await m.answer(f"{'🚫' if banned else '✅'} {B('User Banned' if banned else 'User Unbanned')} "
                   f"(<code>{uid}</code>)")
    try:
        await bot.send_message(
            uid, f"🚫 {B('You Have Been Banned')}" if banned else f"✅ {B('You Have Been Unbanned')}")
    except Exception:
        pass


@router.message(Command("broadcast"))
async def bc_start(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.set_state(Broadcast.msg)
    await m.answer(sc("Send The Message To Broadcast"))


@router.message(Broadcast.msg)
async def bc_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ok = 0
    for u in q("SELECT id FROM users WHERE banned=0"):
        try:
            await m.copy_to(u["id"])
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"✅ {B('Sent To')} {ok}")


# ───────────────────────── Run ─────────────────────────
async def start_web():
    """Tiny HTTP server so Render Web Service sees an open PORT."""
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMW())
    dp.callback_query.outer_middleware(BanMW())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
