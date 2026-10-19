# Restaurant Backend V1 - Handoff & Architecture Context

## 1. Project Overview
This repository is the backend for a restaurant web application in India operating alongside the restaurant's existing **PetPooja POS** billing software.

### Technology Stack
- **Framework:** FastAPI (Python 3.12+)
- **Database:** PostgreSQL (with psycopg 3) in production; SQLite supported for local dev/testing
- **ORM & Migrations:** SQLAlchemy 2.0 + Alembic (5 migrations)
- **Validation:** Pydantic v2 & Pydantic Settings
- **Payment Gateway:** Razorpay (mock mode when keys are absent)
- **POS Integration:** PetPooja (clean architecture, separated mapping, retry loop, webhooks)

---

## 2. Booking, Table & Payment Rules (Critical for Frontend & Team)

### A. Full Online Payment Only
- Customer takeaway and dine-in orders **must use `payment_option="full_online"`**.
- Prices and balances are strictly calculated on the backend from menu item prices; the frontend never submits amounts.
- Online orders return a Razorpay payment object (`razorpay_order_id`, `key_id`, `amount_paise`). Frontend opens Razorpay Checkout and posts signature to `POST /payments/verify`.

### B. Table Channels (`own_app`, `swiggy`, `walk_in`)
- Tables are allocated to specific business channels:
  - `own_app`: Tables designated for bookings & dine-in orders through this web app.
  - `swiggy`: Reserved for external delivery / partner dine-in aggregators.
  - `walk_in`: Reserved for physical walk-in guests at the counter.
- **Channel Isolation:**
  - Public availability (`GET /tables/availability`) and customer dine-in auto-assignment **only check and assign `own_app` tables**.
  - Admin endpoints (`GET/POST/PATCH/DELETE /admin/tables`) allow managing tables across all channels.
  - Admin manual reservations (`POST /admin/reservations`) allow staff to book or block any table on any channel (e.g. Swiggy booking or walk-in seating), blocking slot availability for that table.
  - Changing a table's channel via `PATCH /admin/tables/{id}` is rejected with `409 Conflict` if future reservations exist for that table.

### C. Takeaway Orders
- `POST /orders` with `order_type="takeaway"` requires `pickup_time` (future, within operating hours, respecting buffers).

### D. Dine-In Orders & Atomic Table Holds
- `POST /orders` with `order_type="dine_in"` atomically creates:
  1. A `Reservation` with `status="held"`, `source="own_app"`, and `hold_expires_at = now + HOLD_MINUTES` (default 15 minutes).
  2. An `Order` with `status="pending_payment"` linked to the reservation.
- If `table_id` is omitted, the backend auto-assigns the smallest available `own_app` table matching the party size.
- **Locking & Concurrency:** Protected by database row locks (`SELECT ... FOR UPDATE`) so concurrent diners cannot hold the same table.
- **Payment Verification:**
  - When payment is verified (`POST /payments/verify`), the order status transitions to `placed` and the reservation transitions to `confirmed` in the exact same database transaction.
- **Abandoned Hold Cleanup:**
  - If payment is not completed before `hold_expires_at`, background cleanup cancels the order and releases the held reservation.
- **Late Payment Recovery:**
  - If a customer completes payment after hold expiration, the system checks if the table is still free. If free, it confirms the table. If already taken, the order is marked `placed` with `needs_attention=True` and flagged for refund to prevent double-booking.

### E. Operating Hours & Reservation Settings
- Stored dynamically in the database (`operating_hours` and `restaurant_settings`).
- Supports closed days and past-midnight shifts (e.g. Friday 6:00 PM to 1:00 AM Saturday).
- Enforces `last_reservation_buffer_minutes` (default 60 mins before closing), `max_party_size` (default 20), `max_days_ahead` (default 30), and `reservation_duration_minutes` (default 90).
- All times localized to `Asia/Kolkata` (`UTC+05:30`).

---

## 3. Reports & Analytics (Admin)
Four reporting endpoints under `/admin/reports`:
1. `GET /admin/reports/summary?from=YYYY-MM-DD&to=YYYY-MM-DD`: Business KPIs, net sales, gross paid, total refunded, cancelled orders, average order value, breakdown by order type (`takeaway`, `dine_in`), and counts by payment status.
2. `GET /admin/reports/daily?from=YYYY-MM-DD&to=YYYY-MM-DD`: Day-by-day sales and order volume aggregated in restaurant local timezone (`Asia/Kolkata`).
3. `GET /admin/reports/top-items?from=YYYY-MM-DD&to=YYYY-MM-DD&limit=10`: Top selling menu items ranked by quantity and revenue.
4. `GET /admin/reports/orders.csv?from=YYYY-MM-DD&to=YYYY-MM-DD`: Direct CSV export with formula injection protection (sanitizes `=, +, -, @` prefixes).

---

## 4. Git Commits & Completed Tasks
- `176355f` - **Task 1:** Alembic migrations setup & initial schema
- `58b27c4` - **Task 2:** PostgreSQL test support, concurrency test, GitHub Actions CI
- `27dd36a` - **Task 3:** Razorpay refunds, partial refund tracking, row-level locks, idempotent webhooks
- `cdde131` - **Task 4:** Abandoned unpaid orders cleanup (`SELECT ... FOR UPDATE SKIP LOCKED`), late payment recovery
- `aae1601` - **Task 5:** Security hardening - rate limiting, change password, admin staff creation, Indian phone validation, prod checks
- `0bb23ed` - **Task 6:** Opening hours & reservation rules - DB settings, closed days, past-midnight shifts, migration, tests
- `0fc4a4d` - **Task 7:** Menu images - pluggable storage, magic bytes validation, UUID filenames, `/media` route, tests
- `9bd2fa0` - **Task 8:** PetPooja POS integration structure - separated order mapping, retry loop, callbacks, migration, tests
- `2da1ece` - **Task A:** Booking model change - table channels (`own_app`, `swiggy`, `walk_in`), held reservations, atomic dine-in, takeaway pickup_time
- `116bf85` - **Task B:** Admin sales reports - summary, daily sales, top items, orders CSV export, tests, and documentation
- Task 9: Production readiness - DB health check (503), request IDs (`X-Request-ID`), consistent JSON error formatting, trusted proxy parsing, full `.env.example`, Linux VPS deployment guide in `README.md`

---

## 5. Alembic Migrations
All 5 migrations are sequentially versioned and verified:
1. `b237ff2ee75b_initial_schema.py`: Initial schema
2. `db04b9d1d367_add_refunds_and_order_flags.py`: `refunds` table, `orders.amount_refunded`, `orders.needs_attention`
3. `925a2cee4b0b_add_operating_hours_and_restaurant_settings.py`: `operating_hours`, `restaurant_settings`
4. `26ed25bd5c63_add_petpooja_order_fields.py`: `orders.petpooja_*` fields
5. `3f8a1c9e2d4b_add_table_channels_and_held_reservations.py`: `dining_tables.channel`, `reservations.source`, `reservations.hold_expires_at`, `reservations.customer_name`

---

## 6. Test Suite Status
Total tests: **67 tests**
- Running on SQLite: **64 passed, 3 skipped** (the 3 PostgreSQL tests in `test_postgres.py` require PostgreSQL row-level locks).
- Running on PostgreSQL (`TEST_DATABASE_URL`): **67 passed, 0 skipped**.

Test files:
- `tests/test_v1_flow.py`: Core order and payment flows, refunds, abandoned cleanup
- `tests/test_booking_rules.py`: Channels, holds, dine-in/takeaway booking rules, late payment handling
- `tests/test_security.py`: Authorization, cross-user isolation, password changes, staff creation, rate limiting, phone validation
- `tests/test_hours.py`: Operating hours, closed days, past-midnight shifts, buffers, party size, timezone
- `tests/test_images.py`: Image upload, magic bytes, UUID names, cleanup, size limits
- `tests/test_petpooja.py`: Mocked push, failed push tracking, retry loop, callbacks, order mapping
- `tests/test_reports.py`: Sales summary, daily breakdown, top items, CSV export, validation
- `tests/test_production.py`: Health check with DB probe (200 & 503), `X-Request-ID` propagation, consistent JSON error responses, trusted proxy validation
- `tests/test_postgres.py`: Row-lock concurrency tests and Alembic migrations on fresh PostgreSQL

