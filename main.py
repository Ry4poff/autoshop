import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mrshop-autoshop")

DB_LOCK = asyncio.Lock()
LOCAL_STOCK_LOCKS: Dict[str, asyncio.Lock] = {}


class ShopError(Exception):
    pass


class OutOfStock(ShopError):
    pass


class ConfigError(ShopError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: List[int]
    support_username: str
    shop_name: str
    database_path: str
    products_file: str
    check_deposits_every_seconds: int
    min_deposit_eur: Decimal
    deposit_expiration_hours: int
    enabled_networks: List[str]
    ltc_address: str
    sol_address: str
    ltc_confirmations: int
    ltc_eur_fallback: Decimal
    sol_eur_fallback: Decimal
    sol_rpc_url: str
    antistock_enabled: bool
    antistock_base_url: str
    antistock_api_token: str
    antistock_shop_id: str
    antistock_delete_strategy: str
    antistock_delete_custom_template: str


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).replace(",", ".").strip())


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN manquant dans les variables d'environnement.")

    admin_ids = []
    for part in os.getenv("ADMIN_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                admin_ids.append(int(part))
            except ValueError:
                raise ConfigError(f"ADMIN_IDS contient une valeur invalide: {part}")

    networks = [n.strip().upper() for n in os.getenv("ENABLED_NETWORKS", "LTC,SOL").split(",") if n.strip()]

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        support_username=os.getenv("SUPPORT_USERNAME", "@support").strip(),
        shop_name=os.getenv("SHOP_NAME", "MRSHOP").strip(),
        database_path=os.getenv("DATABASE_PATH", "shop.db").strip(),
        products_file=os.getenv("PRODUCTS_FILE", "products.json").strip(),
        check_deposits_every_seconds=int(os.getenv("CHECK_DEPOSITS_EVERY_SECONDS", "45")),
        min_deposit_eur=env_decimal("MIN_DEPOSIT_EUR", "2"),
        deposit_expiration_hours=int(os.getenv("DEPOSIT_EXPIRATION_HOURS", "24")),
        enabled_networks=networks,
        ltc_address=os.getenv("LTC_ADDRESS", "").strip(),
        sol_address=os.getenv("SOL_ADDRESS", "").strip(),
        ltc_confirmations=int(os.getenv("LTC_CONFIRMATIONS", "2")),
        ltc_eur_fallback=env_decimal("LTC_EUR_FALLBACK", "75"),
        sol_eur_fallback=env_decimal("SOL_EUR_FALLBACK", "130"),
        sol_rpc_url=os.getenv("SOL_RPC_URL", "https://api.mainnet-beta.solana.com").strip(),
        antistock_enabled=env_bool("ANTISTOCK_ENABLED", False),
        antistock_base_url=os.getenv("ANTISTOCK_BASE_URL", "https://business-api.antistock.io").rstrip("/"),
        antistock_api_token=os.getenv("ANTISTOCK_API_TOKEN", "").strip(),
        antistock_shop_id=os.getenv("ANTISTOCK_SHOP_ID", "").strip(),
        antistock_delete_strategy=os.getenv("ANTISTOCK_DELETE_STRATEGY", "id_list").strip(),
        antistock_delete_custom_template=os.getenv("ANTISTOCK_DELETE_CUSTOM_TEMPLATE", '{"ids":["{{id}}"]}').strip(),
    )


def money_to_cents(amount: Decimal) -> int:
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_money(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}€"


def parse_eur_amount(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Montant invalide.")
    if amount <= 0:
        raise ValueError("Montant invalide.")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass
class Product:
    id: str
    name: str
    description: str
    price_cents: int
    warehouse_id: str
    antistock_product_id: str


def load_products(config: Config) -> List[Product]:
    # Skip loading products.json when using Antistock API
    if config.antistock_enabled:
        return []
    
    path = Path(config.products_file)
    if not path.exists():
        raise ConfigError(f"Fichier produits introuvable: {config.products_file}")
    data = json.loads(path.read_text(encoding="utf-8"))
    products = []
    for item in data:
        products.append(
            Product(
                id=str(item["id"]),
                name=str(item["name"]),
                description=str(item.get("description", "")),
                price_cents=money_to_cents(Decimal(str(item["price_eur"]))),
                warehouse_id=str(item.get("warehouse_id", "")),
                antistock_product_id=str(item.get("antistock_product_id", "")),
            )
        )
    return products


def product_map(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Product]:
    return {p.id: p for p in context.application.bot_data["products"]}


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS deposits (
                    id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    network TEXT NOT NULL,
                    address TEXT NOT NULL,
                    amount_eur_cents INTEGER NOT NULL,
                    expected_units INTEGER NOT NULL,
                    expected_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tx_hash TEXT,
                    created_at INTEGER NOT NULL,
                    credited_at INTEGER
                )
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_deposits_tx_hash
                ON deposits(tx_hash) WHERE tx_hash IS NOT NULL
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    delivery_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.commit()

    async def ensure_user(self, telegram_id: int) -> None:
        async with DB_LOCK:
            with self.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO users(telegram_id, balance_cents, created_at) VALUES (?, 0, ?)",
                    (telegram_id, int(time.time())),
                )
                db.commit()

    async def get_balance(self, telegram_id: int) -> int:
        await self.ensure_user(telegram_id)
        async with DB_LOCK:
            with self.connect() as db:
                row = db.execute("SELECT balance_cents FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
                return int(row["balance_cents"] if row else 0)

    async def add_balance(self, telegram_id: int, cents: int) -> int:
        await self.ensure_user(telegram_id)
        async with DB_LOCK:
            with self.connect() as db:
                db.execute(
                    "UPDATE users SET balance_cents = balance_cents + ? WHERE telegram_id = ?",
                    (cents, telegram_id),
                )
                row = db.execute("SELECT balance_cents FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
                db.commit()
                return int(row["balance_cents"])

    async def set_balance(self, telegram_id: int, cents: int) -> int:
        await self.ensure_user(telegram_id)
        async with DB_LOCK:
            with self.connect() as db:
                db.execute("UPDATE users SET balance_cents = ? WHERE telegram_id = ?", (cents, telegram_id))
                db.commit()
                return cents

    async def deduct_balance_if_enough(self, telegram_id: int, cents: int) -> bool:
        await self.ensure_user(telegram_id)
        async with DB_LOCK:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT balance_cents FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
                current = int(row["balance_cents"] if row else 0)
                if current < cents:
                    db.rollback()
                    return False
                db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE telegram_id = ?", (cents, telegram_id))
                db.commit()
                return True

    async def create_deposit(self, deposit: Dict[str, Any]) -> None:
        async with DB_LOCK:
            with self.connect() as db:
                db.execute(
                    """
                    INSERT INTO deposits(id, telegram_id, network, address, amount_eur_cents,
                    expected_units, expected_text, status, tx_hash, created_at, credited_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                    """,
                    (
                        deposit["id"],
                        deposit["telegram_id"],
                        deposit["network"],
                        deposit["address"],
                        deposit["amount_eur_cents"],
                        deposit["expected_units"],
                        deposit["expected_text"],
                        deposit["created_at"],
                    ),
                )
                db.commit()

    async def pending_deposits(self) -> List[sqlite3.Row]:
        async with DB_LOCK:
            with self.connect() as db:
                return list(db.execute("SELECT * FROM deposits WHERE status = 'pending' ORDER BY created_at ASC").fetchall())

    async def expire_old_deposits(self, hours: int) -> int:
        cutoff = int(time.time()) - hours * 3600
        async with DB_LOCK:
            with self.connect() as db:
                cur = db.execute(
                    "UPDATE deposits SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                    (cutoff,),
                )
                db.commit()
                return int(cur.rowcount or 0)

    async def credit_deposit(self, deposit_id: str, telegram_id: int, amount_cents: int, tx_hash: str) -> bool:
        async with DB_LOCK:
            with self.connect() as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT status FROM deposits WHERE id = ?", (deposit_id,)).fetchone()
                    if not row or row["status"] != "pending":
                        db.rollback()
                        return False
                    duplicate = db.execute("SELECT id FROM deposits WHERE tx_hash = ?", (tx_hash,)).fetchone()
                    if duplicate:
                        db.rollback()
                        return False
                    db.execute(
                        "UPDATE deposits SET status = 'credited', tx_hash = ?, credited_at = ? WHERE id = ?",
                        (tx_hash, int(time.time()), deposit_id),
                    )
                    db.execute(
                        "UPDATE users SET balance_cents = balance_cents + ? WHERE telegram_id = ?",
                        (amount_cents, telegram_id),
                    )
                    db.commit()
                    return True
                except sqlite3.IntegrityError:
                    db.rollback()
                    return False

    async def create_order(self, telegram_id: int, product: Product, delivery_text: str) -> str:
        order_id = str(uuid.uuid4())
        async with DB_LOCK:
            with self.connect() as db:
                db.execute(
                    """
                    INSERT INTO orders(id, telegram_id, product_id, product_name, price_cents, delivery_text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        telegram_id,
                        product.id,
                        product.name,
                        product.price_cents,
                        delivery_text,
                        int(time.time()),
                    ),
                )
                db.commit()
        return order_id


@dataclass
class StockItem:
    content: str
    item_id: Optional[str]
    raw: Any


class AntistockClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self.config = config
        self.session = session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.antistock_api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.config.antistock_api_token or not self.config.antistock_shop_id:
            raise ConfigError("ANTISTOCK_API_TOKEN ou ANTISTOCK_SHOP_ID manquant.")
        url = f"{self.config.antistock_base_url}{path}"
        async with self.session.request(method, url, headers=self._headers(), timeout=30, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ShopError(f"Antistock HTTP {resp.status}: {text[:500]}")
            try:
                return json.loads(text) if text else {}
            except json.JSONDecodeError:
                return {"raw_text": text}

    async def raw_stock(self, warehouse_id: str) -> Any:
        shop_id = self.config.antistock_shop_id
        path = f"/v1/dash/shops/{shop_id}/warehouses/{warehouse_id}/stock/raw"
        return await self._request("GET", path)

    def _extract_items(self, payload: Any) -> List[StockItem]:
        items: List[StockItem] = []
        content_keys = ["content", "raw", "value", "text", "code", "key", "license", "account", "data"]
        id_keys = ["id", "stockId", "stock_id", "itemId", "item_id"]

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                item_id = None
                for key in id_keys:
                    if key in obj and obj[key] is not None:
                        item_id = str(obj[key])
                        break
                content = None
                for key in content_keys:
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        content = val.strip()
                        break
                if content:
                    items.append(StockItem(content=content, item_id=item_id, raw=obj))
                    return
                for val in obj.values():
                    walk(val)
            elif isinstance(obj, list):
                for val in obj:
                    walk(val)
            elif isinstance(obj, str) and obj.strip():
                if len(obj.strip()) <= 5000:
                    items.append(StockItem(content=obj.strip(), item_id=None, raw=obj))

        walk(payload)
        # Anti-doublon par contenu
        seen = set()
        unique = []
        for item in items:
            key = (item.item_id, item.content)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    async def stock_count(self, product: Product) -> int:
        if not self.config.antistock_enabled:
            return await local_stock_count(product.id)
        if not product.warehouse_id or product.warehouse_id.startswith("REMPLACE"):
            return 0
        raw = await self.raw_stock(product.warehouse_id)
        return len(self._extract_items(raw))

    async def claim_one(self, product: Product) -> str:
        if not self.config.antistock_enabled:
            return await claim_local_stock(product.id)

        if not product.warehouse_id or product.warehouse_id.startswith("REMPLACE"):
            raise ConfigError(f"warehouse_id manquant pour {product.name} dans products.json")

        raw = await self.raw_stock(product.warehouse_id)
        items = self._extract_items(raw)
        if not items:
            raise OutOfStock("Stock vide sur Antistock.")
        selected = items[0]
        await self._delete_stock_item(product, selected)
        return selected.content

    async def _delete_stock_item(self, product: Product, item: StockItem) -> None:
        strategy = self.config.antistock_delete_strategy
        shop_id = self.config.antistock_shop_id
        path = f"/v1/dash/shops/{shop_id}/warehouses/{product.warehouse_id}/delete-stock"

        if strategy == "id_list":
            if not item.item_id:
                raise ConfigError("ANTISTOCK_DELETE_STRATEGY=id_list mais aucun id trouvé dans la ligne de stock.")
            body = {"ids": [item.item_id]}
        elif strategy == "stock_ids":
            if not item.item_id:
                raise ConfigError("ANTISTOCK_DELETE_STRATEGY=stock_ids mais aucun id trouvé dans la ligne de stock.")
            body = {"stockIds": [item.item_id]}
        elif strategy == "raw_lines":
            body = {"stock": [item.content]}
        elif strategy == "custom":
            template = self.config.antistock_delete_custom_template
            filled = (
                template.replace("{{id}}", item.item_id or "")
                .replace("{{content}}", item.content.replace('"', '\\"'))
                .replace("{{product_id}}", product.antistock_product_id)
                .replace("{{warehouse_id}}", product.warehouse_id)
            )
            try:
                body = json.loads(filled)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"ANTISTOCK_DELETE_CUSTOM_TEMPLATE invalide: {exc}")
        else:
            raise ConfigError("ANTISTOCK_DELETE_STRATEGY invalide.")

        await self._request("POST", path, json=body)


async def local_stock_count(product_id: str) -> int:
    path = Path("local_stock") / f"{product_id}.txt"
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


async def claim_local_stock(product_id: str) -> str:
    lock = LOCAL_STOCK_LOCKS.setdefault(product_id, asyncio.Lock())
    async with lock:
        path = Path("local_stock") / f"{product_id}.txt"
        if not path.exists():
            raise OutOfStock("Stock local introuvable.")
        lines = [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines()]
        clean = [line for line in lines if line.strip()]
        if not clean:
            raise OutOfStock("Stock vide.")
        item = clean[0]
        remaining = clean[1:]
        path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        return item


class CryptoService:
    def __init__(self, config: Config, db: Database, session: aiohttp.ClientSession):
        self.config = config
        self.db = db
        self.session = session

    async def price_eur(self, network: str) -> Decimal:
        network = network.upper()
        coin_id = "litecoin" if network == "LTC" else "solana"
        fallback = self.config.ltc_eur_fallback if network == "LTC" else self.config.sol_eur_fallback
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=eur"
        try:
            async with self.session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
                price = Decimal(str(data[coin_id]["eur"]))
                if price > 0:
                    return price
        except Exception as exc:
            log.warning("Prix CoinGecko indisponible pour %s, fallback utilisé: %s", network, exc)
        return fallback

    def address_for_network(self, network: str) -> str:
        network = network.upper()
        if network == "LTC":
            return self.config.ltc_address
        if network == "SOL":
            return self.config.sol_address
        raise ConfigError("Réseau non supporté.")

    def units_to_text(self, network: str, units: int) -> str:
        if network == "LTC":
            return f"{Decimal(units) / Decimal(100_000_000):.8f} LTC"
        if network == "SOL":
            return f"{Decimal(units) / Decimal(1_000_000_000):.9f} SOL"
        return str(units)

    async def create_invoice(self, telegram_id: int, amount_eur: Decimal, network: str) -> Dict[str, Any]:
        network = network.upper()
        if network not in self.config.enabled_networks:
            raise ConfigError("Réseau désactivé.")
        address = self.address_for_network(network)
        if not address:
            raise ConfigError(f"Adresse {network} manquante dans .env")

        rate = await self.price_eur(network)
        units_per_coin = Decimal(100_000_000 if network == "LTC" else 1_000_000_000)
        base_units = int(((amount_eur / rate) * units_per_coin).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        # Petite variation pour lier le dépôt au client. Ne change presque rien au montant réel.
        suffix = random.randint(1, 999)
        expected_units = base_units + suffix
        dep = {
            "id": str(uuid.uuid4()),
            "telegram_id": telegram_id,
            "network": network,
            "address": address,
            "amount_eur_cents": money_to_cents(amount_eur),
            "expected_units": expected_units,
            "expected_text": self.units_to_text(network, expected_units),
            "created_at": int(time.time()),
        }
        await self.db.create_deposit(dep)
        return dep

    async def check_pending(self, app: Application) -> None:
        expired = await self.db.expire_old_deposits(self.config.deposit_expiration_hours)
        if expired:
            log.info("Dépôts expirés: %s", expired)

        pending = await self.db.pending_deposits()
        if not pending:
            return

        ltc_outputs = []
        sol_outputs = []
        networks = {row["network"] for row in pending}
        if "LTC" in networks and self.config.ltc_address:
            ltc_outputs = await self.fetch_ltc_outputs(self.config.ltc_address)
        if "SOL" in networks and self.config.sol_address:
            sol_outputs = await self.fetch_sol_outputs(self.config.sol_address)

        outputs_by_network = {"LTC": ltc_outputs, "SOL": sol_outputs}
        tolerance = Decimal("0.002")  # 0.2% de marge arrondi wallet

        for row in pending:
            network = row["network"]
            expected = int(row["expected_units"])
            min_units = int(Decimal(expected) * (Decimal("1") - tolerance))
            max_units = int(Decimal(expected) * (Decimal("1") + tolerance))
            candidates = outputs_by_network.get(network, [])
            for tx_hash, units in candidates:
                if min_units <= units <= max_units:
                    credited = await self.db.credit_deposit(
                        row["id"],
                        int(row["telegram_id"]),
                        int(row["amount_eur_cents"]),
                        tx_hash,
                    )
                    if credited:
                        msg = (
                            f"✅ <b>Dépôt confirmé</b>\n\n"
                            f"Montant crédité: <b>{cents_to_money(int(row['amount_eur_cents']))}</b>\n"
                            f"Réseau: <b>{network}</b>\n"
                            f"Solde mis à jour."
                        )
                        await app.bot.send_message(chat_id=int(row["telegram_id"]), text=msg, parse_mode=ParseMode.HTML)
                        log.info("Deposit credited: %s %s", row["id"], tx_hash)
                    break

    async def fetch_ltc_outputs(self, address: str) -> List[Tuple[str, int]]:
        url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?limit=50"
        out: List[Tuple[str, int]] = []
        try:
            async with self.session.get(url, timeout=25) as resp:
                if resp.status != 200:
                    log.warning("BlockCypher LTC HTTP %s", resp.status)
                    return out
                data = await resp.json()
            for tx in data.get("txs", []):
                confirmations = int(tx.get("confirmations", 0) or 0)
                if confirmations < self.config.ltc_confirmations:
                    continue
                tx_hash = str(tx.get("hash", ""))
                total = 0
                for o in tx.get("outputs", []):
                    if address in o.get("addresses", []):
                        total += int(o.get("value", 0) or 0)
                if tx_hash and total > 0:
                    out.append((tx_hash, total))
        except Exception as exc:
            log.warning("Erreur fetch LTC: %s", exc)
        return out

    async def fetch_sol_outputs(self, address: str) -> List[Tuple[str, int]]:
        out: List[Tuple[str, int]] = []
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": 40}],
            }
            async with self.session.post(self.config.sol_rpc_url, json=payload, timeout=25) as resp:
                if resp.status != 200:
                    log.warning("SOL RPC signatures HTTP %s", resp.status)
                    return out
                sigs = (await resp.json()).get("result", [])

            for sig_obj in sigs:
                sig = sig_obj.get("signature")
                if not sig:
                    continue
                tx_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}],
                }
                async with self.session.post(self.config.sol_rpc_url, json=tx_payload, timeout=25) as resp:
                    if resp.status != 200:
                        continue
                    tx = (await resp.json()).get("result")
                if not tx or not tx.get("meta"):
                    continue
                message = tx.get("transaction", {}).get("message", {})
                keys = message.get("accountKeys", [])
                index = None
                for i, key in enumerate(keys):
                    pubkey = key.get("pubkey") if isinstance(key, dict) else str(key)
                    if pubkey == address:
                        index = i
                        break
                if index is None:
                    continue
                pre = tx["meta"].get("preBalances", [])
                post = tx["meta"].get("postBalances", [])
                if index < len(pre) and index < len(post):
                    delta = int(post[index]) - int(pre[index])
                    if delta > 0:
                        out.append((sig, delta))
        except Exception as exc:
            log.warning("Erreur fetch SOL: %s", exc)
        return out


async def get_services(context: ContextTypes.DEFAULT_TYPE) -> Tuple[Config, Database, AntistockClient, CryptoService]:
    return (
        context.application.bot_data["config"],
        context.application.bot_data["db"],
        context.application.bot_data["antistock"],
        context.application.bot_data["crypto"],
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 Shop", callback_data="shop")],
            [InlineKeyboardButton("💰 Mon solde", callback_data="balance"), InlineKeyboardButton("➕ Déposer", callback_data="deposit_menu")],
            [InlineKeyboardButton("🆘 Support", callback_data="support")],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, db, _, _ = await get_services(context)
    user = update.effective_user
    if user:
        await db.ensure_user(user.id)
    text = (
        f"👋 Bienvenue sur <b>{escape_html(config.shop_name)}</b>\n\n"
        "• Dépose en crypto\n"
        "• Le bot crédite ton solde après confirmation blockchain\n"
        "• Achète instantanément avec ton solde\n\n"
        "⚠️ Aucun withdraw n'est disponible sur ce bot."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db, _, _ = await get_services(context)
    user_id = update.effective_user.id
    bal = await db.get_balance(user_id)
    await update.message.reply_text(f"💰 Ton solde: <b>{cents_to_money(bal)}</b>", parse_mode=ParseMode.HTML)


async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_shop(update, context)


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _, _, crypto = await get_services(context)
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Exemple: /deposit 10 LTC\nOu utilise le bouton ➕ Déposer.")
        return
    try:
        amount = parse_eur_amount(context.args[0])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    network = context.args[1].upper() if len(context.args) >= 2 else (config.enabled_networks[0] if config.enabled_networks else "LTC")
    await create_and_send_invoice(update.effective_user.id, update.effective_chat.id, amount, network, context)


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("❌ Withdraw désactivé. Le solde sert uniquement aux achats sur le bot.")


async def send_shop(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    products: List[Product] = context.application.bot_data["products"]
    if not products:
        text = "Aucun produit configuré."
        markup = back_menu()
    else:
        rows = []
        for p in products:
            rows.append([InlineKeyboardButton(f"{p.name} — {cents_to_money(p.price_cents)}", callback_data=f"product:{p.id}")])
        rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
        text = "🛒 <b>Shop</b>\n\nChoisis un produit."
        markup = InlineKeyboardMarkup(rows)

    if update.callback_query and edit:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def show_product(query, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    _, _, antistock, _ = await get_services(context)
    products = product_map(context)
    product = products.get(product_id)
    if not product:
        await query.answer("Produit introuvable", show_alert=True)
        return

    try:
        count = await antistock.stock_count(product)
        stock_line = f"Stock détecté: <b>{count}</b>"
    except Exception as exc:
        log.warning("stock count error: %s", exc)
        stock_line = "Stock: <b>à vérifier</b>"

    text = (
        f"🛒 <b>{escape_html(product.name)}</b>\n\n"
        f"{escape_html(product.description)}\n\n"
        f"Prix: <b>{cents_to_money(product.price_cents)}</b>\n"
        f"{stock_line}"
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Acheter", callback_data=f"buy:{product.id}")],
            [InlineKeyboardButton("⬅️ Shop", callback_data="shop")],
        ]
    )
    await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def buy_product(query, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    _, db, antistock, _ = await get_services(context)
    user_id = query.from_user.id
    products = product_map(context)
    product = products.get(product_id)
    if not product:
        await query.answer("Produit introuvable", show_alert=True)
        return

    await db.ensure_user(user_id)
    if not await db.deduct_balance_if_enough(user_id, product.price_cents):
        bal = await db.get_balance(user_id)
        await query.answer("Solde insuffisant", show_alert=True)
        await query.edit_message_text(
            f"❌ Solde insuffisant.\n\nTon solde: <b>{cents_to_money(bal)}</b>\nPrix: <b>{cents_to_money(product.price_cents)}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Déposer", callback_data="deposit_menu")], [InlineKeyboardButton("⬅️ Shop", callback_data="shop")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text("⏳ Achat en cours, récupération du stock...")
    try:
        delivery = await antistock.claim_one(product)
        order_id = await db.create_order(user_id, product, delivery)
        text = (
            f"✅ <b>Achat confirmé</b>\n\n"
            f"Produit: <b>{escape_html(product.name)}</b>\n"
            f"Commande: <code>{order_id[:8]}</code>\n\n"
            f"📦 <b>Livraison:</b>\n<code>{escape_html(delivery)}</code>"
        )
        await query.edit_message_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
    except OutOfStock:
        await db.add_balance(user_id, product.price_cents)
        await query.edit_message_text(
            "❌ Stock vide. Ton solde a été remboursé automatiquement.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Shop", callback_data="shop")]]),
        )
    except Exception as exc:
        await db.add_balance(user_id, product.price_cents)
        log.exception("buy failed")
        await query.edit_message_text(
            f"❌ Erreur pendant l'achat. Ton solde a été remboursé.\n\nErreur: <code>{escape_html(str(exc)[:500])}</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Support", callback_data="support")], [InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
            parse_mode=ParseMode.HTML,
        )


async def create_and_send_invoice(user_id: int, chat_id: int, amount: Decimal, network: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _, _, crypto = await get_services(context)
    if amount < config.min_deposit_eur:
        await context.bot.send_message(chat_id=chat_id, text=f"Minimum dépôt: {config.min_deposit_eur:.2f}€")
        return
    try:
        dep = await crypto.create_invoice(user_id, amount, network)
    except Exception as exc:
        await context.bot.send_message(chat_id=chat_id, text=f"Erreur dépôt: {exc}")
        return

    text = (
        f"➕ <b>Dépôt {dep['network']}</b>\n\n"
        f"Montant à créditer: <b>{cents_to_money(dep['amount_eur_cents'])}</b>\n"
        f"Adresse:\n<code>{escape_html(dep['address'])}</code>\n\n"
        f"Montant exact à envoyer:\n<code>{escape_html(dep['expected_text'])}</code>\n\n"
        f"⏳ Le bot crédite automatiquement après confirmation blockchain.\n"
        f"⚠️ Envoie le montant exact. Aucun withdraw n'est disponible."
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    config, db, _, _ = await get_services(context)

    if data == "menu":
        text = f"🏠 <b>{escape_html(config.shop_name)}</b>\n\nChoisis une option."
        await query.edit_message_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    if data == "balance":
        bal = await db.get_balance(query.from_user.id)
        await query.edit_message_text(f"💰 Ton solde: <b>{cents_to_money(bal)}</b>", reply_markup=back_menu(), parse_mode=ParseMode.HTML)
        return

    if data == "shop":
        await send_shop(update, context, edit=True)
        return

    if data == "support":
        await query.edit_message_text(
            f"🆘 Support: {escape_html(config.support_username)}",
            reply_markup=back_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "deposit_menu":
        rows = []
        for amount in [5, 10, 25, 50]:
            rows.append([InlineKeyboardButton(f"{amount}€", callback_data=f"dep_amount:{amount}")])
        rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
        await query.edit_message_text("➕ Choisis le montant à déposer.", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("dep_amount:"):
        amount = data.split(":", 1)[1]
        rows = []
        for net in config.enabled_networks:
            rows.append([InlineKeyboardButton(net, callback_data=f"dep_net:{amount}:{net}")])
        rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
        await query.edit_message_text(f"Réseau pour dépôt de {amount}€:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("dep_net:"):
        _, amount_raw, network = data.split(":", 2)
        amount = parse_eur_amount(amount_raw)
        await create_and_send_invoice(query.from_user.id, query.message.chat_id, amount, network, context)
        return

    if data.startswith("product:"):
        await show_product(query, context, data.split(":", 1)[1])
        return

    if data.startswith("buy:"):
        await buy_product(query, context, data.split(":", 1)[1])
        return


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _, _, _ = await get_services(context)
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("Accès refusé.")
        return
    text = (
        "🛠️ Admin\n\n"
        "/reloadproducts — recharge products.json\n"
        "/addbalance TELEGRAM_ID MONTANT_EUR\n"
        "/setbalance TELEGRAM_ID MONTANT_EUR\n"
        "/checkdeps — vérifie les dépôts maintenant"
    )
    await update.message.reply_text(text)


async def reloadproducts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _, _, _ = await get_services(context)
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("Accès refusé.")
        return
    context.application.bot_data["products"] = load_products(config)
    await update.message.reply_text(f"✅ {len(context.application.bot_data['products'])} produits rechargés.")


async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, db, _, _ = await get_services(context)
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("Accès refusé.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addbalance TELEGRAM_ID MONTANT_EUR")
        return
    try:
        user_id = int(context.args[0])
        amount = parse_eur_amount(context.args[1])
    except Exception as exc:
        await update.message.reply_text(f"Erreur: {exc}")
        return
    new_bal = await db.add_balance(user_id, money_to_cents(amount))
    await update.message.reply_text(f"✅ Nouveau solde {user_id}: {cents_to_money(new_bal)}")


async def setbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, db, _, _ = await get_services(context)
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("Accès refusé.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setbalance TELEGRAM_ID MONTANT_EUR")
        return
    try:
        user_id = int(context.args[0])
        amount = parse_eur_amount(context.args[1])
    except Exception as exc:
        await update.message.reply_text(f"Erreur: {exc}")
        return
    new_bal = await db.set_balance(user_id, money_to_cents(amount))
    await update.message.reply_text(f"✅ Nouveau solde {user_id}: {cents_to_money(new_bal)}")


async def checkdeps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _, _, crypto = await get_services(context)
    if update.effective_user.id not in config.admin_ids:
        await update.message.reply_text("Accès refusé.")
        return
    await update.message.reply_text("Vérification dépôts lancée...")
    await crypto.check_pending(context.application)
    await update.message.reply_text("✅ Vérification terminée.")


async def deposit_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    crypto: CryptoService = context.application.bot_data["crypto"]
    await crypto.check_pending(context.application)


async def post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]
    db: Database = app.bot_data["db"]

    session = aiohttp.ClientSession()
    app.bot_data["session"] = session
    app.bot_data["antistock"] = AntistockClient(config, session)
    app.bot_data["crypto"] = CryptoService(config, db, session)

    if config.check_deposits_every_seconds < 15:
        log.warning("CHECK_DEPOSITS_EVERY_SECONDS trop bas, ajusté à 15s")
        interval = 15
    else:
        interval = config.check_deposits_every_seconds
    app.job_queue.run_repeating(deposit_job, interval=interval, first=10, name="deposit-checker")
    log.info("Deposit checker every %ss", interval)


async def on_shutdown(app: Application) -> None:
    session: aiohttp.ClientSession = app.bot_data.get("session")
    if session and not session.closed:
        await session.close()


def build_app() -> Application:
    config = load_config()
    db = Database(config.database_path)
    db.init()
    products = load_products(config)

    app = ApplicationBuilder().token(config.bot_token).post_init(post_init).post_shutdown(on_shutdown).build()
    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["products"] = products

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("shop", shop_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("reloadproducts", reloadproducts_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("setbalance", setbalance_cmd))
    app.add_handler(CommandHandler("checkdeps", checkdeps_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


def main() -> None:
    app = build_app()
    log.info("Bot lancé.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
