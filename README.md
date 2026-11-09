# The Emporium

Pickup-order website for the school store.

- **Store** (`/`) — students browse items, add to a cart, and place a pickup order. They get a code (like `EM-4K7QZ`) to show at the store and pay at pickup. Sold-out items can't be ordered.
- **Staff** (`/admin`) — password-protected. See incoming orders (New → Ready → Picked up & paid, or Cancel, which puts items back in stock), and manage inventory: add one item, paste many at once, change stock with +/−, edit price/name/photo, hide or delete items.

Python 3.9+. The only dependency is `anthropic` (for Add by name): `pip install -r requirements.txt`. Everything else works without it.

## Adding items

- **Add by name (easiest):** on the staff page → Inventory, type the product (e.g. "JBL Go 4 speaker black") or paste a link and press **Find it**. Claude searches the web and drafts the listing — name, short description, category, the usual retail price, and a few product photos. Pick the best photo, set your price and stock, and press **Add to store**. The photo's background is cut out automatically so the item floats on the store page.
  Needs an Anthropic API key: staff page → **Settings** → paste it (get one at console.anthropic.com and add a little credit). Each lookup costs roughly 10–30¢. The key is stored on the server and never shown again; `ANTHROPIC_API_KEY` in the environment works too.
- **Photos for existing items:** Edit → **✨ Find photo for me** (uses the same lookup), or **Search images myself** → right-click a picture → Copy image → **Paste photo**. **Take or choose photo** uploads from the phone camera or files.
- **Add an item yourself / Add many at once** still work without any key.

Every item shows as a little 3D object on the store: it floats on a stand with a shadow and tilts toward your mouse. The first background cut-out downloads the cut-out tool (~40 MB, cached after that). Untick "Cut out photo backgrounds automatically" to keep photos as-is.

## Run it on your Mac

```bash
cd ~/emporium
python3 server.py
```

Open http://localhost:8000 (store) and http://localhost:8000/admin (staff). The first time you open the staff page, it asks you to **create the staff password**. Only a salted hash of it is stored, never the password itself. Change it any time with the **Password** button; that signs everyone else out.

## Products

On first start, if the database is empty, items are loaded from `products.json`. After that, add and edit items on the staff page — the database (`emporium.db`) is the source of truth.

## Put it online (PythonAnywhere, free)

The live site runs on [PythonAnywhere](https://www.pythonanywhere.com) through `wsgi.py`. Code lives on GitHub; data (orders, inventory, photos) lives in a separate folder on PythonAnywhere so updating the code never touches it.

**First-time setup**
1. In a PythonAnywhere **Bash console**:
   ```bash
   git clone https://github.com/<you>/emporium.git ~/emporium
   mkdir -p ~/emporium-data
   pip3 install --user -r ~/emporium/requirements.txt
   ```
2. **Web** tab → *Add a new web app* → *Manual configuration* → newest Python 3.
3. Click the **WSGI configuration file** link, replace everything in it with:
   ```python
   import os, sys
   HOME = os.path.expanduser("~")
   os.environ["DB_PATH"] = HOME + "/emporium-data/emporium.db"
   os.environ["SECURE_COOKIES"] = "1"
   sys.path.insert(0, HOME + "/emporium")
   from wsgi import application
   ```
4. Same page: turn on **Force HTTPS**, then press **Reload**.
5. Open `https://<you>.pythonanywhere.com/admin` right away and create the staff password.

**Updating after a code change:** push to GitHub, then in a PythonAnywhere Bash console run `cd ~/emporium && git pull` and press **Reload** on the Web tab.

**Keep it alive:** free sites pause after 3 months unless you log in and press *Run until 3 months from today* on the Web tab. PythonAnywhere emails a reminder.

**Backups:** download `~/emporium-data/emporium.db` (and the `uploads` folder beside it) from the **Files** tab now and then.

## Other hosts

Anything that runs Python and keeps a disk works (Railway, Render, Fly.io, a school server): run `python3 server.py`, and point `DB_PATH` at the persistent disk. Vercel/Netlify/GitHub Pages won't work as-is — they can't keep a database.

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `./emporium.db` | Where orders and inventory are stored |
| `UPLOAD_DIR` | `uploads/` next to the database | Where photos and 3D models are stored |
| `SECURE_COOKIES` | off | Set to `1` when served over HTTPS |
| `PORT` | `8000` | Port to listen on (`server.py` only) |
| `ADMIN_PASSWORD` | unset | Optional: fix the staff password from the server instead of creating it on the staff page |
| `SECRET_KEY` | generated and stored in the database | Optional: signs staff sign-in sessions |
