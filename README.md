# MM — Magic Market

Telegram Mini App: магазин товаров на вес с градацией цены по граммам.

- `index.html` — самодостаточный фронт Mini App (тёмная тема с золотым акцентом, Manrope)
- Страницы: Магазин, История, Профиль, Поддержка + настройки (пин-код, перенос аккаунта)
- Оплата с баланса или картой, доставка Новой Почтой, реферальная программа 5%

## Бэкенд (Railway)

- `main.py` — FastAPI: отдаёт `index.html` + API (`/api/auth`, `/api/order`, `/api/topup`, `/api/transfer/*`) + aiogram-бот ([@Magic_Marketplace_bot](https://t.me/Magic_Marketplace_bot)) в одном процессе
- `db.py` — PostgreSQL (asyncpg), `auth.py` — проверка подписи Telegram initData
- Переменные окружения: `BOT_TOKEN`, `APP_URL` (публичный https-адрес сервиса), `DATABASE_URL` (даёт Railway Postgres)

Без сервера фронт работает в демо-режиме на localStorage (превью, GitHub Pages).
