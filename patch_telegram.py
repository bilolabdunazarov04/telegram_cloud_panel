from pathlib import Path

p = Path("app.py")
s = p.read_text()

marker = '@app.get("/login", response_class=HTMLResponse)'
if marker not in s:
    raise SystemExit("XATO: /login qismi topilmadi. Fayl o‘zgartirilmadi.")

if '@app.get("/telegram", response_class=HTMLResponse)' in s:
    raise SystemExit("Telegram sahifasi allaqachon mavjud. Qayta qo‘shilmadi.")

s = s.replace(
    '("dashboard", "/", "Overview"),',
    '("dashboard", "/", "Overview"),\n        ("telegram", "/telegram", "Telegram ulash"),',
    1
)

block = r'''
@app.get("/telegram", response_class=HTMLResponse)
async def telegram_page(request: Request, message: str = ""):
    guard = page_guard(request)
    if guard:
        return guard

    state = auth_state
    notice = f'<div class="notice">{message}</div>' if message else ""
    code_form = ""
    password_form = ""

    if state["phone_code_hash"]:
        code_form = """
        <h2>Tasdiqlash kodi</h2>
        <form method="post" action="/telegram/verify">
          <label>Telegram yuborgan kod</label>
          <input name="code" required autocomplete="one-time-code">
          <button class="btn primary">Kodni tasdiqlash</button>
        </form>
        """

    if state["needs_password"]:
        password_form = """
        <h2>Ikki bosqichli parol</h2>
        <form method="post" action="/telegram/password">
          <label>Telegram 2FA paroli</label>
          <input type="password" name="password" required>
          <button class="btn primary">Parolni tasdiqlash</button>
        </form>
        """

    content = f"""
    <section class="panel" style="padding:24px">
      <h2>Telegram akkauntini ulash</h2>
      <p>{'Akkaunt ulangan.' if state['connected'] else 'Telefon raqamingizni xalqaro formatda kiriting, masalan +998901234567.'}</p>
      {notice}
      <form method="post" action="/telegram/send-code">
        <label>Telefon raqami</label>
        <input name="phone" type="tel" required placeholder="+998901234567">
        <button class="btn primary">Telegram kodini yuborish</button>
      </form>
      {code_form}
      {password_form}
      <hr>
      <p>Eski sessiya ishlamayotgan bo‘lsa, avval uni zaxiralab yangilashingiz mumkin.</p>
      <form method="post" action="/telegram/reset-session"
            onsubmit="return confirm('Eski Telegram sessiyasini zaxiralab, yangisini boshlaysizmi?')">
        <button class="btn">Sessiyani yangilash</button>
      </form>
    </section>
    """
    return HTMLResponse(html_page("Telegram ulash", content, "telegram"))


@app.post("/telegram/send-code")
async def telegram_send_code(request: Request, phone: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard
    phone = phone.strip()
    try:
        async def send():
            tg = await get_client()
            return await tg.send_code_request(phone)
        sent = tg_call(send())
        auth_state["phone"] = phone
        auth_state["phone_code_hash"] = sent.phone_code_hash
        auth_state["needs_password"] = False
        return RedirectResponse("/telegram?message=Kod%20yuborildi", status_code=303)
    except Exception as e:
        return HTMLResponse(
            html_page("Telegram ulash",
                      f'<div class="notice">Kod yuborilmadi: {str(e)}</div><a href="/telegram">Orqaga</a>',
                      "telegram"),
            status_code=400
        )


@app.post("/telegram/verify")
async def telegram_verify(request: Request, code: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard
    if not auth_state["phone"] or not auth_state["phone_code_hash"]:
        return RedirectResponse("/telegram", status_code=303)

    try:
        async def verify():
            tg = await get_client()
            await tg.sign_in(
                phone=auth_state["phone"],
                code=code.strip(),
                phone_code_hash=auth_state["phone_code_hash"]
            )
        tg_call(verify())
        auth_state.update(phone=None, phone_code_hash=None,
                          needs_password=False, connected=True)
        return RedirectResponse("/telegram?message=Akkaunt%20ulandi", status_code=303)
    except SessionPasswordNeededError:
        auth_state["needs_password"] = True
        return RedirectResponse("/telegram?message=Ikki%20bosqichli%20parol%20kerak", status_code=303)
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        return HTMLResponse(
            html_page("Telegram ulash",
                      f'<div class="notice">Kod xatosi yoki muddati tugagan: {str(e)}</div><a href="/telegram">Qaytish</a>',
                      "telegram"),
            status_code=400
        )
    except Exception as e:
        return HTMLResponse(
            html_page("Telegram ulash",
                      f'<div class="notice">Kirishda xato: {str(e)}</div><a href="/telegram">Qaytish</a>',
                      "telegram"),
            status_code=400
        )


@app.post("/telegram/password")
async def telegram_password(request: Request, password: str = Form(...)):
    guard = page_guard(request)
    if guard:
        return guard
    try:
        async def verify_password():
            tg = await get_client()
            await tg.sign_in(password=password)
        tg_call(verify_password())
        auth_state.update(phone=None, phone_code_hash=None,
                          needs_password=False, connected=True)
        return RedirectResponse("/telegram?message=Akkaunt%20ulandi", status_code=303)
    except Exception as e:
        return HTMLResponse(
            html_page("Telegram ulash",
                      f'<div class="notice">Parol bilan kirishda xato: {str(e)}</div><a href="/telegram">Qaytish</a>',
                      "telegram"),
            status_code=400
        )


@app.post("/telegram/reset-session")
async def telegram_reset_session(request: Request):
    global tg_client
    guard = page_guard(request)
    if guard:
        return guard
    try:
        if tg_client is not None and tg_client.is_connected():
            await tg_call(tg_client.disconnect())
        tg_client = None
        session_file = Path(str(SESSION_PATH) + ".session")
        if session_file.exists():
            backup = Path(str(session_file) + ".bak")
            if backup.exists():
                from datetime import datetime
                backup = Path(str(session_file) + "." + datetime.now().strftime("%Y%m%d%H%M%S") + ".bak")
            session_file.rename(backup)
        auth_state.update(phone=None, phone_code_hash=None,
                          needs_password=False, connected=False)
        return RedirectResponse("/telegram?message=Eski%20sessiya%20zaxiralandi", status_code=303)
    except Exception as e:
        return HTMLResponse(
            html_page("Telegram ulash",
                      f'<div class="notice">Sessiyani yangilashda xato: {str(e)}</div><a href="/telegram">Qaytish</a>',
                      "telegram"),
            status_code=400
        )


'''

s = s.replace(marker, block + "\n" + marker, 1)
p.write_text(s)
print("Tayyor: Telegram ulash sahifasi qo‘shildi.")
