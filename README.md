# The Emporium

Pickup-order website for the school store.

- **Store** (`/`) — students browse items, add to a cart, and place a pickup order. They get a code (like `EM-4K7QZ`) to show at the store and pay at pickup. Sold-out items can't be ordered.
- **Staff** (`/admin`) — password-protected. See incoming orders (New → Ready → Picked up & paid, or Cancel, which puts items back in stock), and manage inventory: add one item, paste many at once, change stock with +/−, edit price/name/photo, hide or delete items.

No dependencies — just Python 3.9+.

## Photos and 3D

Every item shows as a little 3D object on the store: it floats on a stand with a shadow and tilts toward your mouse.

- **Photos:** on the staff page → Inventory, tap an item's **+ Photo** square (on a phone it offers the camera). The background is cut out automatically in the browser, so the item floats on its own. The first cut-out downloads the cut-out tool (~40 MB, cached after that) and takes 10–30 seconds. Untick "Cut out photo backgrounds automatically" to upload photos as-is; they then show as a tilted framed photo.
  Tip: shoot the item on a plain table against a plain wall, filling most of the frame.
- **Spinnable 3D models (optional):** scan the item with a free app such as **Polycam** or **KIRI Engine**, export as **GLB**, then on the staff page tap **Edit → Upload .glb file**. Shoppers see a "↻ 3D" button and can drag to spin the item all the way around (and view it in AR on phones). Max 30 MB.

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
