# Telegram Client Desk — cloud web panel

MacBook va iPhone brauzeridan boshqariladigan FastAPI + Telethon paneli.

## Nimalar bor
- Admin paroli bilan kirish
- Telegram shaxsiy akkaunt sessiyasini ishlatish
- So‘nggi dialoglardagi shaxsiy chatlarni ro‘yxatga yuklash
- Klientlarni saqlash va faollashtirish/o‘chirish
- Saved Messages ichidan matn/rasm/video postlarini ko‘rish va 10 tagacha tanlash
- Yuborishdan oldingi tasdiq va rozilik checkboxi
- Yuborish tarixi va statistika
- Manba ko‘rsatilmasdan yangi xabar sifatida matn/media yuborish

## Muhim cheklovlar
- Panel barcha Telegram dialoglarini emas, oxirgi 300 dialogdagi shaxsiy user chatlarni sinxronlaydi.
- Saved Messages ichidan oxirgi 100 ta xabar olinadi.
- Albomlar alohida postlar sifatida ishlanishi mumkin; albom guruhlanishi kafolatlanmaydi.
- Telegram API flood limits yoki maxfiylik sozlamalari tufayli ayrim yuborishlar muvaffaqiyatsiz bo‘lishi mumkin.
- Faqat xabar olishga rozilik bergan klientlarga yuboring. Telegram cheklovlarini chetlab o‘tish uchun foydalanmang.
- Birinchi ishga tushirishda Telegram login kodi va ikki bosqichli parol terminalda so‘ralishi mumkin. Ularni web panel orqali kiritish bu starter versiyada yo‘q.
- Telegram session fayli hisobga kirish imkonini beradi. Uni maxfiy saqlang, GitHub'ga yoki boshqa odamlarga yubormang.
- Cloud serverni doimiy diskli, private, bitta instance'li qilib sozlang. Bir vaqtning o‘zida bir xil session faylini ikki serverda ishlatmang.
- Bu starter production audit/security review o‘rnini bosmaydi. Internetga ochishdan oldin HTTPS, kuchli parol, private disk, backup va server accessni sozlang.

## 1. Lokal tekshirish (Mac)
```bash
cd telegram_cloud_panel
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

`.env` faylida `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `PANEL_PASSWORD`, `SECRET_KEY` qiymatlarini kiriting. `SECRET_KEY` uchun terminalda quyidagini ishlatishingiz mumkin:
```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Muhim: API_HASH va parollarni chatga yubormang, Git'ga qo‘shmang.

Terminalda environment o‘zgaruvchilarini `.env` dan yuklash uchun:
```bash
set -a
source .env
set +a
uvicorn app:app --host 127.0.0.1 --port 8000
```
`.env` fayli shell sintaksisiga mos oddiy `KEY=value` qatorlaridan iborat bo‘lishi kerak.

Brauzerda `http://127.0.0.1:8000` ni oching. Birinchi marta Telegram sessiyasi yo‘q bo‘lsa, `api_id`/`api_hash` va `my_account.session` sessiyasini oldindan loyihada mavjud Telethon `main.py` orqali yaratish tavsiya qilinadi. App birinchi API ishlatilganda sessiyani ulashga harakat qiladi, ammo web orqali login kodi kiritish oqimi bu starterga kiritilmagan.

## 2. Mavjud Telethon sessiyasini ko‘chirish
Agar eski loyihangizda `my_account.session` ishlayotgan bo‘lsa, uni `data/telegram_user.session` nomiga ko‘chiring (faqat serveringizga xavfsiz usulda). Masalan:
```bash
mkdir -p data
cp "/path/to/your/my_account.session" data/telegram_user.session
chmod 600 data/telegram_user.session
```
Session nomi `TELEGRAM_SESSION_PATH` qiymatiga mos kelishi shart. Session fayli maxfiy hisoblanadi.

## 3. Cloud deploy
1. Private repository yarating yoki kodni xavfsiz tarzda serverga yuklang.
2. Doimiy private disk/volume biriktiring va `data/` shu diskda saqlansin.
3. Environment variables kiriting: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `PANEL_PASSWORD`, `SECRET_KEY`, `COOKIE_SECURE=true`.
4. `uvicorn app:app --host 0.0.0.0 --port $PORT` bilan ishga tushiring.
5. HTTPS yoqilganini tekshiring; panelga kuchli parol qo‘ying.
6. Session faylini va SQLite bazani private backup qiling. Public repository yoki logga qo‘ymang.
7. Platforma uyquga ketsa yoki disk vaqtinchalik bo‘lsa, Telegram sessiyasi yo‘qolishi mumkin; persistent disk shart.

## Mavjud GUI loyihasi bilan bog‘lash
Bu web panel `selected_clients.json` o‘rniga SQLite bazadan foydalanadi. Eski Tkinter GUI alohida qoladi; xohlasangiz keyin uni olib tashlab, web paneldan boshqarishingiz mumkin.
