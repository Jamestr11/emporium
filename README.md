# The Emporium

Pickup-order website for the school store.

- **Store** (`/`) — students browse items, add to a cart, and place a pickup order. They get a code (like `EM-4K7QZ`) to show at the store and pay at pickup. Sold-out items can't be ordered.
- **Staff** (`/admin`) — password-protected. See incoming orders (New → Ready → Picked up & paid, or Cancel, which puts items back in stock), and manage inventory: add one item, paste many at once, change stock with +/−, edit price/name/photo, hide or delete items.

No dependencies — just Python 3.9+.

## Run it on your Mac

```bash
cd ~/emporium
ADMIN_PASSWORD=pick-a-password python3 server.py
```

Open http://localhost:8000 (store) and http://localhost:8000/admin (staff).

## Products

On first start, if the database is empty, items are loaded from `products.json`. After that, add and edit items on the staff page — the database (`emporium.db`) is the source of truth.

## Put it online

Any host that runs Python and keeps a disk works (Render, Railway, Fly.io, a school server). On Render:

1. Push this folder to a GitHub repo.
2. New **Web Service** → pick the repo. Start command: `python3 server.py`.
3. Environment variables: `ADMIN_PASSWORD` (staff password), `SECRET_KEY` (any long random string), `SECURE_COOKIES=1`.
4. Add a **persistent disk** mounted at `/data` and set `DB_PATH=/data/emporium.db` — without it, orders and inventory are wiped on every redeploy.

Vercel/Netlify won't work as-is: they don't keep a database file between requests.

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | `emporium` | Staff sign-in password — **change it** |
| `SECRET_KEY` | derived from password | Signs staff login sessions |
| `PORT` | `8000` | Port to listen on |
| `DB_PATH` | `./emporium.db` | Where orders and inventory are stored |
| `SECURE_COOKIES` | off | Set to `1` when served over HTTPS |
