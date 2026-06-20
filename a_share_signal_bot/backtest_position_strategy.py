#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股持仓盘中管理策略回测器（daily-proxy版）。

目的：验证 position_monitor.py 里的“持仓交易建议”卖出逻辑，而不是验证买入信号本身。
默认用 v6.x 的买入信号产生开仓，之后用持仓监控逻辑管理卖出；也可用 --entries portfolio
只回测一个已有 portfolio.csv 的持仓管理效果。

重要说明：
- 免费历史分钟K很不稳定、覆盖也有限，所以本脚本默认使用日K代理盘中状态。
- 日内弱势、VWAP、从高点回落等用当日 OHLCV 近似，不等于真实10分钟级别回测。
- A股T+1默认开启：T+1买入当天不允许任何卖出，T+2起才管理持仓。
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from . import scanner as bot
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请把本脚本放在 a_share_signal_bot 目录下运行，确保可 import main.py") from exc

try:
    from . import backtest as bt
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先保留/替换好严格T+1版 backtest.py，确保可 import backtest.py") from exc


@dataclass
class MonitorBacktestConfig:
    account: float
    start: str
    end: str
    fetch_start: str
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0
    max_hold_days: int = 60
    max_signal_rows: int = 20000
    a_share_t1: bool = True
    block_limit_up_buys: bool = True
    block_limit_down_sells: bool = True
    limit_tolerance_pct: float = 0.003
    recalc_targets_from_entry: bool = True

    # 持仓监控逻辑参数；默认从 config.position_monitor 读取，这里只是兜底。
    max_loss_pct: float = 0.06
    hard_stop_sell_pct: float = 1.0
    trend_break_reduce_pct: float = 0.50
    profit_protect_1: float = 0.08
    profit_protect_2: float = 0.15
    profit_reduce_pct: float = 0.33
    weak_intraday_reduce_pct: float = 0.33
    min_trade_lot: int = 100
    allow_add: bool = False
    add_max_position_pct: float = 0.22
    add_step_pct: float = 0.25


def ymd_to_ts(s: str) -> pd.Timestamp:
    return bt.ymd_to_ts(s)


def ts_to_ymd(ts: pd.Timestamp) -> str:
    return bt.ts_to_ymd(ts)


def date_back_from_ymd(end_ymd: str, days: int) -> str:
    return bt.date_back_from_ymd(end_ymd, days)


def safe_float(x: Any, default: float = np.nan) -> float:
    return bot.safe_float(x, default)


def round_lot(shares: float, lot: int = 100) -> int:
    if not np.isfinite(shares) or shares <= 0:
        return 0
    return int(math.floor(float(shares) / lot) * lot)


def read_csv_flexible(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, encoding="gbk")


def _col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def read_initial_portfolio(path: str, account_default: float) -> Tuple[float, float, pd.DataFrame]:
    """读取 portfolio.csv。用于 --entries portfolio/both。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"持仓文件不存在: {path}")
    df = read_csv_flexible(p)
    if df.empty:
        return float(account_default), float(account_default), pd.DataFrame(columns=["code", "name", "shares", "cost_price"])
    code_col = _col(df, ["code", "symbol", "股票代码", "证券代码", "代码", "股票"])
    name_col = _col(df, ["name", "股票名称", "证券简称", "名称", "简称"])
    shares_col = _col(df, ["shares", "qty", "quantity", "股数", "股票股数", "持仓股数", "数量"])
    cost_col = _col(df, ["cost_price", "cost", "buy_price", "买入价格", "成本价", "持仓成本", "成本"])
    total_col = _col(df, ["total_equity", "total_funds", "account", "equity", "总资金", "账户总资金", "账户权益", "总资产"])
    cash_col = _col(df, ["cash", "available_cash", "可用现金", "现金", "可用资金"])
    if code_col is None or shares_col is None or cost_col is None:
        raise ValueError("持仓文件至少需要 code/股票代码、shares/持仓股数、cost_price/买入价格")
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        try:
            code = bot.normalize_code(r.get(code_col, ""))
        except Exception:
            continue
        shares = int(safe_float(r.get(shares_col), 0.0))
        cost = safe_float(r.get(cost_col), np.nan)
        if shares <= 0 or not np.isfinite(cost) or cost <= 0:
            continue
        name = str(r.get(name_col, "")).strip() if name_col else ""
        rows.append({"code": code, "name": name, "shares": shares, "cost_price": float(cost)})
    total = float(account_default)
    if total_col is not None:
        vals = pd.to_numeric(df[total_col], errors="coerce").dropna()
        if not vals.empty and float(vals.iloc[0]) > 0:
            total = float(vals.iloc[0])
    cash = np.nan
    if cash_col is not None:
        vals = pd.to_numeric(df[cash_col], errors="coerce").dropna()
        if not vals.empty:
            cash = float(vals.iloc[0])
    return total, cash, pd.DataFrame(rows)


def bt_cfg_from_monitor_cfg(mcfg: MonitorBacktestConfig) -> bt.BacktestConfig:
    return bt.BacktestConfig(
        account=mcfg.account,
        start=mcfg.start,
        end=mcfg.end,
        fetch_start=mcfg.fetch_start,
        commission_rate=mcfg.commission_rate,
        stamp_tax_rate=mcfg.stamp_tax_rate,
        slippage_bps=mcfg.slippage_bps,
        max_hold_days=mcfg.max_hold_days,
        trend_exit=False,  # 本脚本不用 backtest.py 的MA退出，使用持仓监控代理逻辑。
        market_exit=False,
        max_signal_rows=mcfg.max_signal_rows,
        strict_execution=False,  # 持仓监控是盘中/尾盘动作，不走“收盘确认->次日开盘”的趋势退出。
        recalc_targets_from_entry=mcfg.recalc_targets_from_entry,
        block_limit_up_buys=mcfg.block_limit_up_buys,
        block_limit_down_sells=mcfg.block_limit_down_sells,
        limit_tolerance_pct=mcfg.limit_tolerance_pct,
        a_share_t1=mcfg.a_share_t1,
    )


def daily_vwap_proxy(bar: pd.Series) -> float:
    amount = safe_float(bar.get("amount"))
    volume = safe_float(bar.get("volume"))
    close = safe_float(bar.get("close"))
    if np.isfinite(amount) and np.isfinite(volume) and volume > 0:
        # volume 标准化通常为“股”；若接口是手，main.py normalize_stock_hist 已尽量处理。
        vwap = amount / max(volume, 1.0)
        # 如果看起来小100倍，则按“手”换算。
        if np.isfinite(close) and close > 0 and vwap < close * 0.2:
            vwap = amount / max(volume * 100.0, 1.0)
        if np.isfinite(vwap) and vwap > 0:
            return float(vwap)
    o = safe_float(bar.get("open")); h = safe_float(bar.get("high")); l = safe_float(bar.get("low")); c = safe_float(bar.get("close"))
    vals = [x for x in [o, h, l, c] if np.isfinite(x) and x > 0]
    return float(np.mean(vals)) if vals else np.nan


def intraday_weak_proxy(bar: pd.Series, ind_row: pd.Series) -> Tuple[bool, str, Dict[str, float]]:
    """用日K代理 position_monitor 的 VWAP/分钟均线弱势判断。

    因为两年历史分钟K很难稳定获取，默认用当日OHLCV近似：
    - 收盘低于日内VWAP代理 0.3%；
    - 收盘位于日内区间下40%；
    - 从日内高点回落超过2.5%；
    - 阴线且收盘低于MA5/MA10。
    """
    o = safe_float(bar.get("open")); h = safe_float(bar.get("high")); l = safe_float(bar.get("low")); c = safe_float(bar.get("close"))
    vwap = daily_vwap_proxy(bar)
    ma5 = safe_float(ind_row.get("ma5")); ma10 = safe_float(ind_row.get("ma10"))
    pos = (c - l) / max(h - l, 1e-9) if np.isfinite(h) and np.isfinite(l) and h > l and np.isfinite(c) else np.nan
    from_high_dd = c / h - 1.0 if np.isfinite(h) and h > 0 and np.isfinite(c) else np.nan
    flags: List[str] = []
    if np.isfinite(vwap) and c < vwap * 0.997:
        flags.append("收盘低于日内VWAP代理")
    if np.isfinite(pos) and pos <= 0.40:
        flags.append("收盘位于日内区间下40%")
    if np.isfinite(from_high_dd) and from_high_dd <= -0.025:
        flags.append(f"从日内高点回落{from_high_dd:.1%}")
    if np.isfinite(o) and np.isfinite(c) and c < o and ((np.isfinite(ma5) and c < ma5) or (np.isfinite(ma10) and c < ma10)):
        flags.append("阴线且跌破短均线")
    return bool(flags), "；".join(flags) if flags else "日内弱势代理未触发", {"vwap_proxy": vwap, "range_pos": pos, "from_high_dd": from_high_dd}


def monitor_hard_trend_stops(pos: Dict[str, Any], bar: pd.Series, ind_row: pd.Series, mcfg: MonitorBacktestConfig) -> Tuple[float, float]:
    current = safe_float(bar.get("close"))
    cost = safe_float(pos.get("entry_price"), safe_float(pos.get("cost_price")))
    ma10 = safe_float(ind_row.get("ma10")); ma20 = safe_float(ind_row.get("ma20"))
    low10 = safe_float(ind_row.get("low10")); low20 = safe_float(ind_row.get("low20")); atr = safe_float(ind_row.get("atr"))
    hard_candidates = [cost * (1.0 - mcfg.max_loss_pct)]
    if np.isfinite(ma20):
        hard_candidates.append(ma20 * 0.975)
    if np.isfinite(low10):
        hard_candidates.append(low10 * 0.985)
    if np.isfinite(current) and np.isfinite(atr):
        hard_candidates.append(current - 2.2 * atr)
    hard_stop = max([x for x in hard_candidates if np.isfinite(x) and x > 0] or [cost * 0.94])
    trend_candidates: List[float] = []
    if np.isfinite(ma10):
        trend_candidates.append(ma10 * 0.985)
    if np.isfinite(ma20):
        trend_candidates.append(ma20 * 0.985)
    if np.isfinite(low20):
        trend_candidates.append(low20 * 0.985)
    trend_stop = max([x for x in trend_candidates if np.isfinite(x) and x > 0] or [hard_stop])
    return float(hard_stop), float(trend_stop)


def append_blocked_sell(trades: List[Dict[str, Any]], date: pd.Timestamp, pos: Dict[str, Any], price: float, reason: str, cash: float) -> None:
    marker_key = f"_blocked_{pd.Timestamp(date).strftime('%Y%m%d')}_{reason}"
    if pos.get(marker_key):
        return
    pos[marker_key] = True
    trades.append({
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "code": str(pos.get("code", "")).zfill(6),
        "name": pos.get("name", ""),
        "side": "BLOCKED_SELL",
        "shares": int(pos.get("shares", 0)),
        "price": round(float(price), 4) if np.isfinite(price) else np.nan,
        "gross": 0,
        "fee_tax": 0,
        "cash_after": round(cash, 2),
        "reason": reason,
        "entry_date": pos.get("entry_date", ""),
        "entry_price": round(safe_float(pos.get("entry_price")), 4),
        "setup_type": pos.get("setup_type", ""),
        "signal_date": pos.get("signal_date", ""),
    })


def try_execute_monitor_sell(
    positions: Dict[str, Dict[str, Any]],
    code: str,
    date: pd.Timestamp,
    shares: int,
    price: float,
    reason: str,
    cash: float,
    trades: List[Dict[str, Any]],
    raw_bars: Dict[str, pd.DataFrame],
    bt_cfg: bt.BacktestConfig,
) -> float:
    pos = positions.get(code)
    if not pos or shares <= 0:
        return cash
    if not bt.is_sellable_by_a_share_t1(pos, date, bt_cfg):
        append_blocked_sell(trades, date, pos, price, f"A股T+1限制，当日不可卖；原触发={reason}", cash)
        return cash
    bar = bt.get_bar(raw_bars.get(code), date)
    if bar is not None and bt.is_limit_down_locked(raw_bars.get(code), date, bar, code, str(pos.get("name", "")), bt_cfg):
        append_blocked_sell(trades, date, pos, safe_float(bar.get("open"), price), f"{reason}；接近跌停封死，按无法成交", cash)
        return cash
    return bt.execute_sell(positions, code, int(shares), float(price), date, f"持仓监控：{reason}", cash, trades, bt_cfg)


def manage_positions_by_monitor_proxy(
    date: pd.Timestamp,
    positions: Dict[str, Dict[str, Any]],
    raw_bars: Dict[str, pd.DataFrame],
    ind_map: Dict[str, pd.DataFrame],
    market: bot.MarketState,
    cash: float,
    trades: List[Dict[str, Any]],
    bot_cfg: Dict[str, Any],
    mcfg: MonitorBacktestConfig,
    bt_cfg: bt.BacktestConfig,
) -> float:
    for code in list(positions.keys()):
        pos = positions.get(code)
        if not pos:
            continue
        bar = bt.get_bar(raw_bars.get(code), date)
        if bar is None:
            continue
        open_px = safe_float(bar.get("open")); high_px = safe_float(bar.get("high")); low_px = safe_float(bar.get("low")); close_px = safe_float(bar.get("close"))
        if not np.isfinite(close_px) or close_px <= 0:
            continue
        pos["last_price"] = close_px
        pos["highest_close"] = max(safe_float(pos.get("highest_close"), close_px), close_px)

        # A股T+1：买入当天不允许卖，持仓监控也不能越过这个规则。
        if not bt.is_sellable_by_a_share_t1(pos, date, bt_cfg):
            pos["t1_locked_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
            continue

        ind = bt.slice_ind(ind_map.get(code), date)
        if ind.empty:
            continue
        ind_row = ind.iloc[-1]
        hard_stop, trend_stop = monitor_hard_trend_stops(pos, bar, ind_row, mcfg)
        pos["monitor_hard_stop"] = hard_stop
        pos["monitor_trend_stop"] = trend_stop

        shares = int(pos.get("shares", 0))
        entry_price = safe_float(pos.get("entry_price"))
        profit_pct = close_px / entry_price - 1.0 if np.isfinite(entry_price) and entry_price > 0 else np.nan
        weak, weak_reason, weak_detail = intraday_weak_proxy(bar, ind_row)
        from_high_dd = safe_float(weak_detail.get("from_high_dd"))
        ma10 = safe_float(ind_row.get("ma10"))

        # 1) 风险闸门：用截至当日的日K判断，触发则全出。
        try:
            risk_gate = bot.compute_risk_gate(ind, code, str(pos.get("name", "")), bot_cfg)
        except Exception:
            risk_gate = {"risk_gate_block": False, "risk_gate_reason": ""}
        if bool(risk_gate.get("risk_gate_block", False)):
            sell_shares = round_lot(shares * mcfg.hard_stop_sell_pct, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, "A股风险闸门触发：" + str(risk_gate.get("risk_gate_reason", "")), cash, trades, raw_bars, bt_cfg)
            continue

        # 2) 硬止损：盘中 low 碰到 hard_stop，用 hard_stop 或跳空开盘价成交。
        if np.isfinite(low_px) and np.isfinite(hard_stop) and low_px <= hard_stop:
            px = open_px if np.isfinite(open_px) and open_px < hard_stop else hard_stop
            sell_shares = round_lot(shares * mcfg.hard_stop_sell_pct, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, px, f"硬止损触发{hard_stop:.2f}", cash, trades, raw_bars, bt_cfg)
            continue

        # 3) 大盘弱且跌破趋势线：减半，不一定全出，贴近 position_monitor.py。
        if market.target_exposure <= 0 and np.isfinite(trend_stop) and close_px < trend_stop:
            sell_shares = round_lot(shares * 0.50, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, f"大盘弱势且跌破趋势防守线{trend_stop:.2f}", cash, trades, raw_bars, bt_cfg)
            continue

        # 4) 趋势破位 + 日内弱势代理：减半。
        if np.isfinite(trend_stop) and close_px < trend_stop and weak:
            sell_shares = round_lot(shares * mcfg.trend_break_reduce_pct, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, f"趋势破位减仓：跌破{trend_stop:.2f}且{weak_reason}", cash, trades, raw_bars, bt_cfg)
            continue

        # 5) 盈利保护：不是R倍止盈，而是浮盈达到阈值后盘中转弱锁利润。
        if np.isfinite(profit_pct) and profit_pct >= mcfg.profit_protect_2 and (weak or (np.isfinite(from_high_dd) and from_high_dd <= -0.025)):
            sell_shares = round_lot(shares * 0.50, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, f"盈利保护止盈：浮盈{profit_pct:.1%}，{weak_reason}", cash, trades, raw_bars, bt_cfg)
            continue
        if np.isfinite(profit_pct) and profit_pct >= mcfg.profit_protect_1 and weak:
            sell_shares = round_lot(shares * mcfg.profit_reduce_pct, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, f"部分止盈：浮盈{profit_pct:.1%}但{weak_reason}", cash, trades, raw_bars, bt_cfg)
            continue

        # 6) 亏损且盘中弱势：减仓，避免亏损扩大。
        if np.isfinite(profit_pct) and profit_pct < 0 and weak and np.isfinite(ma10) and close_px < ma10:
            sell_shares = round_lot(shares * mcfg.weak_intraday_reduce_pct, mcfg.min_trade_lot)
            cash = try_execute_monitor_sell(positions, code, date, sell_shares, close_px, f"亏损弱势减仓：{weak_reason}且跌破MA10", cash, trades, raw_bars, bt_cfg)
            continue

        # 7) 最长持仓天数：这是回测安全阀；用收盘价退出。若不需要可设为0。
        if mcfg.max_hold_days > 0:
            entry_ts = pd.Timestamp(pos.get("entry_date"))
            hold_days = max(0, (pd.Timestamp(date) - entry_ts).days)
            if hold_days >= mcfg.max_hold_days:
                cash = try_execute_monitor_sell(positions, code, date, shares, close_px, f"持仓超过{mcfg.max_hold_days}天", cash, trades, raw_bars, bt_cfg)
                continue

        # 8) 可选加仓：默认关闭。开启后模拟 position_monitor 的小步顺势加仓。
        if mcfg.allow_add:
            total_equity_now = cash + bt.value_positions(positions, raw_bars, date)
            market_value = close_px * shares
            weight = market_value / total_equity_now if total_equity_now > 0 else np.nan
            ma5 = safe_float(ind_row.get("ma5")); ma10 = safe_float(ind_row.get("ma10")); ma20 = safe_float(ind_row.get("ma20"))
            vwap_proxy = safe_float(weak_detail.get("vwap_proxy"))
            trend_ok = np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and close_px >= ma5 >= ma10 >= ma20
            intraday_strong = (not np.isfinite(vwap_proxy)) or close_px >= vwap_proxy * 1.002
            pct_chg = safe_float(bar.get("pct_chg"), np.nan)
            if not np.isfinite(pct_chg):
                hist_all = raw_bars.get(code)
                if hist_all is not None and not hist_all.empty:
                    hist_prev = hist_all[hist_all["date"] < pd.Timestamp(date)].tail(1)
                    prev_close = safe_float(hist_prev["close"].iloc[0]) if not hist_prev.empty else np.nan
                    if np.isfinite(prev_close) and prev_close > 0:
                        pct_chg = close_px / prev_close - 1.0
            not_chasing = not np.isfinite(pct_chg) or pct_chg <= 0.048
            if market.target_exposure > 0 and trend_ok and intraday_strong and not_chasing and (not np.isfinite(weight) or weight < mcfg.add_max_position_pct * 0.75):
                target_value = min(total_equity_now * mcfg.add_max_position_pct, market_value + total_equity_now * mcfg.add_step_pct * mcfg.add_max_position_pct)
                buy_cash = max(0.0, min(target_value - market_value, cash * 0.98))
                buy_px = bt.apply_slippage(close_px, "buy", bt_cfg)
                buy_shares = round_lot(buy_cash / max(buy_px, 1e-9), mcfg.min_trade_lot)
                if buy_shares >= mcfg.min_trade_lot and cash > buy_shares * buy_px * (1 + bt_cfg.commission_rate):
                    gross = buy_shares * buy_px
                    fee = bt.cost_buy(gross, bt_cfg)
                    cash -= gross + fee
                    # 加仓后平均成本重算；entry_date 不改，避免用加仓逃避持仓天数和T+1。真实加仓新买部分T+1无法卖，这里保守地让整仓沿用原入场日。
                    old_shares = int(pos.get("shares", 0))
                    old_cost = safe_float(pos.get("entry_price"))
                    new_shares = old_shares + buy_shares
                    avg_cost = (old_cost * old_shares + buy_px * buy_shares) / max(new_shares, 1)
                    pos["shares"] = new_shares
                    pos["entry_price"] = avg_cost
                    pos["entry_fee_per_share"] = (safe_float(pos.get("entry_fee_per_share"), 0.0) * old_shares + fee) / max(new_shares, 1)
                    pos["last_price"] = close_px
                    trades.append({
                        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                        "code": code,
                        "name": pos.get("name", ""),
                        "side": "ADD",
                        "shares": buy_shares,
                        "price": round(buy_px, 4),
                        "gross": round(gross, 2),
                        "fee_tax": round(fee, 2),
                        "cash_after": round(cash, 2),
                        "reason": "持仓监控：趋势强、日内强于VWAP代理且仓位未到上限，小步加仓",
                        "entry_date": pos.get("entry_date", ""),
                        "setup_type": pos.get("setup_type", ""),
                        "signal_date": pos.get("signal_date", ""),
                    })
    return cash


def prepare_data(args: argparse.Namespace, cfg: Dict[str, Any], out_dir: Path) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], List[pd.Timestamp], bot.AkshareFetcher]:
    fetcher = bot.AkshareFetcher(cfg, refresh=bool(args.refresh))
    pool = bot.enrich_pool_sectors(bot.read_stock_pool(args.pool), args.pool, cfg, fetcher=fetcher)
    if args.limit and args.limit > 0:
        pool = pool.head(int(args.limit)).copy()
    if pool["name"].fillna("").astype(str).str.strip().eq("").any():
        try:
            name_map = bot.fetch_name_map(fetcher)
        except Exception:
            name_map = {}
        pool["name"] = pool.apply(lambda r: r["name"] if str(r["name"]).strip() else name_map.get(r["code"], ""), axis=1)

    index_inds: Dict[str, pd.DataFrame] = {}
    index_symbols = cfg["data"].get("market_indices") or ["sh000001"]
    if isinstance(index_symbols, str):
        index_symbols = [x.strip() for x in index_symbols.split(",") if x.strip()]
    for sym in index_symbols:
        try:
            raw = fetcher.index_hist(sym)
            ind = bot.add_indicators(raw, atr_period=int(cfg["strategy"].get("atr_period", 14)))
            index_inds[sym] = ind
            print(f"[持仓策略回测] 指数 {sym} K线 {len(ind)} 行")
        except Exception as exc:
            print(f"[警告] 指数 {sym} 获取失败：{exc}")
    if not index_inds:
        raise RuntimeError("大盘指数数据获取失败，无法回测")

    raw_bars: Dict[str, pd.DataFrame] = {}
    ind_map: Dict[str, pd.DataFrame] = {}
    errors: List[Dict[str, str]] = []
    for n, (_, r) in enumerate(pool.iterrows(), start=1):
        code = str(r.get("code", "")).zfill(6)
        try:
            raw = fetcher.stock_hist(code, args.fetch_start, args.end, cfg["data"].get("adjust", "qfq"))
            ind = bot.add_indicators(raw, atr_period=int(cfg["strategy"].get("atr_period", 14)))
            raw_bars[code] = raw
            ind_map[code] = ind
        except Exception as exc:
            errors.append({"code": code, "name": str(r.get("name", "")), "error": str(exc)})
        if n % 20 == 0 or n == len(pool):
            print(f"[持仓策略回测] 已预取个股 {n}/{len(pool)}")
    if errors:
        pd.DataFrame(errors).to_csv(out_dir / "latest_position_strategy_data_errors.csv", index=False, encoding="utf-8-sig")
        print(f"[警告] 个股数据失败 {len(errors)} 只，已写 latest_position_strategy_data_errors.csv")
    if not ind_map:
        raise RuntimeError("个股 K 线全部获取失败，无法回测")

    dates = bt.first_index_dates(index_inds, args.start, args.end)
    if len(dates) < 30:
        raise RuntimeError("回测交易日不足，检查 start/end 或指数数据")
    return pool, index_inds, raw_bars, ind_map, dates, fetcher


def seed_initial_portfolio(
    portfolio_path: str,
    positions: Dict[str, Dict[str, Any]],
    raw_bars: Dict[str, pd.DataFrame],
    start_date: pd.Timestamp,
    account: float,
    cash_override: float,
    trades: List[Dict[str, Any]],
    bt_cfg: bt.BacktestConfig,
) -> float:
    total, cash_file, pf = read_initial_portfolio(portfolio_path, account)
    cash = cash_file if np.isfinite(cash_file) else account
    invested_cost = 0.0
    for _, r in pf.iterrows():
        code = str(r.get("code", "")).zfill(6)
        shares = int(r.get("shares", 0))
        cost_px = safe_float(r.get("cost_price"))
        if shares <= 0 or not np.isfinite(cost_px) or cost_px <= 0:
            continue
        bar = bt.get_bar(raw_bars.get(code), start_date)
        last_price = safe_float(bar.get("close"), cost_px) if bar is not None else cost_px
        positions[code] = {
            "code": code,
            "name": str(r.get("name", "")),
            "sector": "",
            "shares": shares,
            # 视为回测开始前已经买入，第一天即可管理；避免把旧持仓也锁T+1。
            "entry_date": (pd.Timestamp(start_date) - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
            "signal_date": "initial_portfolio",
            "entry_price": cost_px,
            "entry_fee_per_share": 0.0,
            "stop_loss": cost_px * 0.94,
            "initial_stop": cost_px * 0.94,
            "take_profit_1": cost_px * 1.10,
            "take_profit_2": cost_px * 1.20,
            "tp1_done": False,
            "highest_close": last_price,
            "last_price": last_price,
            "setup_type": "initial_portfolio",
            "score": np.nan,
            "reason": "初始持仓导入",
        }
        invested_cost += cost_px * shares
        trades.append({
            "date": pd.Timestamp(start_date).strftime("%Y-%m-%d"),
            "code": code,
            "name": str(r.get("name", "")),
            "side": "SEED",
            "shares": shares,
            "price": round(cost_px, 4),
            "gross": round(cost_px * shares, 2),
            "fee_tax": 0,
            "cash_after": round(cash, 2) if np.isfinite(cash) else np.nan,
            "reason": "初始持仓导入，不作为新买入成交",
        })
    if not np.isfinite(cash_file):
        cash = max(0.0, account - invested_cost)
    if np.isfinite(cash_override):
        cash = float(cash_override)
    return cash


def compute_summary(equity_df: pd.DataFrame, trades_df: pd.DataFrame, initial_account: float, benchmark_return: float = np.nan) -> Dict[str, Any]:
    return bt.compute_summary(equity_df, trades_df, initial_account, benchmark_return)


def fmt_pct(x: Any) -> str:
    return bt.fmt_pct(x)


def fmt_num(x: Any, digits: int = 2) -> str:
    return bt.fmt_num(x, digits)


def write_report(out_dir: Path, summary: Dict[str, Any], equity_df: pd.DataFrame, trades_df: pd.DataFrame, signal_rows: pd.DataFrame, positions: Dict[str, Dict[str, Any]], args: argparse.Namespace) -> Path:
    lines: List[str] = []
    lines.append("# A股持仓盘中管理策略回测报告\n")
    lines.append(f"- 初始资金：{float(args.account):,.2f}")
    lines.append(f"- 回测区间：{args.start} ~ {args.end}")
    lines.append(f"- 开仓来源：{args.entries}")
    lines.append("- 买入配合：若 entries=signal/both，则仍使用 v6.x 买入信号，T日收盘出信号，T+1开盘买入。")
    lines.append("- 持仓卖出：使用 position_monitor.py 的日线代理版卖出逻辑：硬止损、趋势防守+日内弱势代理、盈利保护、亏损弱势减仓、大盘弱势减仓。")
    if not args.allow_sell_on_buy_day:
        lines.append("- A股T+1：T+1买入当天不允许任何卖出/止盈/止损，T+2起才管理持仓。")
    lines.append("- 日内数据说明：本脚本默认不拉历史分钟K，使用日K OHLCV 代理 VWAP/日内弱势；这用于验证规则方向，不等价于真实10分钟级别逐笔回测。")
    lines.append(f"- 手续费参数：买卖佣金 {float(args.commission_rate):.4%}；卖出印花税 {float(args.stamp_tax_rate):.4%}；单边滑点 {float(args.slippage_bps):.1f} bps。")
    if args.allow_add:
        lines.append("- 加仓：已开启，按持仓监控顺势加仓规则模拟；默认建议先关闭加仓单独验证卖出策略。")
    else:
        lines.append("- 加仓：关闭，只验证持仓卖出/减仓策略。")
    lines.append("")
    lines.append("## 结果概览")
    lines.append(f"- 期末权益：{fmt_num(summary.get('end_equity'))}")
    lines.append(f"- 总收益率：{fmt_pct(summary.get('total_return'))}")
    lines.append(f"- 年化收益率：{fmt_pct(summary.get('cagr'))}")
    lines.append(f"- 最大回撤：{fmt_pct(summary.get('max_drawdown'))}")
    lines.append(f"- Sharpe：{fmt_num(summary.get('sharpe'))}")
    lines.append(f"- 卖出成交记录数：{int(summary.get('trade_count', 0))}")
    lines.append(f"- 胜率：{fmt_pct(summary.get('win_rate'))}")
    lines.append(f"- 盈亏比 Profit Factor：{fmt_num(summary.get('profit_factor'))}")
    lines.append(f"- 平均仓位：{fmt_pct(summary.get('avg_exposure'))}")
    if np.isfinite(safe_float(summary.get("benchmark_return"))):
        lines.append(f"- 基准收益：{fmt_pct(summary.get('benchmark_return'))}")
        lines.append(f"- 超额收益：{fmt_pct(summary.get('excess_return'))}")
    lines.append("")
    if not trades_df.empty:
        sells = trades_df[trades_df["side"].astype(str).eq("SELL")].copy() if "side" in trades_df.columns else pd.DataFrame()
        lines.append("## 退出/交易原因统计")
        if not sells.empty:
            counts = sells["reason"].astype(str).value_counts().head(15)
            for reason, cnt in counts.items():
                lines.append(f"- {reason}：{cnt} 次")
        else:
            lines.append("- 无卖出记录")
        lines.append("")
        lines.append("## 最近 20 笔成交")
        for _, r in trades_df.tail(20).iterrows():
            pnl = r.get("pnl", "")
            pnl_text = "" if pd.isna(pnl) or pnl == "" else f"，PnL={fmt_num(pnl)}"
            lines.append(f"- {r.get('date','')} {r.get('side','')} {r.get('code','')} {r.get('name','')} {r.get('shares','')}股 @ {r.get('price','')}{pnl_text}，{r.get('reason','')}")
        lines.append("")
    lines.append("## 期末持仓")
    if not positions:
        lines.append("- 无")
    else:
        for code, pos in positions.items():
            lines.append(f"- {code} {pos.get('name','')}：{pos.get('shares',0)}股，成本 {fmt_num(pos.get('entry_price'),4)}，监控硬止损 {fmt_num(pos.get('monitor_hard_stop'),4)}，趋势防守 {fmt_num(pos.get('monitor_trend_stop'),4)}，买点 {pos.get('setup_type','')}")
    lines.append("")
    lines.append("## 输出文件")
    lines.append("- latest_position_strategy_equity.csv：每日权益曲线")
    lines.append("- latest_position_strategy_trades.csv：成交明细")
    lines.append("- latest_position_strategy_signals.csv：历史买入信号记录")
    lines.append("- latest_position_strategy_open_positions.csv：期末持仓")
    lines.append("- latest_position_strategy_summary.csv：概要指标")
    path = out_dir / "latest_position_strategy_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "latest_position_strategy_message.txt").write_text("\n".join(lines[:40]), encoding="utf-8")
    return path


def run_backtest(args: argparse.Namespace) -> int:
    cfg = bot.load_config(args.config)
    cfg["data"]["use_realtime_tail"] = False
    end = args.end or bot.today_yyyymmdd()
    start = args.start or date_back_from_ymd(end, int(float(args.years) * 365))
    fetch_start = args.fetch_start or date_back_from_ymd(start, int(args.lookback_days))
    args.start = start; args.end = end; args.fetch_start = fetch_start
    cfg["data"]["start_date"] = fetch_start
    cfg["data"]["end_date"] = end
    if args.market_index:
        cfg["data"]["market_indices"] = [x.strip() for x in args.market_index.split(",") if x.strip()]
    if args.no_sector_autofill:
        cfg.setdefault("strategy", {}).setdefault("sector", {})["auto_fill"] = False

    out_dir = bot.ensure_dir(args.out)
    pm = cfg.get("position_monitor", {})
    mcfg = MonitorBacktestConfig(
        account=float(args.account), start=start, end=end, fetch_start=fetch_start,
        commission_rate=float(args.commission_rate), stamp_tax_rate=float(args.stamp_tax_rate), slippage_bps=float(args.slippage_bps),
        max_hold_days=int(args.max_hold_days), max_signal_rows=int(args.max_signal_rows),
        a_share_t1=not bool(args.allow_sell_on_buy_day),
        block_limit_up_buys=not bool(args.allow_limit_up_buy), block_limit_down_sells=not bool(args.allow_limit_down_sell),
        limit_tolerance_pct=float(args.limit_tolerance_pct), recalc_targets_from_entry=not bool(args.no_recalc_targets_from_entry),
        max_loss_pct=float(args.max_loss_pct if args.max_loss_pct is not None else pm.get("max_loss_pct", 0.06)),
        hard_stop_sell_pct=float(pm.get("hard_stop_sell_pct", 1.0)),
        trend_break_reduce_pct=float(pm.get("trend_break_reduce_pct", 0.50)),
        profit_protect_1=float(pm.get("profit_protect_1", 0.08)),
        profit_protect_2=float(pm.get("profit_protect_2", 0.15)),
        profit_reduce_pct=float(pm.get("profit_reduce_pct", 0.33)),
        weak_intraday_reduce_pct=float(pm.get("weak_intraday_reduce_pct", 0.33)),
        min_trade_lot=int(pm.get("min_trade_lot", 100)),
        allow_add=bool(args.allow_add),
        add_max_position_pct=float(pm.get("add_max_position_pct", 0.22)),
        add_step_pct=float(pm.get("add_step_pct", 0.25)),
    )
    bt_cfg = bt_cfg_from_monitor_cfg(mcfg)

    print(f"[持仓策略回测] 区间 {start} ~ {end}，初始资金 {args.account:,.2f}，预取历史从 {fetch_start}，entries={args.entries}")
    pool, index_inds, raw_bars, ind_map, dates, fetcher = prepare_data(args, cfg, out_dir)
    max_positions = int(cfg["strategy"].get("max_positions", 5))

    cash = float(args.account)
    positions: Dict[str, Dict[str, Any]] = {}
    pending_orders: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    equity_rows: List[Dict[str, Any]] = []
    signal_rows_all: List[pd.DataFrame] = []
    peak_equity = cash

    if args.entries in {"portfolio", "both"}:
        cash = seed_initial_portfolio(args.portfolio, positions, raw_bars, dates[0], float(args.account), float(args.initial_cash) if args.initial_cash is not None else np.nan, trades, bt_cfg)
        print(f"[持仓策略回测] 初始持仓 {len(positions)} 只，初始现金 {cash:,.2f}")

    for idx, date in enumerate(dates):
        market = bt.evaluate_market_asof(index_inds, cfg, date)
        # T日开盘执行上一交易日收盘后的买入信号。
        if args.entries in {"signal", "both"}:
            cash, pending_orders = bt.execute_pending_buys(date, pending_orders, positions, raw_bars, cash, trades, bt_cfg, max_positions)

        # 持仓监控策略：T+2起管理；默认同日用收盘价代理盘中/尾盘监控动作。
        cash = manage_positions_by_monitor_proxy(date, positions, raw_bars, ind_map, market, cash, trades, cfg, mcfg, bt_cfg)

        mv = bt.value_positions(positions, raw_bars, date)
        equity = cash + mv
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1.0 if peak_equity > 0 else 0.0
        exposure = mv / equity if equity > 0 else 0.0

        # 收盘后生成下一交易日买入信号。
        signals_count = 0
        if args.entries in {"signal", "both"}:
            candidates = bt.build_daily_candidates(date, pool, ind_map, cfg, market)
            data_error_count = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if not candidates.empty and "filter_reason" in candidates.columns else 0
            data_error_rate = data_error_count / max(1, len(pool))
            if data_error_rate > float(cfg.get("data", {}).get("max_error_rate_for_valid_run", 0.20)):
                if not candidates.empty:
                    candidates["is_signal"] = False
            signals = candidates[candidates.get("is_signal", False) == True].copy() if not candidates.empty else pd.DataFrame()
            allocated = bot.allocate_positions(signals, cfg, market, equity)
            pending_orders = bt.generate_pending_orders(date, allocated, positions, equity, max_positions)
            signals_count = len(signals)
            if not signals.empty:
                sig_keep = signals.copy()
                sig_keep["signal_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
                signal_rows_all.append(sig_keep)
            elif args.keep_all_candidates and len(signal_rows_all) < mcfg.max_signal_rows:
                top = candidates.sort_values("score", ascending=False).head(10).copy() if not candidates.empty else pd.DataFrame()
                if not top.empty:
                    top["signal_date"] = pd.Timestamp(date).strftime("%Y-%m-%d")
                    signal_rows_all.append(top)

        equity_rows.append({
            "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
            "cash": round(cash, 2),
            "market_value": round(mv, 2),
            "total_equity": round(equity, 2),
            "drawdown": drawdown,
            "exposure": exposure,
            "positions": len(positions),
            "market_regime": market.regime,
            "market_score": market.score,
            "target_exposure": market.target_exposure,
            "signals": int(signals_count),
            "pending_buys": int(len(pending_orders)),
        })
        if idx % 50 == 0 or idx == len(dates) - 1:
            print(f"[持仓策略回测] {pd.Timestamp(date).strftime('%Y-%m-%d')} {idx+1}/{len(dates)} 权益={equity:,.2f} 持仓={len(positions)} 信号={signals_count}")

    final_date = dates[-1]
    open_positions_snapshot = pd.DataFrame(list(positions.values())) if positions else pd.DataFrame()
    if args.close_at_end:
        for code in list(positions.keys()):
            bar = bt.get_bar(raw_bars.get(code), final_date)
            if bar is not None:
                cash = bt.execute_sell(positions, code, int(positions[code].get("shares", 0)), safe_float(bar.get("close")), final_date, "期末清算", cash, trades, bt_cfg)
        mv = bt.value_positions(positions, raw_bars, final_date)
        equity = cash + mv
        if equity_rows:
            equity_rows[-1]["cash"] = round(cash, 2)
            equity_rows[-1]["market_value"] = round(mv, 2)
            equity_rows[-1]["total_equity"] = round(equity, 2)
            equity_rows[-1]["positions"] = len(positions)

    equity_df = pd.DataFrame(equity_rows)
    if not equity_df.empty:
        peak = pd.to_numeric(equity_df["total_equity"], errors="coerce").cummax()
        equity_df["drawdown"] = pd.to_numeric(equity_df["total_equity"], errors="coerce") / peak - 1.0
    trades_df = pd.DataFrame(trades)
    signals_df = pd.concat(signal_rows_all, ignore_index=True) if signal_rows_all else pd.DataFrame()

    benchmark_return = np.nan
    bench_sym = args.benchmark or (next(iter(index_inds.keys())) if index_inds else "")
    if bench_sym in index_inds:
        bench = index_inds[bench_sym]
        b = bench[(bench["date"] >= ymd_to_ts(start)) & (bench["date"] <= ymd_to_ts(end))].copy()
        if len(b) >= 2:
            benchmark_return = safe_float(b["close"].iloc[-1] / b["close"].iloc[0] - 1.0)
    summary = compute_summary(equity_df, trades_df, float(args.account), benchmark_return)

    equity_df.to_csv(out_dir / "latest_position_strategy_equity.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(out_dir / "latest_position_strategy_trades.csv", index=False, encoding="utf-8-sig")
    signals_df.to_csv(out_dir / "latest_position_strategy_signals.csv", index=False, encoding="utf-8-sig")
    open_positions_snapshot.to_csv(out_dir / "latest_position_strategy_open_positions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary]).to_csv(out_dir / "latest_position_strategy_summary.csv", index=False, encoding="utf-8-sig")
    open_pos_dict = {str(r.get("code", "")): r for r in open_positions_snapshot.to_dict("records")} if not open_positions_snapshot.empty else {}
    report_path = write_report(out_dir, summary, equity_df, trades_df, signals_df, open_pos_dict, args)
    print("\n[持仓策略回测完成]")
    print(f"期末权益：{fmt_num(summary.get('end_equity'))}")
    print(f"总收益率：{fmt_pct(summary.get('total_return'))}，最大回撤：{fmt_pct(summary.get('max_drawdown'))}，卖出记录：{int(summary.get('trade_count', 0))}")
    print(f"报告：{report_path}")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A股持仓盘中管理策略回测器：用日K代理 position_monitor.py 的卖出/减仓逻辑")
    p.add_argument("--pool", default="stock_pool.csv", help="股票池文件；entries=signal/both 时必需")
    p.add_argument("--portfolio", default="portfolio.csv", help="初始持仓文件；entries=portfolio/both 时使用")
    p.add_argument("--entries", choices=["signal", "portfolio", "both"], default="signal", help="开仓来源：signal=用v6买入信号；portfolio=只管理初始持仓；both=两者都用")
    p.add_argument("--config", default="config.example.yml", help="配置文件 YAML/JSON")
    p.add_argument("--out", default="position_strategy_backtest_output", help="输出目录")
    p.add_argument("--account", type=float, default=200000.0, help="初始资金，默认200000")
    p.add_argument("--initial-cash", type=float, default=None, help="entries=portfolio/both 时强制指定初始现金；默认从portfolio读取或用account-持仓成本")
    p.add_argument("--years", type=float, default=2.0, help="回测最近几年，默认2年；若提供 --start 则忽略")
    p.add_argument("--start", default="", help="回测开始日期 YYYYMMDD")
    p.add_argument("--end", default="", help="回测结束日期 YYYYMMDD，默认今天")
    p.add_argument("--fetch-start", default="", help="指标预热数据开始日期，默认 start 往前 lookback-days")
    p.add_argument("--lookback-days", type=int, default=900, help="指标预热天数，默认900自然日")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉取行情")
    p.add_argument("--limit", type=int, default=0, help="只回测股票池前N只，用于调试")
    p.add_argument("--market-index", default="", help="大盘指数，逗号分隔，例如 sh000001,sz399001,sh000300")
    p.add_argument("--benchmark", default="sh000001", help="基准指数，默认 sh000001")
    p.add_argument("--commission-rate", type=float, default=0.0003, help="单边佣金率，默认0.03%%")
    p.add_argument("--stamp-tax-rate", type=float, default=0.0005, help="卖出印花税率，默认0.05%%")
    p.add_argument("--slippage-bps", type=float, default=5.0, help="单边滑点bps，默认5")
    p.add_argument("--max-hold-days", type=int, default=60, help="最长持仓自然日，默认60；0表示关闭")
    p.add_argument("--max-loss-pct", type=float, default=None, help="硬止损最大成本亏损，例如0.06；默认读取config.position_monitor.max_loss_pct")
    p.add_argument("--allow-add", action="store_true", help="开启持仓监控加仓回测；默认关闭，先单独验证卖出策略")
    p.add_argument("--close-at-end", action="store_true", help="回测结束日按收盘价清算所有持仓")
    p.add_argument("--keep-all-candidates", action="store_true", help="保存每天最高分候选，不只保存信号；文件会更大")
    p.add_argument("--max-signal-rows", type=int, default=20000, help="保存候选最大行数提示参数")
    p.add_argument("--no-sector-autofill", action="store_true", help="关闭 AkShare 自动补全板块")
    p.add_argument("--no-recalc-targets-from-entry", action="store_true", help="不按真实买入价重算止盈位；主要影响买入记录，持仓监控退出不用R止盈")
    p.add_argument("--allow-limit-up-buy", action="store_true", help="允许开盘接近涨停时买入；默认跳过")
    p.add_argument("--allow-limit-down-sell", action="store_true", help="允许接近跌停封死也卖出；默认按卖不出")
    p.add_argument("--limit-tolerance-pct", type=float, default=0.003, help="涨跌停近似容差，默认0.003")
    p.add_argument("--allow-sell-on-buy-day", action="store_true", help="关闭A股T+1限制，允许买入当天卖出；只用于对比")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    return run_backtest(args)


if __name__ == "__main__":
    raise SystemExit(main())
