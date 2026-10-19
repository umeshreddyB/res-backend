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

## Migrations (Alembic)

Tables are managed by Alembic. In dev/test (`ENV=dev`, the default), `create_all` still runs at startup so
you can work without running migrations. In production (`ENV=prod`), only Alembic is used.

```bash
# Apply all pending migrations (run this before starting the app in production):
alembic upgrade head

# Create a new migration after changing models:
alembic revision --autogenerate -m "describe_the_change"

# Roll back the last migration:
alembic downgrade -1

# See current revision:
alembic current

# See migration history:
alembic history
```

Alembic reads `DATABASE_URL` from app settings (`.env`), so no separate database URL config is needed.

Tests: `pytest` (uses SQLite, no setup needed).

## Endpoints (full contract is live at /docs - share that with the frontend developer)

| Area | Endpoint | Who |
|---|---|---|
| Auth | `POST /auth/register`, `POST /auth/login`, `GET /auth/me` | public / logged in |
| Menu | `GET /menu` (`?only_available=true`), `GET /menu/items/{id}` | public |
| Menu admin | `POST/PATCH/DELETE /admin/categories`, `POST/PATCH/DELETE /admin/items`, `PATCH /admin/items/{id}/availability`, `POST /admin/items/{id}/image` | admin |
| Tables | `GET /tables/availability?start_time=&guests=` (own_app tables only) | public |
| Tables admin | `GET/POST/PATCH/DELETE /admin/tables` (with channel: own_app, swiggy, walk_in) | admin |
| Reservations | `POST /reservations`, `GET /reservations/me`, `POST /reservations/{id}/cancel` | customer |
| Reservations admin | `GET /admin/reservations?date=&status=`, `POST /admin/reservations` (manual booking/block), `PATCH /admin/reservations/{id}/status` | admin |
| Orders | `POST /orders` (takeaway with pickup_time; dine_in creates atomic held table + order), `GET /orders/me`, `GET /orders/{id}` | customer |
| Orders admin | `GET /admin/orders?status=&date=`, `PATCH /admin/orders/{id}/status`, `POST /admin/orders/{id}/mark-paid` | admin |
| Payments | `POST /payments/verify`, `POST /payments/webhook` | customer / Razorpay |
| Settings | `GET /settings/hours` | public |
| Settings admin | `GET /admin/settings/hours`, `PUT /admin/settings/hours` | admin |
| PetPooja admin | `POST /admin/petpooja/retry`, `POST /admin/petpooja/orders/{id}/push` | admin |
| Reports admin | `GET /admin/reports/summary`, `GET /admin/reports/daily`, `GET /admin/reports/top-items`, `GET /admin/reports/orders.csv` | admin |

### Reports endpoints & response shapes (admin)

All report endpoints accept optional `from` and `to` query parameters in ISO format (`YYYY-MM-DD`). Defaults to today in `TIMEZONE` (Asia/Kolkata). Date ranges cannot exceed 366 days.

1. **Summary Report (`GET /admin/reports/summary?from=YYYY-MM-DD&to=YYYY-MM-DD`)**
   High-level business KPIs, breakdown by order type, and payment status counts:
   ```json
   {
     "net_sales": 12500.0,
     "gross_paid": 13000.0,
     "total_refunded": 500.0,
     "orders_count": 42,
     "cancelled_count": 3,
     "average_order_value": 297.62,
     "by_order_type": [
       {"order_type": "takeaway", "orders": 22, "net_sales": 6500.0},
       {"order_type": "dine_in", "orders": 20, "net_sales": 6000.0}
     ],
     "by_payment_status": {
       "unpaid": 2,
       "partially_paid": 0,
       "paid": 37,
       "partially_refunded": 1,
       "refunded": 2
     },
     "pending_amount": 450.0
   }
   ```

2. **Daily Sales Report (`GET /admin/reports/daily?from=YYYY-MM-DD&to=YYYY-MM-DD`)**
   Day-by-day sales breakdown aggregated in the restaurant's local timezone:
   ```json
   [
     {
       "date": "2026-10-18",
       "net_sales": 6200.0,
       "gross_paid": 6500.0,
       "total_refunded": 300.0,
       "orders_count": 21,
       "cancelled_count": 1
     }
   ]
   ```

3. **Top Selling Items (`GET /admin/reports/top-items?from=YYYY-MM-DD&to=YYYY-MM-DD&limit=10`)**
   Top items ranked by quantity sold and net sales (excludes cancelled orders):
   ```json
   [
     {
       "item_id": 1,
       "item_name": "Chicken Biryani",
       "quantity_sold": 45,
       "net_sales": 15750.0
     }
   ]
   ```

4. **Orders CSV Export (`GET /admin/reports/orders.csv?from=YYYY-MM-DD&to=YYYY-MM-DD`)**
   Direct CSV file download (`Content-Type: text/csv; Content-Disposition: attachment; filename="orders_YYYY-MM-DD_YYYY-MM-DD.csv"`) with formula injection protection (values starting with `=`, `+`, `-`, or `@` are safely prefixed with a quote):
   Columns:
   `order_id,created_at,order_type,table_number,status,payment_status,total_amount,amount_paid,amount_refunded,net_amount,customer_name,customer_phone`



## Booking & Payment flow (tell the frontend developer)

1. **Takeaway Orders**: `POST /orders` with `order_type="takeaway"` requires `pickup_time` (future, within operating hours). Customers must use `payment_option="full_online"`.
2. **Dine-In Orders**: `POST /orders` with `order_type="dine_in"` creates an atomic table hold and order. Requires `guests`, `pickup_time` (arrival time), and optional `table_id` (auto-assigns smallest free `own_app` table if omitted). Customers must use `payment_option="full_online"`. Creates a reservation with status `held` (`HOLD_MINUTES`, default 15 mins).
3. If online, the response has a `payment` object (`razorpay_order_id`, `key_id`, `amount_paise`). Open Razorpay Checkout with it.
4. On success Razorpay gives `razorpay_payment_id` and `razorpay_signature`. Send those 3 values to `POST /payments/verify`.
5. For dine-in, verifying payment marks the order `placed` and the reservation `confirmed` in the same transaction. If unpaid before `hold_expires_at`, background cleanup cancels the order and releases the hold.
6. Prices are always calculated on the server. The frontend never sends amounts.

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

## Production Deployment Guide (Single Linux VPS)

This guide walks through deploying the backend on a single Ubuntu 22.04 / 24.04 LTS VPS without Docker, using standard system tools: PostgreSQL, Python virtual environment, Gunicorn with Uvicorn workers managed by Systemd, Nginx reverse proxy with Let's Encrypt SSL, UFW firewall, daily automated backups, and zero-downtime database migrations.

---

### 1. Server Prerequisites & System Packages

Update system packages and install Python, PostgreSQL, Nginx, and Certbot:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip postgresql postgresql-contrib nginx certbot python3-certbot-nginx ufw git
```

---

### 2. Basic Firewall Setup (UFW)

Secure the VPS by allowing only SSH, HTTP, and HTTPS traffic:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

---

### 3. PostgreSQL Database Setup

Create a dedicated database user and production database:
```bash
sudo -u postgres psql
```
Inside the `psql` console:
```sql
CREATE USER restaurant_user WITH PASSWORD 'StrongSecureDatabasePasswordHere';
CREATE DATABASE restaurant OWNER restaurant_user;
GRANT ALL PRIVILEGES ON DATABASE restaurant TO restaurant_user;
\q
```

---

### 4. Application Installation

Create an application directory (e.g. `/var/www/restaurant-backend`), clone the repository, and set up the Python virtual environment:
```bash
sudo mkdir -p /var/www/restaurant-backend
sudo chown -R $USER:$USER /var/www/restaurant-backend
cd /var/www/restaurant-backend

# Clone your repository
git clone https://github.com/umeshreddyB/res-backend.git .

# Create virtualenv and install dependencies (including gunicorn)
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

---

### 5. Production Environment Configuration (`.env`)

Create the production `.env` file:
```bash
cp .env.example .env
nano .env
```
Ensure the following critical production variables are set:
```ini
ENV=prod
DATABASE_URL=postgresql+psycopg://restaurant_user:StrongSecureDatabasePasswordHere@localhost:5432/restaurant
SECRET_KEY=generate-a-long-random-secret-key-at-least-32-chars
CORS_ORIGINS=https://your-frontend-domain.com
ADMIN_EMAIL=admin@your-restaurant.com
ADMIN_PASSWORD=SetAStrongUniqueAdminPassword
TIMEZONE=Asia/Kolkata
TRUSTED_PROXIES=127.0.0.1,::1
MEDIA_DIR=/var/www/restaurant-backend/media
```
*(In `ENV=prod`, the backend refuses to start if `SECRET_KEY` or `ADMIN_PASSWORD` are left at defaults or if `CORS_ORIGINS` is empty).*

---

### 6. Run Alembic Migrations

Apply all schema migrations to the production database:
```bash
source venv/bin/activate
alembic upgrade head
```

---

### 7. Systemd Service Unit (`Gunicorn + Uvicorn`)

Create a systemd service file to manage the application process and ensure auto-start on boot and automatic restart on crashes:
```bash
sudo nano /etc/systemd/system/restaurant-api.service
```
Paste the following configuration:
```ini
[Unit]
Description=Restaurant Backend FastAPI Service
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/var/www/restaurant-backend
EnvironmentFile=/var/www/restaurant-backend/.env
ExecStart=/var/www/restaurant-backend/venv/bin/gunicorn app.main:app \
    --workers 4 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 127.0.0.1:8000 \
    --access-logfile /var/log/restaurant-api-access.log \
    --error-logfile /var/log/restaurant-api-error.log \
    --forwarded-allow-ips "127.0.0.1,::1"

Restart=always
RestartSec=5s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Set appropriate directory ownership and start the service:
```bash
sudo chown -R www-data:www-data /var/www/restaurant-backend
sudo systemctl daemon-reload
sudo systemctl enable restaurant-api
sudo systemctl start restaurant-api
sudo systemctl status restaurant-api
```

---

### 8. Nginx Reverse Proxy Configuration

Create an Nginx configuration file for the site:
```bash
sudo nano /etc/nginx/sites-available/restaurant-api
```
Add the server block:
```nginx
server {
    server_name api.your-restaurant.com;

    client_max_body_size 10M;

    # Static media files served directly by Nginx
    location /media/ {
        alias /var/www/restaurant-backend/media/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
        access_log off;
    }

    # Proxy all API requests to Gunicorn/Uvicorn
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 60s;
    }
}
```

Enable the site and test Nginx configuration:
```bash
sudo ln -s /etc/nginx/sites-available/restaurant-api /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

---

### 9. SSL / HTTPS Setup (Certbot Let's Encrypt)

Obtain and configure free, automatically-renewing SSL certificates:
```bash
sudo certbot --nginx -d api.your-restaurant.com
```
Test automatic renewal:
```bash
sudo certbot renew --dry-run
```

---

### 10. Automated Daily PostgreSQL Backups & Restore Steps

#### A. Backup Script
Create a backup directory and automated script:
```bash
sudo mkdir -p /var/backups/restaurant-db
sudo nano /usr/local/bin/backup-restaurant-db.sh
```
Add the script content:
```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="/var/backups/restaurant-db"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/restaurant_${TIMESTAMP}.sql.gz"

# Export database
PGPASSWORD="StrongSecureDatabasePasswordHere" pg_dump -h localhost -U restaurant_user -d restaurant | gzip > "${BACKUP_FILE}"

# Restrict permissions
chmod 600 "${BACKUP_FILE}"

# Retain backups for 14 days, delete older
find "${BACKUP_DIR}" -type f -name "restaurant_*.sql.gz" -mtime +14 -delete

echo "Backup created: ${BACKUP_FILE}"
```
Make the script executable:
```bash
sudo chmod +x /usr/local/bin/backup-restaurant-db.sh
```

#### B. Daily Cron Job
Add a cron job to run the backup daily at 2:30 AM:
```bash
sudo crontab -e
```
Add line:
```cron
30 2 * * * /usr/local/bin/backup-restaurant-db.sh >> /var/log/db-backup.log 2>&1
```

#### C. Database Restore Procedure
To restore the database from a backup file:
```bash
# 1. Stop the application service to prevent new writes
sudo systemctl stop restaurant-api

# 2. Decompress and restore into PostgreSQL
gunzip -c /var/backups/restaurant-db/restaurant_YYYYMMDD_HHMMSS.sql.gz | PGPASSWORD="StrongSecureDatabasePasswordHere" psql -h localhost -U restaurant_user -d restaurant

# 3. Restart the application service
sudo systemctl start restaurant-api
```

---

### 11. Routine Deployment Workflow (`deploy.sh`)

Every subsequent code deployment must pull changes, install updated dependencies, run pending Alembic migrations, and restart the service:
```bash
cd /var/www/restaurant-backend
git pull origin master
source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
sudo systemctl restart restaurant-api
```
Check health:
```bash
curl -i https://api.your-restaurant.com/health
```
*(Returns `{"status": "ok", "database": "ok", "request_id": "..."}`)*

