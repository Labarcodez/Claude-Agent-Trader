#!/usr/bin/env python3
"""Minimal Kraken REST API client, stdlib only (mirrors the
dependency-free convention of research/discover_candidates.py and
backtest/fetch_history.py -- no `requests`, no npm package needed here
unlike wallet/'s Solana tx signing, since Kraken's REST auth is just
HMAC-SHA512 over stdlib primitives).

Public endpoints (AssetPairs, Ticker, OHLC, Assets) need no credentials --
everything discovery, backtesting, and paper trading use is public. Private
endpoints (Balance, AddOrder, OpenOrders, ClosedOrders) need
KRAKEN_API_KEY/KRAKEN_API_SECRET (see .env.example, docs/KRAKEN_SETUP.md)
and Kraken's own request-signing scheme, implemented in `_sign()` below
exactly per Kraken's published sample code.

Kraken returns HTTP 200 even for most application-level errors (rate
limits, bad pairs, insufficient funds, bad signature) -- the real error
lives in the JSON body's "error" array, not the HTTP status. Every function
here checks that array and raises KrakenAPIError rather than silently
returning a result with error data still attached, so a caller can never
mistake `{"error": ["EAPI:Rate limit exceeded"], "result": {}}` for a
successful empty result.

Usage (public):
    python3 -c "from kraken.client import ticker; print(ticker(['XBTUSD']))"
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.kraken.com"
USER_AGENT = "claude-agent-trader-kraken/1.0"

# Same transient-vs-definitive split as research/discover_candidates.py's
# RETRYABLE_HTTP_CODES / backtest/fetch_history.py's copy of the same --
# 429/502/503/504 are worth retrying, everything else (400/401/403/404) is
# a definitive error retrying can't fix.
RETRYABLE_HTTP_CODES = {429, 502, 503, 504}

# Kraken's own rate-limit error string (HTTP 200, error array populated) --
# distinct from an HTTP 429, but the same "back off and retry" treatment
# applies. Confirmed in Kraken's API docs as the exact string returned.
RETRYABLE_KRAKEN_ERROR_PREFIXES = ("EAPI:Rate limit", "EService:", "EGeneral:Temporary")


class KrakenAPIError(RuntimeError):
    """Raised for anything in a Kraken response's "error" array that survives
    retrying (or wasn't retryable in the first place) -- callers should treat
    this the same as any other definitive fetch failure, not parse it
    further."""


def _load_dotenv_if_present() -> None:
    """Tiny, dependency-free .env loader (this project has no python-dotenv
    dependency anywhere else, and adding one just for two variables isn't
    worth breaking the stdlib-only convention). Only sets a variable if it
    isn't already in the environment, so an explicitly-exported shell var
    always wins over the file. Silently does nothing if .env doesn't exist
    or a line doesn't parse -- this must never be the reason a public-only
    call (discovery, backtesting, paper trading) fails."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()
    except OSError:
        pass


_load_dotenv_if_present()


def _api_key() -> str:
    key = os.environ.get("KRAKEN_API_KEY", "")
    if not key:
        raise KrakenAPIError("KRAKEN_API_KEY is not set -- see .env.example / docs/KRAKEN_SETUP.md")
    return key


def _api_secret() -> str:
    secret = os.environ.get("KRAKEN_API_SECRET", "")
    if not secret:
        raise KrakenAPIError("KRAKEN_API_SECRET is not set -- see .env.example / docs/KRAKEN_SETUP.md")
    return secret


def _sign(urlpath: str, data: dict, secret: str) -> str:
    """Kraken's published HMAC-SHA512 request-signing scheme, verbatim:
    https://docs.kraken.com/rest/#section/Authentication"""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _check_errors(payload: dict, attempt_context: str) -> None:
    errors = payload.get("error") or []
    if errors:
        raise KrakenAPIError(f"Kraken API error ({attempt_context}): {errors}")


def _is_retryable_kraken_error(errors: list) -> bool:
    return any(str(e).startswith(RETRYABLE_KRAKEN_ERROR_PREFIXES) for e in errors)


def _request(method: str, path: str, data: dict | None = None, private: bool = False,
             retries: int = 3, backoff: float = 2.0) -> dict:
    data = dict(data or {})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if private:
                data["nonce"] = str(int(time.time() * 1000))
                headers = {
                    "API-Key": _api_key(),
                    "API-Sign": _sign(path, data, _api_secret()),
                    "User-Agent": USER_AGENT,
                }
                req = urllib.request.Request(
                    BASE_URL + path, urllib.parse.urlencode(data).encode(), headers, method="POST",
                )
            else:
                url = BASE_URL + path
                if data:
                    url += "?" + urllib.parse.urlencode(data)
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            errors = payload.get("error") or []
            if errors and _is_retryable_kraken_error(errors) and attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                last_err = KrakenAPIError(f"Kraken API error ({method} {path}): {errors}")
                continue
            _check_errors(payload, f"{method} {path}")
            return payload.get("result", {})
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                last_err = e
                continue
            raise
        except OSError as e:
            # Broad on purpose, same reasoning as research/discover_candidates.py's
            # _get_json: a raw connection-level failure (reset, timeout, DNS
            # hiccup) must be retried the same as a definitive HTTP error, not
            # left to crash the whole discovery/pricing run on one hiccup.
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    assert last_err is not None
    raise last_err


# ---- Public endpoints (no credentials needed) --------------------------------

def asset_pairs() -> dict:
    """{pair_name: {altname, wsname, base, quote, status, pair_decimals, ...}}
    for every pair Kraken lists (tradable or not -- callers filter on
    status/quote themselves, see research/discover_candidates.py)."""
    return _request("GET", "/0/public/AssetPairs")


def assets() -> dict:
    """{asset_code: {altname, decimals, ...}} -- used to map Kraken's own
    asset codes (e.g. "XXBT") to their common altname (e.g. "XBT") for the
    CoinGecko-id lookup in research/discover_candidates.py's classify_tier()."""
    return _request("GET", "/0/public/Assets")


CHUNK_SIZE = 20  # keep each request URL a safe length; Kraken accepts a comma-separated pair list


def ticker(pairs: list[str]) -> dict:
    """{pair_name: {a, b, c, v, p, t, l, h, o}} (see module docstring) for as
    many pairs as fit in CHUNK_SIZE-sized batches -- mirrors
    paper_trading/run_paper_cycle.py's old Jupiter-batching approach for the
    same reason (avoid one request per pair)."""
    result: dict = {}
    unique_pairs = list(dict.fromkeys(pairs))  # de-dupe, keep order
    for i in range(0, len(unique_pairs), CHUNK_SIZE):
        chunk = unique_pairs[i:i + CHUNK_SIZE]
        result.update(_request("GET", "/0/public/Ticker", {"pair": ",".join(chunk)}))
    return result


# Kraken's supported OHLC interval values, in minutes -- 1440 = daily candles,
# which is what every daily-scale indicator in this repo (SMA10/30, RSI14,
# the regime filter's N-day SMA) assumes each series entry represents (see
# backtest/fetch_history.py's _resample_to_daily() for the CoinGecko version
# of this same assumption). Kraken's OHLC endpoint returns already-daily
# candles at this interval -- no resampling needed here.
OHLC_DAILY_INTERVAL_MINUTES = 1440


def ohlc(pair: str, days: int, retries: int = 3, backoff: float = 2.0) -> list[list]:
    """Returns up to `days` days of [timestamp_ms, close] pairs for `pair`,
    oldest first. Kraken's OHLC endpoint only returns its most recent ~720
    intervals regardless of a `since` param requesting more -- callers
    needing more history than that should treat this the same as any other
    "not enough history" case (see backtest/backtest_all.py's n_points < 20
    skip).

    retries/backoff are overridable for the same reason
    backtest/fetch_history.py's _fetch() exposes them: a one-off backtest
    run can afford patience, but paper_trading/run_paper_cycle.py's
    per-candidate signal fetches run inside a tight cron loop where a
    candidate this gives up on quickly just gets reconsidered next cycle."""
    result = _request("GET", "/0/public/OHLC", {"pair": pair, "interval": OHLC_DAILY_INTERVAL_MINUTES},
                       retries=retries, backoff=backoff)
    # Kraken echoes the pair back as a dict key that isn't always exactly the
    # requested string (e.g. requesting "XBTUSD" can come back keyed
    # "XXBTZUSD") -- there's exactly one non-"last" key in the result, so
    # take whichever key that is rather than assuming an exact match.
    candles = next((v for k, v in result.items() if k != "last"), [])
    candles = candles[-days:] if days else candles
    return [[int(c[0]) * 1000, float(c[4])] for c in candles]  # c[4] = close


# ---- Private endpoints (require KRAKEN_API_KEY / KRAKEN_API_SECRET) ----------

def balance() -> dict:
    """{asset_code: "quantity_string"} for every asset with a nonzero
    balance in the account. Requires the API key's "Query Funds"
    permission."""
    return _request("POST", "/0/private/Balance", private=True)


def add_order(pair: str, side: str, ordertype: str, volume: str, price: str | None = None,
              validate: bool = True) -> dict:
    """Places (or, with validate=True, only *validates* -- Kraken's own
    dry-run flag, never actually placed) an order. validate=True is the
    default and the only mode kraken/propose_order.py's default (no
    --execute) path ever calls -- see that script's docstring for why an AI
    agent must never flip this. Requires the API key's "Create & Modify
    Orders" permission."""
    data = {"pair": pair, "type": side, "ordertype": ordertype, "volume": volume, "validate": validate}
    if price is not None:
        data["price"] = price
    return _request("POST", "/0/private/AddOrder", data, private=True)


def trade_volume(pair: str) -> dict:
    """Wraps Kraken's private TradeVolume endpoint: the account's real
    current 30-day-volume fee tier for `pair` (both maker and taker rates),
    not a static assumption. See kraken/fees.py's parse_fee_tier() for
    turning this into a usable FeeTier. Requires "Query Funds"."""
    return _request("POST", "/0/private/TradeVolume", {"pair": pair, "fee-info": True}, private=True)


def open_orders() -> dict:
    return _request("POST", "/0/private/OpenOrders", private=True)


def closed_orders() -> dict:
    return _request("POST", "/0/private/ClosedOrders", private=True)
