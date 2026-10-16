import asyncio
import html
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "panel.db")))
SESSION_PATH = Path(os.getenv("TELEGRAM_SESSION_PATH", str(ROOT / "data" / "telegram_user")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
ADMIN_PASSWORD = os.getenv("PANEL_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

if not API_ID or not API_HASH:
    print("WARNING: Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables.")
if not ADMIN_PASSWORD:
    print("WARNING: Set PANEL_PASSWORD environment variable before using this app.")
if len(SECRET_KEY) < 32:
    print("WARNING: Set SECRET_KEY to a random value of at least 32 characters.")

app = FastAPI(title="Telegram Client Desk")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or "unsafe-development-secret-change-me",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    same_site="lax",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# One Telethon client and one long-lived event loop.
tg_loop = asyncio.new_event_loop()
tg_ready = threading.Event()
tg_client: Optional[TelegramClient] = None
auth_state = {
    "phone": None, "phone_code_hash": None, "needs_password": False,
    "connected": False, "code_delivery": "", "last_code_sent_at": 0.0,
}
auth_lock = threading.Lock()

def loop_thread():
    asyncio.set_event_loop(tg_loop)
    tg_ready.set()
    tg_loop.run_forever()

threading.Thread(target=loop_thread, daemon=True).start()
tg_ready.wait()

def tg_call(coro, timeout=180):
    future = asyncio.run_coroutine_threadsafe(coro, tg_loop)
    return future.result(timeout=timeout)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS recipients (
            peer_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            peer_id INTEGER NOT NULL,
            peer_title TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_date TEXT,
            media_type TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")

init_db()

def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def logged_in(request: Request):
    return request.session.get("authenticated") is True

def page_guard(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None

def html_page(title, content, active="dashboard"):
    nav = [
        ("dashboard", "/", "Overview"),
        ("telegram", "/telegram", "Telegram ulash"),
        ("recipients", "/recipients", "Klientlar"),
        ("posts", "/posts", "Post tanlash"),
        ("history", "/history", "Tarix"),
    ]
    nav_html = "".join(
        f'<a class="nav-item {"active" if key == active else ""}" href="{href}">'
        f'<span class="nav-dot"></span>{label}</a>'
        for key, href, label in nav
    )
    return f"""<!doctype html>
<html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Client Desk</title><link rel="stylesheet" href="/static/style.css"></head>
<body><div class="shell"><aside class="sidebar">
<div class="brand"><div class="brand-mark">T</div><div><b>Client Desk</b><small>TELEGRAM WORKSPACE</small></div></div>
<div class="workspace"><span class="status-dot"></span><div><b>Personal account</b><small>Private workspace</small></div></div>
<nav>{nav_html}</nav><div class="side-bottom"><div class="secure"> <span>●</span> Session protected</div><a class="nav-item" href="/logout">Chiqish</a></div>
</aside><main class="main"><header class="topbar"><div><span class="eyebrow">TELEGRAM AUTOMATION</span><h1>{title}</h1></div><div class="top-right"><span class="pill"><i></i> Panel faol</span><span class="avatar">CD</span><a class="btn logout-btn" href="/logout" title="Tizimdan chiqish">↪ Chiqish</a></div></header>
{content}<footer>Client Desk <span>·</span> Xabar yuborishdan oldin qabul qiluvchilarni tekshiring.</footer></main></div>
<script src="/static/app.js"></script></body></html>"""

def need_client():
    if tg_client is None:
        raise RuntimeError("Telegram mijozi hali sozlanmagan.")
    return tg_client

async def get_client():
    global tg_client
    if tg_client is None:
        tg_client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
    if not tg_client.is_connected():
        await tg_client.connect()
    if await tg_client.is_user_authorized():
        auth_state["connected"] = True
    return tg_client

def media_kind(msg):
    if not msg.media:
        return "Matn"
    if isinstance(msg.media, MessageMediaPhoto):
        return "Rasm"
    if isinstance(msg.media, MessageMediaDocument):
        mime = getattr(msg.media.document, "mime_type", "") or ""
        if mime.startswith("video/"):
            return "Video"
        if mime.startswith("image/"):
            return "Rasm"
        return "Fayl"
    return "Media"

def excerpt(msg):
    return (msg.message or "[Media post]").replace("\n", " ")[:160]


@app.get("/telegram", response_class=HTMLResponse)
async def telegram_page(request: Request, message: str = "", kind: str = "info"):
    guard = page_guard(request)
    if guard:
        return guard

    # Re-check the persisted Telethon session instead of trusting a stale UI flag.
    try:
        tg = tg_call(get_client(), timeout=45)
        auth_state["connected"] = bool(tg_call(tg.is_user_authorized(), timeout=30))
    except Exception:
        auth_state["connected"] = False

    state = auth_state
    notice = ""
    if message:
        safe_message = html.escape(message[:300])
        notice = f'<div class="notice telegram-notice {html.escape(kind)}" role="status">{safe_message}</div>'

    if state["connected"]:
        step_html = f"""
          <div class="tg-connected"><span class="tg-check">✓</span><div>
            <strong>Telegram akkaunti ulangan</strong>
            <p>Ulanish faol. Klientlarni sinxronlash va boshqa funksiyalardan foydalanishingiz mumkin.</p>
          </div></div>
          <div class="tg-actions"><a class="btn primary" href="/recipients">Klientlarga o‘tish</a></div>
        """
    elif state["needs_password"]:
        step_html = """
          <div class="tg-step"><span class="tg-step-number">3</span><div><h3>Ikki bosqichli parol</h3><p>Telegram akkauntingizda qo‘shimcha himoya yoqilgan.</p></div></div>
          <form class="tg-form" method="post" action="/telegram/password">
            <label for="tg-password">Telegram 2FA paroli</label>
            <input id="tg-password" type="password" name="password" required autocomplete="current-password" placeholder="Ikki bosqichli parol">
            <button class="btn primary" type="submit">Parolni tasdiqlash</button>
          </form>
        """
    elif state["phone_code_hash"]:
        delivery = html.escape(state.get("code_delivery") or "Telegram")
        phone = html.escape(state.get("phone") or "")
        step_html = f"""
          <div class="tg-step"><span class="tg-step-number">2</span><div><h3>Tasdiqlash kodi</h3><p><b>{phone}</b> raqamiga yuborish so‘rovi bajarildi. Kod Telegram ko‘rsatgan joyga keladi ({delivery}).</p></div></div>
          <form class="tg-form" method="post" action="/telegram/verify">
            <label for="tg-code">Telegram kodi</label>
            <input id="tg-code" name="code" required inputmode="numeric" autocomplete="one-time-code" maxlength="12" placeholder="Kodni kiriting">
            <button class="btn primary" type="submit">Kodni tasdiqlash</button>
          </form>
          <form class="tg-inline-form" method="post" action="/telegram/send-code">
            <input type="hidden" name="phone" value="{phone}">
            <button class="btn" type="submit">Kodni qayta yuborish</button>
          </form>
        """
    else:
        step_html = """
          <div class="tg-step"><span class="tg-step-number">1</span><div><h3>Telefon raqamingiz</h3><p>Raqamni xalqaro formatda kiriting. Masalan, +998901234567.</p></div></div>
          <form class="tg-form" method="post" action="/telegram/send-code">
            <label for="tg-phone">Telefon raqami</label>
            <input id="tg-phone" name="phone" type="tel" required autocomplete="tel" inputmode="tel" placeholder="+998901234567" pattern="\\+?[0-9() -]{8,20}">
            <button class="btn primary" type="submit">Telegram kodini yuborish</button>
          </form>
        """

    content = f"""
    <section class="panel telegram-panel">
      <div class="tg-card-heading"><div class="tg-icon">T</div><div><span class="eyebrow">XAVFSIZ ULANISH</span><h2>Telegram akkauntini ulash</h2><p>Akkauntingizni bir marta ulang — sessiya serverda saqlanadi.</p></div></div>
      {notice}
      <div class="tg-progress">
        <span class="{'done' if state['connected'] or state['phone_code_hash'] else 'current'}">Telefon</span>
        <i></i><span class="{'done' if state['connected'] else ('current' if state['phone_code_hash'] else '')}">Tasdiqlash</span>
        <i></i><span class="{'done' if state['connected'] else ('current' if state['needs_password'] else '')}">Tayyor</span>
      </div>
      {step_html}
      <div class="tg-help"><strong>Kod kelmadimi?</strong><p>Kod Telegram ilovasidagi xizmat xabarlariga yuborilishi mumkin. Telegram ko‘rsatgan yetkazish usulini tekshiring. Qayta yuborishni ko‘p takrorlamang — vaqtinchalik cheklov qo‘yilishi mumkin.</p></div>
      <details class="tg-reset"><summary>Sessiya bilan muammo bormi?</summary>
        <p>Faqat eski sessiya ishlamayotganiga ishonchingiz komil bo‘lsa, yangilang. Bu akkauntni qayta ulashni talab qilishi mumkin.</p>
        <form method="post" action="/telegram/reset-session" onsubmit="return confirm('Sessiya fayli zaxiralanadi. Telegram akkauntini qayta ulash kerak bo‘lishi mumkin. Davom etasizmi?')">
          <button class="btn" type="submit">Sessiyani zaxiralab yangilash</button>
        </form>
      </details>
    </section>
    """
    return HTMLResponse(html_page("Telegram ulash", content, "telegram"))


def telegram_error_message(exc):
    name = type(exc).__name__
    if isinstance(exc, FloodWaitError):
        seconds = max(1, int(getattr(exc, "seconds", 1)))
        return f"Telegram vaqtinchalik kutishni talab qildi. {seconds} soniyadan keyin qayta urinib ko‘ring."
    known = {
        "PhoneNumberInvalidError": "Telefon raqami noto‘g‘ri. +998... formatida tekshiring.",
        "PhoneNumberBannedError": "Bu telefon raqami Telegram tomonidan cheklangan.",
        "PhoneCodeInvalidError": "Kod noto‘g‘ri. Telegram yuborgan eng so‘nggi kodni kiriting.",
        "PhoneCodeExpiredError": "Kod muddati tugagan. Yangi kod so‘rang.",
        "PhoneCodeEmptyError": "Telegram kodini kiriting.",
        "PasswordHashInvalidError": "Ikki bosqichli parol noto‘g‘ri.",
        "SessionPasswordNeededError": "Telegram ikki bosqichli parolni talab qilmoqda.",
        "AuthRestartError": "Telegram tasdiqlash jarayonini qayta boshlashni so‘radi. Yangi kod yuboring.",
    }
    text = str(exc).lower()
    if "key is not registered in the system" in text or "auth_key_unregistered" in text:
        return "Telegram sessiyasi eskirgan yoki bekor qilingan. «Telegram ulash» sahifasida sessiyani zaxiralab yangilang, keyin telefon raqamingiz bilan qayta ulaning."
    if "database is locked" in text:
        return "Telegram sessiya fayli boshqa jarayon tomonidan band. Biroz kutib qayta urinib ko‘ring."
    return known.get(name, f"Telegram so‘rovi bajarilmadi ({name}). Birozdan keyin qayta urinib ko‘ring.")


@app.post("/telegram/send-code")
async def telegram_send_code(request: Request, phone: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard

    phone = re.sub(r"[\s()\-]", "", phone.strip())
    if not re.fullmatch(r"\+?[1-9][0-9]{7,14}", phone):
        return RedirectResponse("/telegram?message=Telefon%20raqamini%20xalqaro%20formatda%20tekshiring&kind=error", status_code=303)
    if not phone.startswith("+"):
        phone = "+" + phone

    if not auth_lock.acquire(blocking=False):
        return RedirectResponse("/telegram?message=Oldingi%20so‘rov%20bajarilmoqda.%20Biroz%20kuting&kind=error", status_code=303)
    try:
        async def send():
            tg = await get_client()
            if await tg.is_user_authorized():
                return None
            return await tg.send_code_request(phone)

        sent = tg_call(send(), timeout=60)
        if sent is None:
            auth_state.update(phone=None, phone_code_hash=None, needs_password=False, connected=True)
            return RedirectResponse("/telegram?message=Bu%20akkaunt%20allaqachon%20ulangan&kind=success", status_code=303)

        auth_state["phone"] = phone
        auth_state["phone_code_hash"] = sent.phone_code_hash
        auth_state["needs_password"] = False
        auth_state["code_delivery"] = getattr(sent.type, "__class__", type(sent.type)).__name__
        # Prefer Telethon's type name because it distinguishes app, SMS, call, etc.
        auth_state["code_delivery"] = type(sent.type).__name__
        return RedirectResponse("/telegram?message=Telegram%20kod%20yuborish%20so‘rovini%20qabul%20qildi&kind=success", status_code=303)
    except Exception as exc:
        message = telegram_error_message(exc)
        return RedirectResponse("/telegram?message=" + __import__("urllib.parse", fromlist=["quote"]).quote(message) + "&kind=error", status_code=303)
    finally:
        auth_lock.release()


@app.post("/telegram/verify")
async def telegram_verify(request: Request, code: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard
    if not auth_state["phone"] or not auth_state["phone_code_hash"]:
        return RedirectResponse("/telegram?message=Avval%20telefon%20raqamiga%20kod%20yuboring&kind=error", status_code=303)
    code = re.sub(r"\s+", "", code)
    if not code or len(code) > 12:
        return RedirectResponse("/telegram?message=Kodni%20tekshirib%20qayta%20kiriting&kind=error", status_code=303)
    if not auth_lock.acquire(blocking=False):
        return RedirectResponse("/telegram?message=Oldingi%20so‘rov%20bajarilmoqda&kind=error", status_code=303)
    try:
        async def verify():
            tg = await get_client()
            await tg.sign_in(phone=auth_state["phone"], code=code,
                             phone_code_hash=auth_state["phone_code_hash"])
            return await tg.is_user_authorized()
        authorized = tg_call(verify(), timeout=60)
        if authorized:
            auth_state.update(phone=None, phone_code_hash=None, needs_password=False,
                              connected=True, code_delivery="")
            return RedirectResponse("/telegram?message=Telegram%20akkaunti%20muvaffaqiyatli%20ulandi&kind=success", status_code=303)
        return RedirectResponse("/telegram?message=Telegram%20tasdiqlashi%20yakunlanmadi.%20Yana%20urinib%20ko‘ring&kind=error", status_code=303)
    except SessionPasswordNeededError:
        auth_state["needs_password"] = True
        return RedirectResponse("/telegram?message=Ikki%20bosqichli%20parol%20kerak&kind=info", status_code=303)
    except Exception as exc:
        return RedirectResponse("/telegram?message=" + __import__("urllib.parse", fromlist=["quote"]).quote(telegram_error_message(exc)) + "&kind=error", status_code=303)
    finally:
        auth_lock.release()


@app.post("/telegram/password")
async def telegram_password(request: Request, password: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard
    if not auth_state["needs_password"]:
        return RedirectResponse("/telegram?message=Avval%20kodni%20tasdiqlang&kind=error", status_code=303)
    if not auth_lock.acquire(blocking=False):
        return RedirectResponse("/telegram?message=Oldingi%20so‘rov%20bajarilmoqda&kind=error", status_code=303)
    try:
        async def verify_password():
            tg = await get_client()
            await tg.sign_in(password=password)
            return await tg.is_user_authorized()
        authorized = tg_call(verify_password(), timeout=60)
        if not authorized:
            return RedirectResponse("/telegram?message=Akkaunt%20hali%20tasdiqlanmadi&kind=error", status_code=303)
        auth_state.update(phone=None, phone_code_hash=None, needs_password=False,
                          connected=True, code_delivery="")
        return RedirectResponse("/telegram?message=Telegram%20akkaunti%20muvaffaqiyatli%20ulandi&kind=success", status_code=303)
    except Exception as exc:
        return RedirectResponse("/telegram?message=" + __import__("urllib.parse", fromlist=["quote"]).quote(telegram_error_message(exc)) + "&kind=error", status_code=303)
    finally:
        auth_lock.release()


@app.post("/telegram/reset-session")
async def telegram_reset_session(request: Request):
    global tg_client
    guard = page_guard(request)
    if guard:
        return guard
    if not auth_lock.acquire(blocking=False):
        return RedirectResponse("/telegram?message=Oldingi%20so‘rov%20bajarilmoqda&kind=error", status_code=303)
    try:
        if tg_client is not None and tg_client.is_connected():
            await tg_call(tg_client.disconnect(), timeout=30)
        tg_client = None
        session_file = Path(str(SESSION_PATH) + ".session")
        if session_file.exists():
            from datetime import datetime
            backup = Path(str(session_file) + "." + datetime.now().strftime("%Y%m%d%H%M%S") + ".bak")
            session_file.rename(backup)
        auth_state.update(phone=None, phone_code_hash=None, needs_password=False,
                          connected=False, code_delivery="")
        return RedirectResponse("/telegram?message=Eski%20sessiya%20zaxiralandi.%20Endi%20qayta%20ulashingiz%20mumkin&kind=success", status_code=303)
    except Exception as exc:
        return RedirectResponse("/telegram?message=" + __import__("urllib.parse", fromlist=["quote"]).quote(telegram_error_message(exc)) + "&kind=error", status_code=303)
    finally:
        auth_lock.release()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    error_html = '<div class="login-error" role="alert">Parol noto‘g‘ri. Qayta tekshirib ko‘ring.</div>' if error == "1" else ""
    return HTMLResponse(f"""<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d14"><title>Client Desk · Kirish</title><link rel="stylesheet" href="/static/style.css"></head><body class="login-body"><main class="login-card login-pro"><div class="login-brand"><div class="brand-mark big">T</div><span class="eyebrow">PRIVATE WORKSPACE</span></div><div class="login-illustration" aria-hidden="true"><span>✦</span><i></i><b></b></div><h1>Xush kelibsiz</h1><p>Client Desk boshqaruv paneliga kirish uchun parolingizni kiriting.</p>{error_html}<form method="post" action="/login"><label for="admin-password">Admin paroli</label><input id="admin-password" type="password" name="password" required autofocus autocomplete="current-password" placeholder="Parolingiz"><button class="btn primary full login-submit" type="submit">Ilovaga kirish <span aria-hidden="true">→</span></button></form><div class="login-security"><span>◈</span><div><b>Xavfsiz kirish</b><small>Bu parol Telegram parolingizdan alohida.</small></div></div></main></body></html>""")

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD or not secrets.compare_digest(password, ADMIN_PASSWORD):
        return RedirectResponse("/login?error=1", status_code=303)
    request.session.clear()
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/")
async def dashboard(request: Request):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn:
        recipients = conn.execute("SELECT COUNT(*) n FROM recipients WHERE enabled=1").fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) n FROM send_log").fetchone()["n"]
        sent = conn.execute("SELECT COUNT(*) n FROM send_log WHERE status='sent'").fetchone()["n"]
        failed = conn.execute("SELECT COUNT(*) n FROM send_log WHERE status='failed'").fetchone()["n"]
        recent = conn.execute("SELECT * FROM send_log ORDER BY id DESC LIMIT 8").fetchall()
    connected = auth_state["connected"]
    rows = "".join(f"""<tr><td><span class="table-title">{r['peer_title']}</span><small>{r['created_at']}</small></td><td>#{r['source_message_id']}</td><td>{r['media_type']}</td><td><span class="badge {'success' if r['status']=='sent' else 'danger'}">{'Yuborildi' if r['status']=='sent' else 'Xato'}</span></td></tr>""" for r in recent)
    if not rows: rows = '<tr><td colspan="4" class="empty">Hali yuborish tarixi yo‘q.</td></tr>'
    content = f"""
<section class="welcome"><div><span class="eyebrow">ISH JOYI</span><h2>Assalomu alaykum 👋</h2><p>Postlarni tanlang, klientlarni boshqaring va natijalarni bir joyda kuzating.</p></div><div class="welcome-art"><div class="orb orb1"></div><div class="orb orb2"></div><div class="art-card">✦<small>CAMPAIGN<br>CONTROL</small></div></div></section>
<div class="stats-grid"><article class="stat-card"><div class="stat-head">Saqlangan klientlar <span class="stat-icon purple">♙</span></div><strong>{recipients}</strong><small>Yuborishga yoqilgan</small></article><article class="stat-card"><div class="stat-head">Jami urinishlar <span class="stat-icon blue">↗</span></div><strong>{total}</strong><small>Barcha qaydlar</small></article><article class="stat-card"><div class="stat-head">Muvaffaqiyatli <span class="stat-icon green">✓</span></div><strong>{sent}</strong><small>Yuborilgan xabarlar</small></article><article class="stat-card"><div class="stat-head">Xatolar <span class="stat-icon orange">!</span></div><strong>{failed}</strong><small>Tekshirish talab qiladi</small></article></div>
<div class="section-head"><div><span class="eyebrow">QUICK ACTIONS</span><h2>Tezkor amallar</h2></div></div>
<div class="action-grid"><a class="action-card" href="/recipients"><span class="action-icon purple">♙</span><h3>Klientlarni boshqarish</h3><p>Telegram chatlarini yuklang va keraklilarini saqlang.</p><span class="action-link">Ochish →</span></a><a class="action-card" href="/posts"><span class="action-icon blue">▣</span><h3>Post yuborish</h3><p>Saved Messages ichidan postlarni tanlang va oldindan ko‘ring.</p><span class="action-link">Post tanlash →</span></a><a class="action-card" href="/history"><span class="action-icon green">◷</span><h3>Yuborish tarixi</h3><p>Qaysi klientga nima yuborilganini tekshiring.</p><span class="action-link">Tarixni ko‘rish →</span></a></div>
<div class="section-head"><div><span class="eyebrow">ACTIVITY</span><h2>Oxirgi faollik</h2></div><a class="text-link" href="/history">Barchasi →</a></div><div class="panel table-wrap"><table><thead><tr><th>Klient</th><th>Post ID</th><th>Turi</th><th>Holat</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="notice"><b>Telegram ulanishi</b><span>{'Akkaunt sessiyasi faol.' if connected else 'Telegram sahifasini ochganda sessiya tekshiriladi.'} Xabarlarni faqat rozilik bergan klientlarga yuboring.</span></div>"""
    return HTMLResponse(html_page("Overview", content, "dashboard"))

@app.get("/api/status")
async def api_status(request: Request):
    if not logged_in(request): return JSONResponse({"error":"login required"}, status_code=401)
    try:
        tg = tg_call(get_client())
        me = tg_call(tg.get_me())
        return {"connected": True, "name": me.first_name}
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.get("/recipients", response_class=HTMLResponse)
async def recipients_page(request: Request, view: str = "all"):
    guard = page_guard(request)
    if guard: return guard

    if view not in ("all", "selected"):
        view = "all"
        
    with db() as conn:
        if view == "selected":
            rows = conn.execute(
                "SELECT * FROM recipients WHERE enabled=1 ORDER BY title COLLATE NOCASE"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recipients ORDER BY title COLLATE NOCASE"
            ).fetchall()

        selected_count = conn.execute(
            "SELECT COUNT(*) n FROM recipients WHERE enabled=1"
        ).fetchone()["n"]

    items = "".join(f"""<label class="recipient-row"><input type="checkbox" name="peer_ids" value="{r['peer_id']}" {'checked' if r['enabled'] else ''}><span class="recipient-avatar">{(r['title'] or '?')[:1].upper()}</span><span class="recipient-info"><b>{r['title']}</b><small>{('@'+r['username']) if r['username'] else 'Shaxsiy chat'}</small></span><span class="recipient-state">{'Faol' if r['enabled'] else 'O‘chirilgan'}</span></label>""" for r in rows)
    if not items: items = '<div class="empty big-empty">Klientlar hali saqlanmagan. Telegram chatlarini yuklash tugmasini bosing.</div>'
    content = f"""
    <div class="page-intro">
        <p>Telegramdagi klientlarni boshqaring va keraklilarini tanlang.</p>
    </div>

    <div class="toolbar">
        <form method="post" action="/recipients/sync">
            <button class="btn primary">↻ Telegram chatlarini yuklash</button>
        </form>
        <span class="muted">Tanlangan klientlar: {selected_count} ta</span>
    </div>

    <div class="toolbar">
        <a class="btn {'primary' if view == 'all' else ''}"
           href="/recipients?view=all">Barcha klientlar</a>

        <a class="btn {'primary' if view == 'selected' else ''}"
           href="/recipients?view=selected">
           Tanlangan klientlar ({selected_count})
        </a>
    </div>

    <form method="post" action="/recipients/save" class="panel">
        <div class="panel-head">
            <div>
                <h2>{'Tanlangan klientlar' if view == 'selected' else 'Barcha klientlar'}</h2>
                <p>Kerakli klientlarni belgilang va saqlang.</p>
            </div>
            <input id="recipient-search" class="search"
                   placeholder="Ism bo‘yicha qidirish…">
        </div>

        <div class="recipient-list">{items}</div>

        <div class="panel-footer">
            <span class="muted">{len(rows)} ta klient ko‘rsatilmoqda</span>
            <button class="btn primary">Tanlovni saqlash</button>
        </div>
    </form>

    <div class="notice">
        <b>Eslatma</b>
        <span>Faqat rozilik bergan klientlarga xabar yuboring.</span>
    </div>
    """
    return HTMLResponse(html_page("Klientlar", content, "recipients"))

@app.post("/recipients/sync")
async def recipients_sync(request: Request):
    guard = page_guard(request)
    if guard: return guard
    try:
        async def sync():
            tg = await get_client()
            found = []
            async for dialog in tg.iter_dialogs(limit=300):
                if not dialog.is_user or dialog.entity.bot or dialog.entity.deleted:
                    continue
                entity = dialog.entity
                title = " ".join(x for x in [entity.first_name, entity.last_name] if x) or dialog.name or str(entity.id)
                found.append((entity.id, title, getattr(entity, "username", None), now()))
            with db() as conn:
                conn.execute("UPDATE recipients SET enabled=0")

                for peer_id, title, username, updated in found:
                    conn.execute("""INSERT INTO recipients(peer_id,title,username,enabled,updated_at)
                    VALUES(?,?,?,0,?) ON CONFLICT(peer_id) DO UPDATE SET
                    title=excluded.title, username=excluded.username, updated_at=excluded.updated_at""",
                    (peer_id, title, username, updated))
            return len(found)
        count = tg_call(sync())
        return RedirectResponse("/recipients", status_code=303)
    except Exception as e:
        return HTMLResponse(f"Telegram chatlarini yuklab bo‘lmadi: {str(e)}. Telegram sessiyasi va API sozlamalarini tekshiring.", status_code=500)

@app.post("/recipients/save")
async def recipients_save(request: Request):
    guard = page_guard(request)
    if guard: return guard
    form = await request.form()
    ids = set()
    for value in form.getlist("peer_ids"):
        try: ids.add(int(value))
        except ValueError: pass
    with db() as conn:
        conn.execute("UPDATE recipients SET enabled=0")
        if ids:
            conn.executemany("UPDATE recipients SET enabled=1 WHERE peer_id=?", [(x,) for x in ids])
    return RedirectResponse("/recipients", status_code=303)

@app.get("/posts", response_class=HTMLResponse)
async def posts_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    content = """
<div class="page-intro"><p>Telegram Saved Messages ichidan matn, rasm yoki video postlarni tanlang. Bir nechta postni belgilash mumkin.</p></div>
<div class="toolbar"><button id="load-posts" class="btn primary">↻ Saved Messages yuklash</button><input id="post-search" class="search" placeholder="Post ichidan qidirish…"></div>
<div id="post-status" class="notice"><b>Tayyor</b><span>Postlar hali yuklanmagan.</span></div>
<div id="posts-list" class="posts-grid"><div class="empty big-empty">Postlarni yuklash uchun tugmani bosing.</div></div>
<div class="sticky-send"><div><b id="selected-count">0 ta post tanlandi</b><small>Tanlangan postlar har bir faol klientga yuboriladi.</small></div><button id="send-posts" class="btn primary" disabled>Tekshirish va yuborish →</button></div>
<div id="confirm-modal" class="modal hidden"><div class="modal-card"><button id="close-modal" class="modal-close">×</button><span class="eyebrow">FINAL REVIEW</span><h2>Yuborishni tasdiqlash</h2><p id="confirm-summary"></p><div id="confirm-items" class="confirm-items"></div><label class="consent"><input id="consent-check" type="checkbox"> Tanlangan klientlar ushbu xabarlarni olishga rozilik bergan.</label><button id="confirm-send" class="btn primary full" disabled>Yuborishni boshlash</button></div></div>
"""
    return HTMLResponse(html_page("Post tanlash", content, "posts"))

@app.get("/api/posts")
async def api_posts(request: Request):
    if not logged_in(request): return JSONResponse({"error":"login required"}, status_code=401)
    try:
        async def load():
            tg = await get_client()
            result = []
            async for msg in tg.iter_messages("me", limit=100):
                if not msg.message and not msg.media: continue
                result.append({
                    "id": msg.id,
                    "date": msg.date.astimezone().strftime("%d.%m.%Y %H:%M"),
                    "type": media_kind(msg),
                    "text": excerpt(msg),
                    "has_media": bool(msg.media),
                    "full_text": msg.message or "",
                })
            return result
        return tg_call(load())
    except Exception as e:
        return JSONResponse({"error": telegram_error_message(e)}, status_code=500)

@app.post("/api/send")
async def api_send(request: Request):
    if not logged_in(request): return JSONResponse({"error":"login required"}, status_code=401)
    payload = await request.json()
    post_ids = payload.get("post_ids", [])
    if not isinstance(post_ids, list) or not post_ids or len(post_ids) > 10:
        return JSONResponse({"error":"1–10 ta post tanlang."}, status_code=400)
    if payload.get("confirm") is not True or payload.get("consent") is not True:
        return JSONResponse({"error":"Tasdiq va rozilik belgilanishi kerak."}, status_code=400)
    with db() as conn:
        recipients = conn.execute("SELECT * FROM recipients WHERE enabled=1").fetchall()
    if not recipients:
        return JSONResponse({"error":"Faol klientlar yo‘q."}, status_code=400)
    try:
        async def send():
            tg = await get_client()
            source = {}
            for pid in post_ids:
                msg = await tg.get_messages("me", ids=int(pid))
                if not msg: raise RuntimeError(f"Post {pid} topilmadi.")
                source[int(pid)] = msg
            outcome = []
            for r in recipients:
                peer_id, title = r["peer_id"], r["title"]
                try:
                    entity = await tg.get_input_entity(peer_id)
                    for pid in post_ids:
                        msg = source[int(pid)]
                        kind = media_kind(msg)
                        if msg.media:
                            data = await tg.download_media(msg, file=bytes)
                            if not data: raise RuntimeError("Media yuklab olinmadi.")
                            import io
                            f = io.BytesIO(data)
                            mime = getattr(getattr(msg.media, "document", None), "mime_type", "") or ""
                            f.name = "video.mp4" if mime.startswith("video/") else ("image.jpg" if kind == "Rasm" else "attachment.bin")
                            await tg.send_file(entity, f, caption=msg.message or "", force_document=False,
                                               supports_streaming=mime.startswith("video/"))
                        elif msg.message:
                            await tg.send_message(entity, msg.message)
                        with db() as conn:
                            conn.execute("""INSERT INTO send_log(created_at,peer_id,peer_title,source_message_id,source_date,media_type,status,detail)
                                VALUES(?,?,?,?,?,?,?,?)""",
                                (now(), peer_id, title, int(pid), msg.date.isoformat(), kind, "sent", "Copied as a new message"))
                        await asyncio.sleep(1)
                    outcome.append({"peer_id": peer_id, "title": title, "status": "sent", "detail": "Barcha postlar yuborildi"})
                except Exception as exc:
                    with db() as conn:
                        for pid in post_ids:
                            conn.execute("""INSERT INTO send_log(created_at,peer_id,peer_title,source_message_id,media_type,status,detail)
                                VALUES(?,?,?,?,?,?,?)""", (now(), peer_id, title, int(pid), "—", "failed", str(exc)[:500]))
                    outcome.append({"peer_id": peer_id, "title": title, "status": "failed", "detail": str(exc)[:300]})
            return outcome
        result = tg_call(send(), timeout=900)
        return {"results": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn:
        rows = conn.execute("SELECT * FROM send_log ORDER BY id DESC LIMIT 500").fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM send_log").fetchone()["n"]
        sent = conn.execute("SELECT COUNT(*) n FROM send_log WHERE status='sent'").fetchone()["n"]
        failed = conn.execute("SELECT COUNT(*) n FROM send_log WHERE status='failed'").fetchone()["n"]
    trs = "".join(f"""<tr><td><span class="table-title">{r['peer_title']}</span><small>{r['created_at']}</small></td><td>#{r['source_message_id']}</td><td>{r['media_type']}</td><td><span class="badge {'success' if r['status']=='sent' else 'danger'}">{'Yuborildi' if r['status']=='sent' else 'Xato'}</span></td><td class="detail-cell">{r['detail'] or ''}</td></tr>""" for r in rows)
    if not trs: trs = '<tr><td colspan="5" class="empty">Hali tarix yo‘q.</td></tr>'
    content = f"""<div class="stats-grid compact"><article class="stat-card"><div class="stat-head">Jami qaydlar</div><strong>{total}</strong></article><article class="stat-card"><div class="stat-head">Muvaffaqiyatli</div><strong>{sent}</strong></article><article class="stat-card"><div class="stat-head">Xatolar</div><strong>{failed}</strong></article></div>
<div class="section-head"><div><span class="eyebrow">AUDIT LOG</span><h2>Yuborish tarixi</h2></div></div><div class="panel table-wrap"><table><thead><tr><th>Klient / sana</th><th>Post ID</th><th>Turi</th><th>Holat</th><th>Izoh</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return HTMLResponse(html_page("Yuborish tarixi", content, "history"))
