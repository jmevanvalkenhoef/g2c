"""G2C demo game backend.

Two hats (kept in ONE process for the PoC, but TWO clearly separated API groups):

  1. GAME API (/v1/...)  — player-facing: players, items, listings, purchases.
  2. ESCROW CONTRACT (/reserve_sale, /commit_sale, /release_sale, /sale_status,
     /internal/...)
     — the spec §3.2 surface the escrow service calls. Any escrow that honors
     this contract (stub today, real Lightning escrow tomorrow) can be swapped
     in by changing GAME_BACKEND's ESCROW_URL config only.

Ownership truth lives in SQLite. reserve/commit/release are atomic and
idempotent on payment_hash — that is what makes the escrow's guarantees real.
A reserved item is FROZEN: it can't be listed, traded or moved until the
escrow commits it (after the money is final) or releases it.
"""
import hashlib
import hmac
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("DB_PATH")
               or Path(__file__).parent / "data" / "game.db")
ESCROW_URL = os.environ.get("ESCROW_URL", "http://localhost:8001")
SHARED_SECRET = os.environ.get("ESCROW_SHARED_SECRET", "dev-secret")  # set "" to disable
PAYOUT_MODE = os.environ.get("PAYOUT_MODE", "lab")  # lab | real:
GAME_ADMIN_TOKEN = os.environ.get("GAME_ADMIN_TOKEN", "")  # required for the
# world reset when set (mainnet); empty = open (lab/dev)
# lab  -> escrow pays the lab's seller node (regtest)
# real -> escrow resolves the seller's own payout method (Lightning address, signet+)
# an escrow call that timed out may still have minted an invoice: keep the
# listing held until that invoice can no longer be paid, then reopen it
AMBIGUOUS_HOLD_S = int(os.environ.get("AMBIGUOUS_HOLD_S", "900"))

# demo items (the web app maps each name to its icon and rarity); seeded alice
# gets the even-indexed five, bob the odd, and every new character receives
# STARTER_ITEMS of them at random
ARCHETYPES = [
    "Rusty Sword", "Iron Shield", "Healing Potion", "Elixir of Speed",
    "Wizard Hat", "Leather Boots", "Enchanted Bow", "Bag of Holding",
    "Dragon Scale", "Mysterious Key",
]

STARTER_ITEMS = 5
ROLES = ("archmage", "runeknight", "ranger")
LNADDR_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+(\.[\w\-]+)+$")
PLAYER_COLS = "id, name, role, ln_address"

app = FastAPI(title="g2c demo game")

# ---------------------------------------------------------------- db

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    """Read or autocommit connection, closed on exit (sqlite3's own context
    manager only commits, it never closes)."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """Write transaction with immediate lock (single-writer discipline)."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    # It's a demo: every start begins from the hardcoded Alice/Bob seed.
    # No persisted state, no migrations, no stale in-flight rows.
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
              id    INTEGER PRIMARY KEY,
              name  TEXT UNIQUE NOT NULL,
              ln_address TEXT,                   -- Lightning address (LUD-16)
              role       TEXT NOT NULL DEFAULT 'archmage', -- character class (crest)
              pw_salt    TEXT,                   -- set on first login (claim)
              pw_hash    TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token      TEXT PRIMARY KEY,
              player_id  INTEGER NOT NULL REFERENCES players(id)
            );
            CREATE TABLE IF NOT EXISTS items (
              id      INTEGER PRIMARY KEY,
              name    TEXT NOT NULL,
              owner_id INTEGER NOT NULL REFERENCES players(id),
              reserved_by TEXT                   -- payment_hash of the sale freezing it
            );
            CREATE TABLE IF NOT EXISTS listings (
              id         INTEGER PRIMARY KEY,
              item_id    INTEGER UNIQUE NOT NULL REFERENCES items(id),
              seller_id  INTEGER NOT NULL REFERENCES players(id),
              price_msat INTEGER NOT NULL CHECK (price_msat > 0),
              status     TEXT NOT NULL DEFAULT 'open', -- open|in_flight|completed|cancelled
              in_flight_until INTEGER                  -- ambiguous hold expiry
            );
            -- escrow contract state, idempotency anchored on payment_hash
            CREATE TABLE IF NOT EXISTS sales (
              payment_hash TEXT PRIMARY KEY,
              item_id      INTEGER NOT NULL,
              buyer_id     INTEGER NOT NULL,
              seller_id    INTEGER NOT NULL,
              listing_id   INTEGER,                    -- the listing being sold
              status       TEXT NOT NULL,            -- reserved|committed|released
              created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS purchases (
              payment_hash TEXT PRIMARY KEY,
              buyer_id     INTEGER NOT NULL,
              listing_id   INTEGER NOT NULL,
              sale_id      TEXT,                        -- escrow-side id
              status       TEXT NOT NULL DEFAULT 'pending' -- pending|completed|cancelled
            );
            """
        )
        if conn.execute("SELECT 1 FROM players WHERE name='alice'").fetchone() is None:
            demo = [("alice", "alice@lightning.demo", "archmage"),
                    ("bob", "bob@lightning.demo", "runeknight")]
            pids = {}
            for name, addr, role in demo:
                pids[name] = conn.execute(
                    "INSERT INTO players(name, ln_address, role) VALUES (?, ?, ?)",
                    (name, addr, role)).lastrowid
            for i, name in enumerate(ARCHETYPES):        # split assets 5 / 5
                owner = "alice" if i % 2 == 0 else "bob"
                conn.execute("INSERT INTO items(name, owner_id) VALUES (?, ?)",
                             (name, pids[owner]))
    print(f"[game] db ready at {DB_PATH} (escrow={ESCROW_URL})")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------- helpers

async def require_player(request: Request, response: Response) -> dict:
    """Returns the full player row. Per-TAB sessions (X-Player-Session from
    sessionStorage) take precedence over the cookie, so two tabs in one
    browser can be two players. The token is a random secret issued at
    login: a guessable player id must never be a credential."""
    token = request.headers.get("x-player-session") or request.cookies.get("session")
    if not token:
        raise HTTPException(401, "no player session")
    with db() as conn:
        row = conn.execute(
            "SELECT p.id, p.name, p.role, p.ln_address FROM sessions s "
            "JOIN players p ON p.id = s.player_id WHERE s.token=?",
            (token,)).fetchone()
    if not row:
        response.delete_cookie("session")
        raise HTTPException(401, "unknown session")
    return dict(row)


def check_secret(x_escrow_secret: Optional[str]) -> None:
    if SHARED_SECRET and not hmac.compare_digest(
            (x_escrow_secret or "").encode(), SHARED_SECRET.encode()):
        raise HTTPException(401, "bad escrow secret")


def _pw_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                          n=2**14, r=8, p=1).hex()


def release_stale_holds(conn) -> None:
    """Reopen listings held for an ambiguous escrow call whose invoice can
    no longer be paid (a listing hold, not an item reservation)."""
    conn.execute(
        "UPDATE listings SET status='open', in_flight_until=NULL "
        "WHERE status='in_flight' AND in_flight_until IS NOT NULL "
        "AND in_flight_until < ?", (int(time.time()),))


class PlayerIn(BaseModel):
    name: str
    password: str
    ln_address: Optional[str] = None   # new characters: where sales get paid
    role: Optional[str] = None         # new characters: archmage|runeknight|ranger


class ListingIn(BaseModel):
    item_id: int
    price_msat: int


class PurchaseIn(BaseModel):
    listing_id: int


class ReserveIn(BaseModel):
    item_id: int
    buyer_id: int
    seller_id: int
    idempotency_key: str


class SaleKeyIn(BaseModel):
    idempotency_key: str
    reason: str = ""


class SaleResultIn(BaseModel):
    payment_hash: str
    result: str  # completed | cancelled


# ---------------------------------------------------------------- GAME API (player-facing)

@app.post("/v1/admin/reset")
def admin_reset(x_admin_token: str = Header(default="")):
    """dev/demo: wipe and re-seed the world (fresh Alice/Bob + items)"""
    if GAME_ADMIN_TOKEN and x_admin_token != GAME_ADMIN_TOKEN:
        raise HTTPException(403, "bad admin token")
    init_db()
    return {"status": "reset"}


@app.post("/v1/players")
def create_player(body: PlayerIn, response: Response):
    """Create a character, or sign back in to one. A new name becomes a new
    character (with its Lightning address, class and STARTER_ITEMS random
    items); an existing name needs its password. The first login to a seeded
    name (alice, bob) sets that password."""
    name = body.name.strip()
    if not name or len(name) > 24 or len(body.password) < 4:
        raise HTTPException(422, "a name (up to 24 characters) and a password of 4+ characters are required")
    ln_address = (body.ln_address or "").strip() or None
    if ln_address and not LNADDR_RE.match(ln_address):
        raise HTTPException(422, "that doesn't look like a Lightning address (name@domain)")
    role = body.role if body.role in ROLES else "archmage"
    token = secrets.token_urlsafe(32)
    with tx() as conn:
        row = conn.execute(
            "SELECT id, pw_salt, pw_hash FROM players WHERE name=?", (name,)).fetchone()
        if row and row["pw_hash"]:
            if not hmac.compare_digest(_pw_hash(body.password, row["pw_salt"]),
                                       row["pw_hash"]):
                raise HTTPException(401, "wrong password")
            pid = row["id"]
        else:
            salt = secrets.token_hex(16)
            if row:
                pid = row["id"]
                conn.execute("UPDATE players SET pw_salt=?, pw_hash=? WHERE id=?",
                             (salt, _pw_hash(body.password, salt), pid))
            else:
                pid = conn.execute(
                    "INSERT INTO players(name, role, ln_address, pw_salt, pw_hash) "
                    "VALUES (?,?,?,?,?)",
                    (name, role, ln_address, salt, _pw_hash(body.password, salt))).lastrowid
                for item in random.sample(ARCHETYPES, STARTER_ITEMS):   # starter pack
                    conn.execute("INSERT INTO items(name, owner_id) VALUES (?, ?)",
                                 (item, pid))
        conn.execute("INSERT INTO sessions(token, player_id) VALUES (?,?)", (token, pid))
    response.set_cookie("session", token, httponly=True, samesite="lax")
    with db() as conn:
        row = conn.execute(
            f"SELECT {PLAYER_COLS} FROM players WHERE id=?", (pid,)).fetchone()
    return {**dict(row), "session": token}


@app.get("/v1/players/me")
async def me(request: Request, response: Response):
    return await require_player(request, response)


class ProfileIn(BaseModel):
    ln_address: Optional[str] = None


@app.put("/v1/players/me")
async def update_profile(body: ProfileIn, request: Request, response: Response):
    # a player edits only their own payout method, behind their password session
    pid = (await require_player(request, response))["id"]
    ln_address = (body.ln_address or "").strip()
    if ln_address and not LNADDR_RE.match(ln_address):
        raise HTTPException(422, "that doesn't look like a Lightning address (name@domain)")
    with tx() as conn:
        conn.execute(
            "UPDATE players SET ln_address=? WHERE id=?", (ln_address or None, pid))
    with db() as conn:
        row = conn.execute(
            f"SELECT {PLAYER_COLS} FROM players WHERE id=?", (pid,)).fetchone()
    return dict(row)


@app.get("/v1/items")
def items():
    with tx() as conn:
        release_stale_holds(conn)
        rows = conn.execute(
            """SELECT i.id, i.name, i.owner_id, p.name AS owner_name,
                      l.id AS listing_id, l.price_msat, l.status AS listing_status
               FROM items i JOIN players p ON p.id = i.owner_id
               LEFT JOIN listings l ON l.item_id = i.id
                    AND l.status IN ('open','in_flight')
               ORDER BY i.id""").fetchall()
    return [dict(r) for r in rows]


@app.post("/v1/listings")
async def create_listing(body: ListingIn, request: Request, response: Response):
    seller = (await require_player(request, response))["id"]
    with tx() as conn:
        release_stale_holds(conn)
        if conn.execute("SELECT 1 FROM items WHERE id=? AND reserved_by IS NOT NULL",
                        (body.item_id,)).fetchone():
            raise HTTPException(409, "item is frozen by a sale in progress")
        # purge old listing rows for this item (completed/cancelled) so the
        # UNIQUE(item_id) constraint allows re-listing for back-and-forth trades
        conn.execute(
            "DELETE FROM listings WHERE item_id=? AND status IN ('completed','cancelled')",
            (body.item_id,))
        cur = conn.execute(
            """INSERT INTO listings(item_id, seller_id, price_msat)
               SELECT ?, owner_id, ? FROM items
               WHERE id=? AND owner_id=?""",
            (body.item_id, body.price_msat, body.item_id, seller))
        lid = cur.lastrowid if cur.rowcount == 1 else None
    if lid is None:
        raise HTTPException(409, "item not listable (not yours or already listed)")
    return {"id": lid, "status": "open"}


@app.delete("/v1/listings/{listing_id}")
async def cancel_listing(listing_id: int, request: Request, response: Response):
    seller = (await require_player(request, response))["id"]
    with tx() as conn:
        cur = conn.execute(
            "UPDATE listings SET status='cancelled' "
            "WHERE id=? AND seller_id=? AND status='open'", (listing_id, seller))
    if cur.rowcount != 1:
        raise HTTPException(409, "listing not cancellable")
    return {"status": "cancelled"}


@app.get("/v1/market")
def market():
    with tx() as conn:
        release_stale_holds(conn)
        rows = conn.execute(
            """SELECT l.id, l.item_id, l.price_msat, i.name AS item_name,
                      p.name AS seller_name
               FROM listings l JOIN items i ON i.id = l.item_id
               JOIN players p ON p.id = l.seller_id
               WHERE l.status='open' ORDER BY l.id DESC""").fetchall()
    return [dict(r) for r in rows]


@app.post("/v1/purchases")
async def purchase(body: PurchaseIn, request: Request, response: Response):
    """Buyer clicks Buy → we ask the ESCROW (swap by config) to open a sale and
    return a payment request. Game never touches sats."""
    buyer = (await require_player(request, response))["id"]
    with tx() as conn:
        release_stale_holds(conn)
        row = conn.execute(
            """SELECT l.item_id, l.seller_id, l.price_msat
               FROM listings l WHERE l.id=? AND l.status='open'""",
            (body.listing_id,)).fetchone()
        if not row:
            raise HTTPException(409, "listing not open")
        if row["seller_id"] == buyer:
            raise HTTPException(409, "you can't buy your own listing")
        seller = conn.execute(
            "SELECT ln_address FROM players WHERE id=?",
            (row["seller_id"],)).fetchone()
        conn.execute(
            "UPDATE listings SET status='in_flight', in_flight_until=NULL WHERE id=?",
            (body.listing_id,))
    if PAYOUT_MODE == "real":
        if not (seller and seller["ln_address"]):
            with tx() as c2:      # reopen the listing before refusing
                c2.execute("UPDATE listings SET status='open' WHERE id=?",
                           (body.listing_id,))
            raise HTTPException(422, "seller has no payout method configured")
        seller_payout = {"type": "lnurl", "target": seller["ln_address"]}
    else:
        seller_payout = {"type": "stub", "target": "lab"}
    payload = {   # NOTE: no fee_msat — the fee is the ESCROW's revenue and
        # decided by the escrow, returned in its 201 response
        "item_id": row["item_id"], "buyer_id": buyer, "seller_id": row["seller_id"],
        "amount_msat": row["price_msat"],
        "seller_payout": seller_payout,
    }
    headers = {"X-Escrow-Secret": SHARED_SECRET} if SHARED_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{ESCROW_URL}/v1/sales", json=payload, headers=headers)
    except httpx.HTTPError as e:
        r, err = None, str(e)
    if r is not None and 400 <= r.status_code < 500:
        # definitive refusal by the escrow -> listing back to open
        with tx() as conn:
            conn.execute(
                "UPDATE listings SET status='open' WHERE id=?", (body.listing_id,))
        raise HTTPException(409, f"escrow refused: {r.text}")
    if r is None or r.status_code >= 500:
        # AMBIGUOUS: the escrow may have minted an invoice we never saw, and
        # it would never tell us about it. Keep the listing held until
        # such an invoice must have expired, then it reopens by itself.
        with tx() as conn:
            conn.execute(
                "UPDATE listings SET in_flight_until=? WHERE id=?",
                (int(time.time()) + AMBIGUOUS_HOLD_S, body.listing_id))
        raise HTTPException(502, "escrow unreachable/ambiguous: "
                                 + (r.text if r is not None else err))
    sale = r.json()
    with tx() as conn:
        conn.execute(
            "INSERT INTO purchases(payment_hash, buyer_id, listing_id, sale_id) "
            "VALUES (?,?,?,?)",
            (sale["payment_hash"], buyer, body.listing_id, sale["sale_id"]))
    return sale  # {sale_id, payment_request, payment_hash, expires_at}


@app.get("/v1/purchases/{payment_hash}")
async def purchase_status(payment_hash: str, request: Request, response: Response):
    await require_player(request, response)
    with db() as conn:
        row = conn.execute(
            "SELECT status, sale_id FROM purchases WHERE payment_hash=?",
            (payment_hash,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown purchase")
    # the escrow's current step, so the player can watch the trade progress;
    # purely informational (ownership truth stays in this DB)
    escrow_state = None
    if row["sale_id"]:
        headers = {"X-Escrow-Secret": SHARED_SECRET} if SHARED_SECRET else {}
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{ESCROW_URL}/v1/sales/{row['sale_id']}",
                                     headers=headers)
            if r.status_code == 200:
                escrow_state = r.json().get("state")
        except httpx.HTTPError:
            pass
    return {"status": row["status"], "escrow_state": escrow_state}


@app.post("/v1/purchases/{payment_hash}/simulate-pay")
async def simulate_pay(payment_hash: str, request: Request, response: Response):
    """PWA calls this when the buyer taps the QR — stands in for the wallet
    submitting the Lightning payment. Forwards to the escrow's /pay."""
    await require_player(request, response)
    with db() as conn:
        row = conn.execute(
            "SELECT sale_id, status FROM purchases WHERE payment_hash=?",
            (payment_hash,)).fetchone()
    if not row or not row["sale_id"]:
        raise HTTPException(404, "unknown purchase")
    if row["status"] != "pending":
        return {"status": row["status"]}
    headers = {"X-Escrow-Secret": SHARED_SECRET} if SHARED_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                f"{ESCROW_URL}/v1/sales/{row['sale_id']}/pay", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"escrow unreachable: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"escrow: {r.text}")
    return {"status": "payment submitted"}


# ---------------------------------------------------------------- ESCROW CONTRACT (spec §3.2)

def _rejected(reason: str) -> JSONResponse:
    return JSONResponse({"status": "rejected", "reason": reason}, 409)


@app.post("/reserve_sale")
def reserve_sale(body: ReserveIn, x_escrow_secret: str = Header(default="")):
    """Freeze the item at the seller for this sale. Called once the buyer's
    payment is held; nothing changes hands yet."""
    check_secret(x_escrow_secret)
    with tx() as conn:
        prior = conn.execute(
            "SELECT status FROM sales WHERE payment_hash=?",
            (body.idempotency_key,)).fetchone()
        if prior and prior["status"] == "reserved":
            return {"status": "already_reserved"}
        if prior and prior["status"] == "committed":
            return {"status": "already_committed"}
        if prior:                                   # released: the key is spent
            return _rejected("sale_released")
        listing = conn.execute(
            "SELECT id FROM listings WHERE item_id=? AND seller_id=? "
            "AND status IN ('open','in_flight')",
            (body.item_id, body.seller_id)).fetchone()
        item = conn.execute(
            "SELECT owner_id, reserved_by FROM items WHERE id=?",
            (body.item_id,)).fetchone()
        if (not listing or not item or item["owner_id"] != body.seller_id
                or item["reserved_by"] is not None):
            return _rejected("item_unavailable")
        conn.execute("UPDATE items SET reserved_by=? WHERE id=?",
                     (body.idempotency_key, body.item_id))
        conn.execute("UPDATE listings SET status='in_flight', in_flight_until=NULL "
                     "WHERE id=?", (listing["id"],))
        conn.execute(
            """INSERT INTO sales(payment_hash, item_id, buyer_id, seller_id,
                                 listing_id, status)
               VALUES (?,?,?,?,?, 'reserved')""",
            (body.idempotency_key, body.item_id, body.buyer_id, body.seller_id,
             listing["id"]))
    return {"status": "reserved"}


@app.post("/commit_sale")
def commit_sale(body: SaleKeyIn, x_escrow_secret: str = Header(default="")):
    """Hand the frozen item to the buyer. Called only after the money is
    final, so this must not fail for a reserved sale."""
    check_secret(x_escrow_secret)
    with tx() as conn:
        sale = conn.execute(
            "SELECT status, item_id, buyer_id, listing_id FROM sales "
            "WHERE payment_hash=?", (body.idempotency_key,)).fetchone()
        if sale is None:
            return _rejected("unknown_sale")
        if sale["status"] == "committed":
            return {"status": "already_committed"}
        if sale["status"] != "reserved":
            return _rejected(f"sale_{sale['status']}")
        conn.execute("UPDATE items SET owner_id=?, reserved_by=NULL "
                     "WHERE id=? AND reserved_by=?",
                     (sale["buyer_id"], sale["item_id"], body.idempotency_key))
        conn.execute("UPDATE listings SET status='completed' WHERE id=?",
                     (sale["listing_id"],))
        conn.execute("UPDATE sales SET status='committed' WHERE payment_hash=?",
                     (body.idempotency_key,))
        conn.execute("UPDATE purchases SET status='completed' WHERE payment_hash=?",
                     (body.idempotency_key,))
    return {"status": "committed"}


@app.post("/release_sale")
def release_sale(body: SaleKeyIn, x_escrow_secret: str = Header(default="")):
    """Unfreeze the item (it never left the seller) and reopen its listing.
    Refused once committed: that item belongs to the buyer now."""
    check_secret(x_escrow_secret)
    with tx() as conn:
        sale = conn.execute(
            "SELECT status, item_id, listing_id FROM sales WHERE payment_hash=?",
            (body.idempotency_key,)).fetchone()
        if sale is None:
            # never reserved here (e.g. reserve was rejected or never arrived);
            # record it so a late reserve with this key can't land afterwards
            conn.execute(
                """INSERT INTO sales(payment_hash, item_id, buyer_id, seller_id, status)
                   VALUES (?, 0, 0, 0, 'released')""", (body.idempotency_key,))
            return {"status": "not_found_ignored"}
        if sale["status"] == "released":
            return {"status": "already_released"}
        if sale["status"] == "committed":
            return _rejected("sale_committed")
        conn.execute("UPDATE items SET reserved_by=NULL WHERE id=? AND reserved_by=?",
                     (sale["item_id"], body.idempotency_key))
        conn.execute("UPDATE listings SET status='open' WHERE id=? AND status='in_flight'",
                     (sale["listing_id"],))
        conn.execute("UPDATE sales SET status='released' WHERE payment_hash=?",
                     (body.idempotency_key,))
        conn.execute("UPDATE purchases SET status='cancelled' WHERE payment_hash=?",
                     (body.idempotency_key,))
    return {"status": "released"}


@app.get("/sale_status")
def sale_status(key: str, x_escrow_secret: str = Header(default="")):
    """reserved | committed | released | not_found (for reconciliation)"""
    check_secret(x_escrow_secret)
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM sales WHERE payment_hash=?", (key,)).fetchone()
    return {"status": row["status"] if row else "not_found"}


# optional webhook from escrow (spec §3.1): terminal state notification.
# Ownership is settled by commit/release; this only closes purchases that
# never got that far (e.g. invoice expired unpaid).
@app.post("/internal/sale_result")
def sale_result(body: SaleResultIn, x_escrow_secret: str = Header(default="")):
    check_secret(x_escrow_secret)
    if body.result == "completed":
        with tx() as conn:
            conn.execute(
                "UPDATE purchases SET status='completed' WHERE payment_hash=?",
                (body.payment_hash,))
    elif body.result == "cancelled":
        with tx() as conn:
            conn.execute(
                "UPDATE purchases SET status='cancelled' WHERE payment_hash=? "
                "AND status='pending'", (body.payment_hash,))
            row = conn.execute(
                "SELECT listing_id FROM purchases WHERE payment_hash=?",
                (body.payment_hash,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE listings SET status='open' WHERE id=? AND status='in_flight'",
                    (row["listing_id"],))
    return {"status": "ok"}


web_dir = Path(os.environ.get("WEB_DIR")
               or Path(__file__).resolve().parent.parent / "game-web")
app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
