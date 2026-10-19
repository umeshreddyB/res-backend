# Restaurant Backend V1 - Handoff & Progress Context

## Project Summary
This is the backend for a restaurant web application in India operating alongside the restaurant's existing **PetPooja POS** system. It supports:
- Customer takeaway & dine-in ordering (full online payment, 50% advance, or pay at pickup).
- Razorpay payment gateway integration with webhooks and refund handling.
- Table reservation system with double-booking prevention (PostgreSQL row locks), operating hours enforcement (including past-midnight shifts and closed days), and custom booking rules.
- Menu management with category hierarchy and image upload using magic bytes detection.
- PetPooja POS integration architecture with separated order payload mapping, retry loop, and callback webhook endpoints.
- Role-based security (customer vs admin), rate limiting, phone number validation, and production environment guards.

---

## Git Commit History
Each task was completed with its own separate git commit:
1. `176355f` - **Task 1: Add Alembic migrations with initial schema**
2. `58b27c4` - **Task 2: PostgreSQL test support, concurrency test, GitHub Actions CI**
3. `27dd36a` - **Task 3: Refunds** - Razorpay refund API, partial refunds, `refund_recommended` flag, webhook
4. `cdde131` - **Task 4: Abandoned unpaid orders** - multi-worker safe cleanup (`SELECT ... FOR UPDATE SKIP LOCKED`), late payment handling
5. `aae1601` - **Task 5: Security hardening** - rate limiting, change password, admin staff creation, phone validation, prod checks
6. `0bb23ed` - **Task 6: Opening hours & reservation rules** - DB settings, closed days, past-midnight shifts, Alembic migration, tests
7. `0fc4a4d` - **Task 7: Menu images** - pluggable storage, magic bytes validation, UUID filenames, `/media` route, tests
8. `9bd2fa0` - **Task 8: PetPooja POS integration structure** - separated order mapping, retry loop, callbacks, Alembic migration, tests

---

## Alembic Migrations
All 4 Alembic migrations have been created, sequentially linked, and tested from scratch on an empty database:
1. `b237ff2ee75b_initial_schema.py`: Initial schema (users, menu_categories, menu_items, dining_tables, reservations, orders, order_items, payments).
2. `db04b9d1d367_add_refunds_and_order_flags.py`: `refunds` table, `orders.amount_refunded`, `orders.needs_attention`.
3. `925a2cee4b0b_add_operating_hours_and_restaurant_settings.py`: `operating_hours` table, `restaurant_settings` table.
4. `26ed25bd5c63_add_petpooja_order_fields.py`: `orders.petpooja_status`, `orders.petpooja_attempts`, `orders.petpooja_last_error`, `orders.petpooja_pushed_at`.

---

## Test Suite Status
All **39 tests pass** (1 test skipped when running locally on SQLite because it requires PostgreSQL for row-level concurrency locks):
```
================== 39 passed, 1 skipped, 1 warning in 47.63s ==================
```
- `tests/test_v1_flow.py`: Full order, payment, and refund lifecycle.
- `tests/test_security.py`: Role authorization, cross-user isolation, password changes, staff creation, rate limiting, Indian phone validation.
- `tests/test_hours.py`: Public & admin opening hours, closed days, overnight shifts past midnight, reservation buffers, max party size, timezone conversions (Asia/Kolkata).
- `tests/test_images.py`: JPEG, PNG, WebP uploads, magic bytes validation, UUID filenames, disk cleanup on replacement/deletion, file size limits.
- `tests/test_petpooja.py`: Mocked push, failed push tracking, retry loop, callbacks, and order payload mapping.
- `tests/test_postgres.py`: PostgreSQL concurrency test with thread barrier.

---

## What We Need from the Client for PetPooja (Task 8 requirement)
As required, the PetPooja integration structure, mapping hook, retry loop, and mocked test harness are built without guessing the proprietary payload. To finalize live PetPooja integration:
1. **API Credentials**:
   - `petpooja_app_key`
   - `petpooja_app_secret`
   - `petpooja_access_token`
   - `petpooja_rest_id` (Restaurant ID)
   - PetPooja environment Base URL (Sandbox vs Production)
2. **Official PetPooja API Documentation**:
   - The official `save_order` JSON specification (field names, tax structures, discount formatting, and payment mode codes).
   - The menu synchronization and order status callback webhook specifications.
3. **Menu Item Mapping**:
   - Mapping between restaurant menu item IDs / SKUs and PetPooja `petpooja_item_id`s.

---

## Next Steps: Task 9 (Production Readiness)
The final planned task is **Task 9: Production Readiness**:
1. **Structured Logging**: Request IDs (middleware) and JSON logging for production.
2. **Health Check**: Update `GET /health` to perform a database ping check.
3. **Consistent JSON Error Format**: Global exception handlers for standard `{ "detail": ... }` responses.
4. **Environment Configuration**: Update `.env.example` with all configuration options (Razorpay, PetPooja, Rate Limiting, CORS, DB, Media).
5. **Deployment Guide in README**: VPS deployment steps, Systemd service unit, Nginx reverse proxy with SSL/HTTPS, database migrations command, and media backup instructions.

---

## Quick Commands to Resume in New Chat
- Run all tests:
  ```powershell
  .\venv\Scripts\python.exe -m pytest tests/ -v
  ```
- Run migrations:
  ```powershell
  .\venv\Scripts\alembic.exe upgrade head
  ```
- Start backend dev server:
  ```powershell
  .\venv\Scripts\uvicorn.exe app.main:app --reload
  ```
