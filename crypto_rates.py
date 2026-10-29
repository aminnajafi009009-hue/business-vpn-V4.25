"""قیمت لحظه‌ای ارزهای دیجیتال به تومان.

ترتیب منابع:
  1) قیمت مستقیم تومانی از نوبیتکس / والکس
  2) قیمت دلاری ارز (Binance public / CoinGecko) × قیمت تتر به تومان (نوبیتکس / والکس)
  3) آخرین قیمت زنده‌ی ذخیره‌شده (حداکثر ۱۰ دقیقه)
قیمت پشتیبان (دستی) فقط وقتی استفاده می‌شود که همه‌ی موارد بالا شکست بخورند و توسط
caller اعمال می‌شود.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

_TTL = 45          # ثانیه: قیمت تازه
_STALE_TTL = 600   # ثانیه: اگر همه‌ی منابع قطع بودند، قیمت قبلی تا این مدت معتبر است
_cache: dict[str, tuple[float, float, str]] = {}  # symbol -> (ts, price_toman, source)

_COINGECKO_IDS = {
    "TRX": "tron", "TON": "the-open-network", "USDT": "tether", "BTC": "bitcoin",
    "ETH": "ethereum", "LTC": "litecoin", "BNB": "binancecoin", "SOL": "solana",
    "XRP": "ripple", "DOGE": "dogecoin", "USDC": "usd-coin", "BCH": "bitcoin-cash",
    "ADA": "cardano", "MATIC": "matic-network", "DAI": "dai",
}


def _num(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    async with session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return await r.json(content_type=None)


# ── پارسرها (جدا از شبکه تا قابل تست باشند) ───────────────────────────────
def parse_nobitex(data, symbol: str) -> float | None:
    """نوبیتکس قیمت را به ریال می‌دهد → تومان."""
    try:
        stats = data.get("stats") or {}
        item = stats.get(f"{symbol.lower()}-rls") or {}
        v = _num(item.get("latest")) or _num(item.get("bestSell"))
        return v / 10.0 if v else None
    except Exception:
        return None


def parse_wallex(data, symbol: str) -> float | None:
    """والکس قیمت را مستقیم به تومان می‌دهد."""
    try:
        sym = (data.get("result") or {}).get("symbols") or {}
        item = sym.get(f"{symbol.upper()}TMN") or {}
        return _num((item.get("stats") or {}).get("lastPrice"))
    except Exception:
        return None


def parse_binance(data) -> float | None:
    try:
        return _num(data.get("price"))
    except Exception:
        return None


def parse_coingecko(data, symbol: str) -> float | None:
    try:
        cid = _COINGECKO_IDS.get(symbol.upper())
        return _num((data.get(cid) or {}).get("usd")) if cid else None
    except Exception:
        return None


# ── دریافت از هر منبع ────────────────────────────────────────────────────
async def _nobitex(session, symbol):
    d = await _get_json(session, "https://api.nobitex.ir/market/stats",
                        {"srcCurrency": symbol.lower(), "dstCurrency": "rls"})
    return parse_nobitex(d, symbol)


_wallex_markets_cache: dict = {"ts": 0.0, "data": None}


async def _wallex(session, symbol):
    now = time.monotonic()
    if _wallex_markets_cache["data"] is None or now - _wallex_markets_cache["ts"] > 20:
        _wallex_markets_cache["data"] = await _get_json(session, "https://api.wallex.ir/v1/markets")
        _wallex_markets_cache["ts"] = now
    return parse_wallex(_wallex_markets_cache["data"], symbol)


async def _binance_usd(session, symbol):
    if symbol.upper() in ("USDT", "USDC", "DAI"):
        return 1.0
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            d = await _get_json(session, f"{host}/api/v3/ticker/price", {"symbol": f"{symbol.upper()}USDT"})
            v = parse_binance(d)
            if v:
                return v
        except Exception:
            continue
    return None


async def _coingecko_usd(session, symbol):
    cid = _COINGECKO_IDS.get(symbol.upper())
    if not cid:
        return None
    d = await _get_json(session, "https://api.coingecko.com/api/v3/simple/price", {"ids": cid, "vs_currencies": "usd"})
    return parse_coingecko(d, symbol)


async def _safe(coro):
    try:
        return await coro
    except Exception as exc:
        logger.debug("crypto rate source failed: %s", exc)
        return None


async def fetch_live_rate_toman(symbol: str, timeout: float = 8.0) -> tuple[float | None, str]:
    """(قیمت هر واحد ارز به تومان, نام منبع) یا (None, '') اگر همه‌ی منابع شکست خوردند."""
    symbol = symbol.upper().strip()
    tmo = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=tmo) as session:
        (nob, wal, nob_usdt, wal_usdt, bin_usd, cg_usd) = await asyncio.gather(
            _safe(_nobitex(session, symbol)),
            _safe(_wallex(session, symbol)),
            _safe(_nobitex(session, "USDT")),
            _safe(_wallex(session, "USDT")),
            _safe(_binance_usd(session, symbol)),
            _safe(_coingecko_usd(session, symbol)),
        )
    if nob:
        return nob, "nobitex"
    if wal:
        return wal, "wallex"
    usdt_toman = nob_usdt or wal_usdt
    usd = bin_usd or cg_usd
    if usdt_toman and usd:
        return usd * usdt_toman, "usd×usdt"
    return None, ""


async def get_rate_toman(symbol: str) -> tuple[float | None, str]:
    """قیمت زنده با کش کوتاه. خروجی: (قیمت, منبع)؛ منبع 'cache' یعنی قیمت چند دقیقه‌ی پیش."""
    symbol = symbol.upper().strip()
    now = time.time()
    hit = _cache.get(symbol)
    if hit and now - hit[0] < _TTL:
        return hit[1], hit[2]
    price, source = await fetch_live_rate_toman(symbol)
    if price:
        _cache[symbol] = (now, price, source)
        return price, source
    if hit and now - hit[0] < _STALE_TTL:
        return hit[1], "cache"
    return None, ""
