# The Emporium

Pickup-order website for the school store.

- **Store** (`/`) — students sign in with their school Google account, browse items, and order. Orders are paid from their Emporium balance; they get a code (like `EM-4K7QZ`) to show at pickup. Sold-out items can't be ordered.
- **Staff** (`/admin`) — password-protected. See incoming orders (New → Ready → Picked up, or Cancel, which restocks the items and refunds the student), manage student balances, and manage inventory: add one item, paste many at once, change stock with +/−, edit price/name/photo, hide or delete items.

Python 3.9+. The only dependency is `anthropic` (for Add by name): `pip install -r requirements.txt`. Everything else works without it.

## Student accounts and balances

- Students tap **Sign in** on the store and use their school Google account. Only **@alpha.school** accounts are accepted (the server checks this, not just the page). The first sign-in creates their account.
- **Weekly limit:** each student can buy **1 item per week** by default (resets Monday, Texas time; cancelled orders don't count). Change it in Settings → Weekly limit (0 = no limit). Set `STORE_TIMEZONE` to use another time zone.
- Each student has an **Emporium balance**. Orders are paid from it; a student can't order more than they have. Cancelling an order refunds it automatically.
- Staff → **Students** tab: search for a student, **Add money** when they hand over cash (a negative amount fixes a mistake), and **History** shows every deposit, order and refund.
- Students see their balance and their orders (New → Ready → Picked up) under their name on the store.

### Turning on Sign in with Google (one time, ~10 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project called **Emporium**.
2. **Google Auth Platform → Branding**: app name *The Emporium*, your email as support email. Under **Audience**, pick **Internal** if you're signed in with your school Google account (then only school accounts can ever use it); otherwise pick External and publish the app.
3. **Clients → Create client → Web application**. Under **Authorized JavaScript origins** add:
   - `http://localhost:8765` (your Mac)
   - `https://<you>.pythonanywhere.com` (the live site)

   No redirect URIs are needed. Press **Create** and copy the **Client ID** (ends in `.apps.googleusercontent.com`). The client ID isn't secret.
4. Staff page → **Settings** → **Student sign-in (Google)** → paste the client ID → **Save client ID**.

To allow a different email domain, set `STUDENT_EMAIL_DOMAIN` on the server.

## Tests

```bash
python3 tests/test_accounts.py
```

Runs the real server against a stand-in for Google and checks sign-in rules, balances, refunds, and that two simultaneous orders can't overspend.

## Adding items

- **Add by name (easiest):** on the staff page → Inventory, type the product (e.g. "JBL Go 4 speaker black") or paste a link and press **Find it**. Claude searches the web and drafts the listing — name, short description, category, the usual retail price, and a few product photos. Pick the best photo, set your price and stock, and press **Add to store**.
  Needs an Anthropic API key: staff page → **Settings** → paste it (get one at console.anthropic.com and add a little credit). Each lookup costs roughly 10–30¢. The key is stored on the server and never shown again; `ANTHROPIC_API_KEY` in the environment works too.
- **Photos for existing items:** Edit → **✨ Find photo for me** (uses the same lookup), or **Search images myself** → right-click a picture → Copy image → **Paste photo**. **Take or choose photo** uploads from the phone camera or files. Product shots on a white background look best.
- **Add an item yourself / Add many at once:** with an AI key saved, each item you add gets a product photo found automatically (one at a time, in the background). Without a key they still work; add photos with Edit.

Every item shows on a white photo tile that floats gently and tilts toward the mouse. The starting photos for the original 30 items live in `static/products/` and load automatically on a fresh install.

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

**Keep it alive:** free sites pause after a month unless you log in and press *Run until 1 month from today* on the Web tab (once a month). PythonAnywhere emails a reminder.

**Backups:** download `~/emporium-data/emporium.db` (and the `uploads` folder beside it) from the **Files** tab now and then.

## Other hosts

Anything that runs Python and keeps a disk works (Railway, Render, Fly.io, a school server): run `python3 server.py`, and point `DB_PATH` at the persistent disk. Vercel/Netlify/GitHub Pages won't work as-is — they can't keep a database.

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `./emporium.db` | Where orders and inventory are stored |
| `UPLOAD_DIR` | `uploads/` next to the database | Where uploaded photos are stored |
| `SECURE_COOKIES` | off | Set to `1` when served over HTTPS |
| `PORT` | `8000` | Port to listen on (`server.py` only) |
| `GOOGLE_CLIENT_ID` | unset | Optional: the Google client ID for student sign-in, instead of saving it in Settings |
| `STUDENT_EMAIL_DOMAIN` | `alpha.school` | Only Google accounts with this email domain can sign in |
| `ADMIN_PASSWORD` | unset | Optional: fix the staff password from the server instead of creating it on the staff page |
| `SECRET_KEY` | generated and stored in the database | Optional: signs staff sign-in sessions |
