# Restaurant API - V1 backend

FastAPI + PostgreSQL + SQLAlchemy 2.0. Covers: login with admin/customer roles, menu and availability,
tables, reservations (no double booking), takeaway/dine-in orders, Razorpay payments (full or 50% advance),
and a stub for the PetPooja order push.

## Run it

Requires Python 3.12+.

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env  (then change SECRET_KEY and ADMIN_PASSWORD)
uvicorn app.main:app --reload     # API on http://localhost:8000 , Swagger docs at /docs
```

**Database**
- Quick start (no install): in `.env` set `DATABASE_URL=sqlite:///./dev.db`.
- Real setup (use this before deploying): install PostgreSQL, create a database (`createdb restaurant`), and set
  `DATABASE_URL=postgresql+psycopg://<user>:<password>@localhost:5432/restaurant`.
  SQLite is fine for building screens, but the row locks that prevent double booking only work on PostgreSQL.

The first admin is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD` on startup.
Tests: `pytest` (uses SQLite, no setup needed).

## Endpoints (full contract is live at /docs - share that with the frontend developer)

| Area | Endpoint | Who |
|---|---|---|
| Auth | `POST /auth/register`, `POST /auth/login`, `GET /auth/me` | public / logged in |
| Menu | `GET /menu` (`?only_available=true`), `GET /menu/items/{id}` | public |
| Menu admin | `POST/PATCH/DELETE /admin/categories`, `POST/PATCH/DELETE /admin/items`, `PATCH /admin/items/{id}/availability` | admin |
| Tables | `GET /tables/availability?start_time=&guests=` | public |
| Tables admin | `GET/POST/PATCH/DELETE /admin/tables` | admin |
| Reservations | `POST /reservations`, `GET /reservations/me`, `POST /reservations/{id}/cancel` | customer |
| Reservations admin | `GET /admin/reservations?date=&status=`, `PATCH /admin/reservations/{id}/status` | admin |
| Orders | `POST /orders`, `GET /orders/me`, `GET /orders/{id}` | customer |
| Orders admin | `GET /admin/orders?status=&date=`, `PATCH /admin/orders/{id}/status`, `POST /admin/orders/{id}/mark-paid` | admin |
| Payments | `POST /payments/verify`, `POST /payments/webhook` | customer / Razorpay |

## Payment flow (tell the frontend developer)

1. `POST /orders` with `payment_option`: `full_online`, `advance_online` (50%, set by `ADVANCE_PERCENT`) or `pay_at_pickup`.
2. If online, the response has a `payment` object (`razorpay_order_id`, `key_id`, `amount_paise`). Open Razorpay Checkout with it.
3. On success Razorpay gives `razorpay_payment_id` and `razorpay_signature`. Send those 3 values to `POST /payments/verify`.
4. The order moves `pending_payment` -> `placed`. The server also confirms via webhook if the customer closes the browser.
5. Prices are always calculated on the server. The frontend never sends amounts.

Dev mode: with `RAZORPAY_KEY_ID` empty, no real Razorpay call is made (mock order ids), so the frontend can
be built before the client's Razorpay account is ready. Real keys: set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`, and add `https://<domain>/payments/webhook` (event `payment.captured`) in the Razorpay dashboard.

## Notes for the frontend developer

- Send `Authorization: Bearer <token>`. Money values are JSON numbers. Send `start_time` as ISO 8601 with offset
  (`2026-10-20T19:30:00+05:30`); times without offset are treated as restaurant local time (`TIMEZONE`).
- Order status: `pending_payment, placed, preparing, ready, completed, cancelled`.
  Payment status: `unpaid, partially_paid, paid, refunded`.
- Admin order screen: poll `GET /admin/orders?status=placed` every few seconds in V1 (WebSocket comes in V2).
- Reservation length is `RESERVATION_MINUTES` (default 90).

## Not done yet (next steps)

- **PetPooja push**: `app/services/petpooja.py` is a stub. Needs the client's API keys and PetPooja's official payload.
- **Alembic migrations** before production: `create_all` on startup is only for development.
- Refunds (cancelled paid orders are refunded manually in the Razorpay dashboard for now), a cleanup job for
  abandoned `pending_payment` orders, rate limiting on login, opening-hours rules for reservations,
  image upload (menu items take an `image_url` now), email/SMS confirmations.
- V2: add `delivery` to `OrderType` (+ lat/lng and 1 km check), `table_id` on orders for QR ordering, WebSocket.
