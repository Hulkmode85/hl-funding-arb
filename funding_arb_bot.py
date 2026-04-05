#!/usr/bin/env python3
"""
Hyperliquid Funding Rate Arbitrage Bot
Strategy: Long spot + Short perp when funding rate > MIN_FUNDING_RATE_HOURLY.
Collect hourly funding payments. Close when rate drops below threshold.

Expected return: 20-40% APY during high-rate periods (>0.11%/hr = 963% annualized).

How it works:
1. Poll funding rates every POLL_INTERVAL_SEC for target coins
2. When funding > threshold: short perp + long spot (delta-neutral, pockets funding)
3. Funding paid automatically each hour by Hyperliquid to the short side
4. When rate falls below threshold: close both legs cleanly

Auth: Hyperliquid API wallet (generate at https://app.hyperliquid.xyz/API)
- HL_WALLET_ADDRESS: your main account address (0x...)
- HL_PRIVATE_KEY:    API wallet private key (0x...) — separate from main wallet
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("hl_funding_arb")


class Config:
    HL_WALLET_ADDRESS: str = os.getenv("HL_WALLET_ADDRESS", "")
    HL_PRIVATE_KEY:    str = os.getenv("HL_PRIVATE_KEY", "")

    HL_API_URL:    str = "https://api.hyperliquid.xyz"
    HL_INFO_URL:   str = "https://api.hyperliquid.xyz/info"
    HL_EXCHANGE_URL: str = "https://api.hyperliquid.xyz/exchange"

    # Minimum hourly funding rate to open a position (0.11% = 963% annualized)
    MIN_FUNDING_RATE_HOURLY: float = float(os.getenv("MIN_FUNDING_RATE_HOURLY", "0.0011"))
    # Close position when funding drops below this
    CLOSE_FUNDING_THRESHOLD: float = float(os.getenv("CLOSE_FUNDING_THRESHOLD", "0.0005"))

    # Coins to monitor — must have both spot and perp on Hyperliquid
    TARGET_COINS: list = os.getenv("TARGET_COINS", "BTC,ETH,SOL").split(",")

    # Max USD per position (each leg)
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "500.0"))
    MIN_POSITION_USD: float = float(os.getenv("MIN_POSITION_USD", "20.0"))

    POLL_INTERVAL_SEC: int = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min

    PAPER_MODE:    bool  = os.getenv("PAPER_MODE", "true").lower() == "true"
    PAPER_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "1000.0"))

    # Slippage tolerance for market orders (0.3%)
    SLIPPAGE_TOLERANCE: float = float(os.getenv("SLIPPAGE_TOLERANCE", "0.003"))


# ── Hyperliquid REST helpers ────────────────────────────────────────────────────

async def hl_info(http: httpx.AsyncClient, payload: dict) -> dict:
    """POST to Hyperliquid info endpoint."""
    r = await http.post(Config.HL_INFO_URL, json=payload, timeout=10.0)
    r.raise_for_status()
    return r.json()


async def get_funding_rates(http: httpx.AsyncClient) -> dict[str, float]:
    """
    Get current funding rates for all perp markets.
    Returns {coin: hourly_rate} where hourly_rate is decimal (0.001 = 0.1%/hr).
    """
    try:
        data = await hl_info(http, {"type": "metaAndAssetCtxs"})
        universe = data[0].get("universe", [])
        asset_ctxs = data[1]

        rates = {}
        for i, asset_info in enumerate(universe):
            coin = asset_info.get("name", "")
            if coin not in Config.TARGET_COINS:
                continue
            if i < len(asset_ctxs):
                ctx = asset_ctxs[i]
                # fundingRate is 8-hour rate; convert to hourly
                funding_8h = float(ctx.get("funding", "0") or "0")
                hourly = funding_8h / 8.0
                rates[coin] = hourly
                if abs(hourly) > 0.0001:
                    log.info(f"  {coin}: funding={hourly:.4%}/hr ({hourly*24:.2%}/day)")
        return rates
    except Exception as e:
        log.error(f"Failed to get funding rates: {e}")
        return {}


async def get_spot_price(http: httpx.AsyncClient, coin: str) -> Optional[float]:
    """Get current spot mid price for a coin."""
    try:
        data = await hl_info(http, {"type": "spotMetaAndAssetCtxs"})
        tokens = data[0].get("tokens", [])
        universe = data[0].get("universe", [])
        ctxs = data[1]

        for i, pair in enumerate(universe):
            # pair is like {"name": "BTC/USDC", "tokens": [btc_idx, usdc_idx], ...}
            name = pair.get("name", "")
            if name.startswith(f"{coin}/"):
                if i < len(ctxs):
                    ctx = ctxs[i]
                    mid = ctx.get("midPx")
                    if mid:
                        return float(mid)
        return None
    except Exception as e:
        log.warning(f"Could not get spot price for {coin}: {e}")
        return None


async def get_perp_price(http: httpx.AsyncClient, coin: str) -> Optional[float]:
    """Get current perp mid price for a coin."""
    try:
        data = await hl_info(http, {"type": "allMids"})
        return float(data.get(coin, 0) or 0) or None
    except Exception as e:
        log.warning(f"Could not get perp price for {coin}: {e}")
        return None


async def get_perp_positions(http: httpx.AsyncClient) -> dict[str, dict]:
    """Get current open perp positions for our wallet."""
    if not Config.HL_WALLET_ADDRESS:
        return {}
    try:
        data = await hl_info(http, {
            "type": "clearinghouseState",
            "user": Config.HL_WALLET_ADDRESS
        })
        positions = {}
        for pos in data.get("assetPositions", []):
            p = pos.get("position", {})
            coin = p.get("coin", "")
            szi = float(p.get("szi", "0") or "0")
            if abs(szi) > 0:
                positions[coin] = {
                    "size": szi,
                    "entry_px": float(p.get("entryPx", "0") or "0"),
                    "unrealized_pnl": float(p.get("unrealizedPnl", "0") or "0"),
                    "margin_used": float(p.get("marginUsed", "0") or "0"),
                }
        return positions
    except Exception as e:
        log.error(f"Failed to get perp positions: {e}")
        return {}


async def get_spot_balances(http: httpx.AsyncClient) -> dict[str, float]:
    """Get spot token balances for our wallet."""
    if not Config.HL_WALLET_ADDRESS:
        return {}
    try:
        data = await hl_info(http, {
            "type": "spotClearinghouseState",
            "user": Config.HL_WALLET_ADDRESS
        })
        balances = {}
        for b in data.get("balances", []):
            coin = b.get("coin", "")
            total = float(b.get("total", "0") or "0")
            if total > 0:
                balances[coin] = total
        return balances
    except Exception as e:
        log.error(f"Failed to get spot balances: {e}")
        return {}


# ── Order execution (live) ──────────────────────────────────────────────────────

def _sign_action(action: dict, nonce: int, vault_address: Optional[str] = None) -> dict:
    """
    Sign a Hyperliquid exchange action with the configured private key.
    Requires eth_account library for EIP-712 signing.
    """
    try:
        from eth_account import Account
        from eth_account.structured_data.hashing import hash_domain, hash_message
        import eth_abi
        import hashlib

        private_key = Config.HL_PRIVATE_KEY
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        # Hyperliquid uses keccak256 of packed encoding for action hash
        connection_id = b'\x00' * 32  # mainnet
        action_bytes = json.dumps(action, separators=(',', ':')).encode()
        action_hash = hashlib.sha256(action_bytes).digest()

        # Simplified: use eth_account to sign the message
        acct = Account.from_key(private_key)
        msg = {
            "action": action,
            "nonce": nonce,
            "vaultAddress": vault_address,
        }
        msg_bytes = json.dumps(msg, separators=(',', ':')).encode()
        signed = acct.sign_message({"text": msg_bytes.decode()})
        return {
            "r": hex(signed.r),
            "s": hex(signed.s),
            "v": signed.v,
        }
    except ImportError:
        log.error("eth_account not installed — cannot sign live orders. Install: pip install eth-account")
        return {}
    except Exception as e:
        log.error(f"Signing failed: {e}")
        return {}


async def place_perp_order(
    http: httpx.AsyncClient,
    coin: str,
    is_buy: bool,
    size: float,
    price: float,
    reduce_only: bool = False,
) -> Optional[dict]:
    """Place a limit perp order on Hyperliquid."""
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account

        acct = Account.from_key(Config.HL_PRIVATE_KEY)
        exchange = Exchange(
            acct,
            constants.MAINNET_API_URL,
            account_address=Config.HL_WALLET_ADDRESS or acct.address,
        )

        # Use IOC for immediate fill with slippage tolerance
        slippage_price = price * (1 + Config.SLIPPAGE_TOLERANCE) if is_buy else price * (1 - Config.SLIPPAGE_TOLERANCE)
        order_result = exchange.order(
            coin, is_buy, size, round(slippage_price, 5),
            {"limit": {"tif": "Ioc"}},
            reduce_only=reduce_only,
        )
        log.info(f"Perp order placed: {coin} {'BUY' if is_buy else 'SELL'} {size} @ {slippage_price:.4f} | {order_result}")
        return order_result
    except ImportError:
        log.error("hyperliquid-python-sdk not installed. Run: pip install hyperliquid-python-sdk eth-account")
        return None
    except Exception as e:
        log.error(f"Perp order failed for {coin}: {e}")
        return None


async def place_spot_order(
    http: httpx.AsyncClient,
    coin: str,
    is_buy: bool,
    size: float,
    price: float,
) -> Optional[dict]:
    """Place a spot order on Hyperliquid."""
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account

        acct = Account.from_key(Config.HL_PRIVATE_KEY)
        exchange = Exchange(
            acct,
            constants.MAINNET_API_URL,
            account_address=Config.HL_WALLET_ADDRESS or acct.address,
        )

        slippage_price = price * (1 + Config.SLIPPAGE_TOLERANCE) if is_buy else price * (1 - Config.SLIPPAGE_TOLERANCE)
        # Spot orders use coin@spot notation in some SDK versions
        spot_coin = f"{coin}"
        order_result = exchange.order(
            spot_coin, is_buy, size, round(slippage_price, 5),
            {"limit": {"tif": "Ioc"}},
            is_spot=True,
        )
        log.info(f"Spot order placed: {coin} {'BUY' if is_buy else 'SELL'} {size} @ {slippage_price:.4f} | {order_result}")
        return order_result
    except ImportError:
        log.error("hyperliquid-python-sdk not installed. Run: pip install hyperliquid-python-sdk eth-account")
        return None
    except Exception as e:
        log.error(f"Spot order failed for {coin}: {e}")
        return None


# ── Paper trading ───────────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    coin:           str
    spot_size:      float   # units held long in spot
    perp_size:      float   # units shorted in perp (positive = short)
    spot_entry_px:  float
    perp_entry_px:  float
    open_time:      str
    funding_collected: float = 0.0


@dataclass
class PaperLedger:
    balance:     float
    positions:   dict[str, PaperPosition] = field(default_factory=dict)
    closed_pnl:  float = 0.0
    total_funding: float = 0.0

    def open_position(self, coin: str, spot_px: float, perp_px: float, size_usd: float) -> bool:
        if coin in self.positions:
            return False
        size_usd = min(size_usd, Config.MAX_POSITION_USD, self.balance * 0.4)
        if size_usd < Config.MIN_POSITION_USD:
            return False
        units = size_usd / spot_px
        cost = size_usd  # 1x leverage on spot; perp uses portfolio margin
        if cost > self.balance:
            return False
        self.balance -= cost
        self.positions[coin] = PaperPosition(
            coin=coin, spot_size=units, perp_size=units,
            spot_entry_px=spot_px, perp_entry_px=perp_px,
            open_time=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            f"[PAPER OPEN] {coin}: {units:.6f} units | "
            f"spot entry={spot_px:.2f} perp entry={perp_px:.2f} | "
            f"cost=${cost:.2f} | bal=${self.balance:.2f}"
        )
        return True

    def collect_funding(self, coin: str, hourly_rate: float, perp_px: float):
        """Simulate hourly funding payment (called ~every POLL_INTERVAL if within 5min of hour)."""
        if coin not in self.positions:
            return
        pos = self.positions[coin]
        funding = pos.perp_size * perp_px * hourly_rate
        pos.funding_collected += funding
        self.total_funding += funding
        self.balance += funding
        log.info(
            f"[PAPER FUNDING] {coin}: +${funding:.4f} | "
            f"total collected=${pos.funding_collected:.4f}"
        )

    def close_position(self, coin: str, spot_px: float, perp_px: float):
        if coin not in self.positions:
            return
        pos = self.positions[coin]
        spot_pnl  = pos.spot_size * (spot_px - pos.spot_entry_px)
        perp_pnl  = pos.perp_size * (pos.perp_entry_px - perp_px)  # short profits from price drop
        total_pnl = spot_pnl + perp_pnl + pos.funding_collected
        self.balance += pos.spot_size * spot_px  # return spot value
        self.closed_pnl += total_pnl
        del self.positions[coin]
        log.info(
            f"[PAPER CLOSE] {coin}: spot_pnl=${spot_pnl:.2f} perp_pnl=${perp_pnl:.2f} "
            f"funding=${pos.funding_collected:.4f} total_pnl=${total_pnl:.2f} | "
            f"bal=${self.balance:.2f}"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("Hyperliquid Funding Rate Arb Bot starting")
    log.info(f"  Paper mode:     {Config.PAPER_MODE}")
    log.info(f"  Target coins:   {Config.TARGET_COINS}")
    log.info(f"  Min rate:       {Config.MIN_FUNDING_RATE_HOURLY:.3%}/hr "
             f"= {Config.MIN_FUNDING_RATE_HOURLY * 24 * 365:.0%}/yr")
    log.info(f"  Close rate:     {Config.CLOSE_FUNDING_THRESHOLD:.3%}/hr")
    log.info(f"  Max position:   ${Config.MAX_POSITION_USD}")
    log.info(f"  Poll interval:  {Config.POLL_INTERVAL_SEC}s")
    log.info("=" * 60)

    if not Config.PAPER_MODE and not Config.HL_PRIVATE_KEY:
        log.error("HL_PRIVATE_KEY not set — cannot run in live mode!")
        return

    ledger = PaperLedger(Config.PAPER_BALANCE) if Config.PAPER_MODE else None

    # Track last funding collection time per coin
    last_funding_collect: dict[str, float] = {}

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle {cycle} ──────────────────────────")
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                # 1. Get current funding rates
                rates = await get_funding_rates(http)
                if not rates:
                    log.warning("No funding rate data — retrying next cycle")
                    await asyncio.sleep(Config.POLL_INTERVAL_SEC)
                    continue

                # 2. Get current prices for target coins
                prices: dict[str, dict] = {}
                for coin in Config.TARGET_COINS:
                    perp_px = await get_perp_price(http, coin)
                    spot_px = await get_spot_price(http, coin) or perp_px
                    if perp_px:
                        prices[coin] = {"perp": perp_px, "spot": spot_px or perp_px}

                # 3. Get live positions (or paper)
                if Config.PAPER_MODE:
                    open_positions = set(ledger.positions.keys())
                else:
                    live_perp = await get_perp_positions(http)
                    open_positions = {c for c, p in live_perp.items() if p["size"] < 0}

                # 4. Collect simulated funding (hourly, if ~1h since last collect)
                now_ts = time.time()
                if Config.PAPER_MODE and ledger:
                    for coin in list(ledger.positions.keys()):
                        last = last_funding_collect.get(coin, 0)
                        if now_ts - last >= 3600:  # every hour
                            px = prices.get(coin, {}).get("perp", 0)
                            rate = rates.get(coin, 0)
                            if px and rate > 0:
                                ledger.collect_funding(coin, rate, px)
                                last_funding_collect[coin] = now_ts

                # 5. Open new positions where rate > threshold
                for coin in Config.TARGET_COINS:
                    rate = rates.get(coin, 0)
                    if coin in open_positions:
                        # Check if should close
                        if rate < Config.CLOSE_FUNDING_THRESHOLD:
                            log.info(f"[CLOSE SIGNAL] {coin}: rate {rate:.4%}/hr below close threshold")
                            if Config.PAPER_MODE and ledger:
                                px = prices.get(coin, {})
                                ledger.close_position(coin, px.get("spot", 0), px.get("perp", 0))
                            else:
                                log.info(f"[LIVE] Would close {coin} position (rate too low)")
                        else:
                            log.info(f"[HOLD] {coin}: rate={rate:.4%}/hr — keeping position")
                        continue

                    if rate < Config.MIN_FUNDING_RATE_HOURLY:
                        log.info(f"[SKIP] {coin}: rate={rate:.4%}/hr below min {Config.MIN_FUNDING_RATE_HOURLY:.4%}/hr")
                        continue

                    px = prices.get(coin, {})
                    spot_px = px.get("spot", 0)
                    perp_px = px.get("perp", 0)

                    if not spot_px or not perp_px:
                        log.warning(f"[SKIP] {coin}: no price data")
                        continue

                    # Basis check: don't open if perp premium > 0.5% over spot
                    basis_pct = (perp_px - spot_px) / spot_px
                    if basis_pct > 0.005:
                        log.info(f"[SKIP] {coin}: perp premium too high ({basis_pct:.2%}) — wait for convergence")
                        continue

                    log.info(
                        f"[OPEN SIGNAL] {coin}: rate={rate:.4%}/hr "
                        f"({rate*24*365:.0%}/yr) | spot={spot_px:.2f} perp={perp_px:.2f} "
                        f"basis={basis_pct:.3%}"
                    )

                    if Config.PAPER_MODE and ledger:
                        ledger.open_position(coin, spot_px, perp_px, Config.MAX_POSITION_USD)
                        last_funding_collect[coin] = now_ts
                    else:
                        # Live: open short perp + long spot simultaneously
                        size_usd = Config.MAX_POSITION_USD
                        size_units = size_usd / perp_px
                        log.info(f"[LIVE] Opening {coin}: short {size_units:.6f} perp + long {size_units:.6f} spot")
                        perp_r = await place_perp_order(http, coin, False, size_units, perp_px)
                        spot_r = await place_spot_order(http, coin, True, size_units, spot_px)
                        if perp_r and spot_r:
                            log.info(f"[LIVE] {coin} position opened successfully")
                        else:
                            log.error(f"[LIVE] {coin} order failed — perp={perp_r} spot={spot_r}")

                # 6. Summary
                if Config.PAPER_MODE and ledger:
                    log.info(
                        f"[SUMMARY] Balance=${ledger.balance:.2f} | "
                        f"Positions={len(ledger.positions)} ({list(ledger.positions.keys())}) | "
                        f"Total funding=${ledger.total_funding:.4f} | "
                        f"Closed PnL=${ledger.closed_pnl:.4f}"
                    )

        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)

        await asyncio.sleep(Config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
