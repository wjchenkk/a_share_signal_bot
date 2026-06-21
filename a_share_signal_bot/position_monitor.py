#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中持仓交易建议监控。

由 OpenClaw/cron 在 10:00-10:30、14:00-14:30 每 10 分钟调用。
本脚本只输出交易建议，不自动下单。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import scanner as bot


def _read_csv_flexible(path: Path) -> pd.DataFrame:
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


def read_portfolio(path: str, account_default: float = 0.0) -> Tuple[float, float, pd.DataFrame]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"持仓文件不存在: {path}")
    df = _read_csv_flexible(p)
    if df.empty:
        return float(account_default or 0.0), 0.0, pd.DataFrame(columns=["code", "name", "shares", "cost_price"])
    code_col = _col(df, ["code", "symbol", "股票代码", "证券代码", "代码", "股票"])
    name_col = _col(df, ["name", "股票名称", "证券简称", "名称", "简称"])
    shares_col = _col(df, ["shares", "qty", "quantity", "股数", "股票股数", "持仓股数", "数量"])
    cost_col = _col(df, ["cost_price", "cost", "buy_price", "买入价格", "成本价", "持仓成本", "成本"])
    total_col = _col(df, ["total_equity", "total_funds", "account", "equity", "总资金", "账户总资金", "账户权益"])
    cash_col = _col(df, ["cash", "available_cash", "可用现金", "现金", "可用资金"])
    if code_col is None or shares_col is None or cost_col is None:
        raise ValueError("持仓文件至少需要：股票代码/code、股数/shares、买入价格/cost_price；可选总资金/total_equity、可用现金/cash")
    rows = []
    for _, r in df.iterrows():
        try:
            code = bot.normalize_code(r.get(code_col, ""))
        except Exception:
            continue
        shares = bot.safe_float(r.get(shares_col), 0.0)
        cost = bot.safe_float(r.get(cost_col), np.nan)
        if shares <= 0 or not np.isfinite(cost) or cost <= 0:
            continue
        name = str(r.get(name_col, "")).strip() if name_col else ""
        rows.append({"code": code, "name": name, "shares": int(shares), "cost_price": float(cost)})
    out = pd.DataFrame(rows)
    if out.empty:
        total_equity = account_default
        if total_col is not None:
            vals = pd.to_numeric(df[total_col], errors="coerce").dropna()
            if not vals.empty and float(vals.iloc[0]) > 0:
                total_equity = float(vals.iloc[0])
        cash = 0.0
        if cash_col is not None:
            vals = pd.to_numeric(df[cash_col], errors="coerce").dropna()
            if not vals.empty:
                cash = float(vals.iloc[0])
        return float(total_equity or 0.0), cash, pd.DataFrame(columns=["code", "name", "shares", "cost_price"])
    total_equity = account_default
    if total_col is not None:
        vals = pd.to_numeric(df[total_col], errors="coerce").dropna()
        if not vals.empty and float(vals.iloc[0]) > 0:
            total_equity = float(vals.iloc[0])
    cash = np.nan
    if cash_col is not None:
        vals = pd.to_numeric(df[cash_col], errors="coerce").dropna()
        if not vals.empty:
            cash = float(vals.iloc[0])
    return float(total_equity or 0.0), cash, out


def normalize_minute_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    rename = {
        "时间": "datetime", "日期时间": "datetime", "day": "datetime", "date": "datetime",
        "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
        "成交量": "volume", "成交额": "amount", "均价": "avg_price",
    }
    out = df.rename(columns=rename).copy()
    if "datetime" not in out.columns:
        # 有些接口返回第一列为时间
        out = out.rename(columns={out.columns[0]: "datetime"})
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "amount", "avg_price"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "amount" not in out.columns and {"close", "volume"}.issubset(out.columns):
        out["amount"] = out["close"] * out["volume"] * 100
    out = out.dropna(subset=["datetime", "close"]).sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    return out.reset_index(drop=True)


def _snapshot_minute_from_spot(code: str, cfg: Dict[str, Any], spot_all: pd.DataFrame) -> pd.DataFrame:
    try:
        return bot.update_intraday_snapshot_from_spot(code, spot_all, cfg)
    except Exception:
        return pd.DataFrame()


def fetch_minute(code: str, cfg: Dict[str, Any], period: str = "5", spot_all: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """获取分钟K线；失败时用实时快照构造伪分时。

    免费分钟源在盘中经常被限流。v6.5 的设计是：
    1) 先尝试 AkShare 东方财富/新浪分钟K；
    2) 再尝试本地实时快照序列；
    3) 若仍不足，返回当前快照而不是让持仓监控失败。
    """
    providers = cfg.get("position_monitor", {}).get("minute_providers", ["eastmoney", "sina", "snapshot"])
    if isinstance(providers, str):
        providers = [x.strip() for x in providers.split(",") if x.strip()]
    errors: List[str] = []
    today = bot.now_cn().strftime("%Y-%m-%d")
    start_dt = f"{today} 09:30:00"
    end_dt = f"{today} 15:00:00"
    for provider in providers:
        provider = str(provider).lower().strip()
        try:
            if provider in {"snapshot", "spot_snapshot", "local"}:
                if bot.is_stale_data_frame(spot_all):
                    raise ValueError("实时行情为旧缓存，跳过本地快照")
                out = _snapshot_minute_from_spot(code, cfg, spot_all if spot_all is not None else pd.DataFrame())
                out = normalize_minute_df(out)
                if not out.empty:
                    out.attrs["minute_provider"] = "snapshot"
                    return out
                raise ValueError("本地快照不足")
            import akshare as ak
            if provider in {"eastmoney", "em"}:
                def _call1():
                    return ak.stock_zh_a_hist_min_em(symbol=code, start_date=start_dt, end_date=end_dt, period=str(period), adjust="")
                try:
                    raw = _call1()
                except TypeError:
                    raw = ak.stock_zh_a_hist_min_em(symbol=code, period=str(period), adjust="")
            elif provider in {"sina", "sinajs"}:
                symbol = bot.market_code_prefix(code)
                try:
                    raw = ak.stock_zh_a_minute(symbol=symbol, period=str(period), adjust="")
                except TypeError:
                    raw = ak.stock_zh_a_minute(symbol=symbol, period=str(period))
            else:
                continue
            out = normalize_minute_df(raw)
            if not out.empty:
                out.attrs["minute_provider"] = provider
                return out
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            continue
    # 最终兜底：不抛异常，返回快照。持仓监控宁可退化，也不能崩。
    if bool(cfg.get("position_monitor", {}).get("minute_allow_snapshot_fallback", True)) and not bot.is_stale_data_frame(spot_all):
        out = _snapshot_minute_from_spot(code, cfg, spot_all if spot_all is not None else pd.DataFrame())
        out = normalize_minute_df(out)
        if not out.empty:
            out.attrs["minute_provider"] = "snapshot_fallback"
            out.attrs["minute_warning"] = "真实分钟K失败，使用本地实时快照序列：" + " | ".join(errors)
            return out
    raise RuntimeError("分钟K线获取失败：" + " | ".join(errors))


def minute_window_for_today(mdf: pd.DataFrame, current_date: Optional[datetime] = None) -> Tuple[pd.DataFrame, str]:
    if mdf is None or mdf.empty or "datetime" not in mdf.columns:
        return pd.DataFrame(), "分钟K缺失"
    out = mdf.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        return pd.DataFrame(), "分钟K缺失"
    today = (current_date or bot.now_cn()).date()
    today_df = out[out["datetime"].dt.date == today].copy()
    if today_df.empty:
        latest = out["datetime"].max()
        latest_text = pd.Timestamp(latest).strftime("%Y-%m-%d") if pd.notna(latest) else "未知日期"
        return pd.DataFrame(), f"当日分钟K缺失（最新{latest_text}），未使用历史分钟K"
    return today_df.reset_index(drop=True), ""

def current_spot_row(spot: pd.DataFrame, code: str) -> Dict[str, float]:
    if spot is None or spot.empty or "代码" not in spot.columns:
        return {}
    row = spot[spot["代码"].astype(str).str.zfill(6) == bot.normalize_code(code)]
    if row.empty:
        return {}
    r = row.iloc[0]
    def f(col: str, default: float = np.nan) -> float:
        return bot.safe_float(r.get(col), default)
    return {
        "price": f("最新价"), "open": f("今开"), "high": f("最高"), "low": f("最低"),
        "pct_chg": f("涨跌幅"), "amount": f("成交额"), "turnover": f("换手率"),
    }


def in_allowed_window(now: datetime, windows: List[str]) -> bool:
    cur = now.strftime("%H:%M")
    for w in windows:
        m = re.match(r"^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$", str(w).strip())
        if not m:
            continue
        if m.group(1) <= cur <= m.group(2):
            return True
    return False


def round_lot(shares: float, lot: int = 100) -> int:
    if shares <= 0:
        return 0
    return int(math.floor(shares / lot) * lot)


def price_range(price: float, mode: str) -> str:
    if not np.isfinite(price) or price <= 0:
        return "按盘口成交"
    if mode == "urgent_sell":
        lo, hi = price * 0.985, price * 0.998
    elif mode == "sell":
        lo, hi = price * 0.995, price * 1.005
    elif mode == "buy":
        lo, hi = price * 0.998, price * 1.010
    else:
        lo, hi = price * 0.992, price * 1.008
    return f"{lo:.2f}~{hi:.2f}"


def analyze_position(
    pos: pd.Series,
    cfg: Dict[str, Any],
    fetcher: bot.AkshareFetcher,
    market: bot.MarketState,
    spot_all: pd.DataFrame,
    total_equity: float,
    inferred_cash: float,
) -> Dict[str, Any]:
    pm = cfg.get("position_monitor", {})
    code = str(pos["code"]).zfill(6)
    name = str(pos.get("name", ""))
    shares = int(pos["shares"])
    cost = float(pos["cost_price"])
    adjust = cfg.get("data", {}).get("adjust", "qfq")
    start_date = cfg.get("data", {}).get("start_date") or (bot.now_cn() - timedelta(days=500)).strftime("%Y%m%d")
    end_date = cfg.get("data", {}).get("end_date") or bot.today_yyyymmdd()
    result: Dict[str, Any] = {"code": code, "name": name, "shares": shares, "cost_price": cost}
    try:
        hist = fetcher.stock_hist(code, start_date, end_date, adjust)
        ind = bot.add_indicators(hist, atr_period=int(cfg.get("strategy", {}).get("atr_period", 14)))
        last = ind.iloc[-1]
    except Exception as exc:
        result.update({"action": "DATA_ERROR", "action_cn": "数据错误", "reason": f"日K获取失败：{exc}", "trade_shares": 0, "price_range": ""})
        return result

    stale_spot = bot.is_stale_data_frame(spot_all)
    spot = {} if stale_spot else current_spot_row(spot_all, code)
    has_fresh_realtime = bool(spot)
    current = bot.safe_float(spot.get("price"), bot.safe_float(last.get("close")))
    if not np.isfinite(current) or current <= 0:
        current = bot.safe_float(last.get("close"))
    pct_chg = bot.safe_float(spot.get("pct_chg"), bot.safe_float(last.get("pct_chg")))
    profit_pct = current / cost - 1.0
    market_value = current * shares
    weight = market_value / total_equity if total_equity > 0 else np.nan

    ma5 = bot.safe_float(last.get("ma5")); ma10 = bot.safe_float(last.get("ma10")); ma20 = bot.safe_float(last.get("ma20")); ma60 = bot.safe_float(last.get("ma60"))
    low5 = bot.safe_float(last.get("low5")); low10 = bot.safe_float(last.get("low10")); low20 = bot.safe_float(last.get("low20")); atr = bot.safe_float(last.get("atr"))
    risk_gate = bot.compute_risk_gate(ind, code, name, cfg)
    hard_stop = max([x for x in [cost * (1 - float(pm.get("max_loss_pct", 0.06))), ma20 * 0.975 if np.isfinite(ma20) else np.nan, low10 * 0.985 if np.isfinite(low10) else np.nan, current - 2.2 * atr if np.isfinite(atr) else np.nan] if np.isfinite(x) and x > 0] or [cost * 0.94])
    trend_stop = max([x for x in [ma10 * 0.985 if np.isfinite(ma10) else np.nan, ma20 * 0.985 if np.isfinite(ma20) else np.nan, low20 * 0.985 if np.isfinite(low20) else np.nan] if np.isfinite(x) and x > 0] or [hard_stop])

    intraday_vwap = np.nan; minute_ma = np.nan; intraday_high = bot.safe_float(spot.get("high")); intraday_low = bot.safe_float(spot.get("low")); intraday_note = "实时行情为旧缓存，按日K收盘价观察" if stale_spot else "分钟K缺失"
    try:
        mdf = fetch_minute(code, cfg, period=str(pm.get("minute_period", "5")), spot_all=spot_all)
        if not mdf.empty:
            mdf_today, minute_warning = minute_window_for_today(mdf)
            if mdf_today.empty:
                intraday_note = minute_warning
            else:
                if "amount" in mdf_today.columns and "volume" in mdf_today.columns and pd.to_numeric(mdf_today["volume"], errors="coerce").sum() > 0:
                    intraday_vwap = float(pd.to_numeric(mdf_today["amount"], errors="coerce").sum() / (pd.to_numeric(mdf_today["volume"], errors="coerce").sum() * 100))
                else:
                    intraday_vwap = float(pd.to_numeric(mdf_today["close"], errors="coerce").mean())
                minute_ma = float(pd.to_numeric(mdf_today["close"], errors="coerce").rolling(6, min_periods=2).mean().iloc[-1])
                intraday_high = float(pd.to_numeric(mdf_today["high"] if "high" in mdf_today.columns else mdf_today["close"], errors="coerce").max())
                intraday_low = float(pd.to_numeric(mdf_today["low"] if "low" in mdf_today.columns else mdf_today["close"], errors="coerce").min())
                intraday_note = f"VWAP {intraday_vwap:.2f}，分钟均线 {minute_ma:.2f}，源={mdf.attrs.get('minute_provider', 'unknown')}"
    except Exception as exc:
        intraday_note = f"分钟K失败：{str(exc)[:80]}"

    weak_intraday = False
    if np.isfinite(intraday_vwap) and current < intraday_vwap * 0.995:
        weak_intraday = True
    if np.isfinite(minute_ma) and current < minute_ma * 0.995:
        weak_intraday = True
    from_high_dd = current / intraday_high - 1 if np.isfinite(intraday_high) and intraday_high > 0 else np.nan

    reasons: List[str] = []
    action = "HOLD"; action_cn = "持有观察"; trade_shares = 0; mode = "hold"
    if bool(risk_gate.get("risk_gate_block", False)):
        action = "SELL"; action_cn = "风险卖出"; trade_shares = round_lot(shares * float(pm.get("hard_stop_sell_pct", 1.0)), int(pm.get("min_trade_lot", 100))); mode = "urgent_sell"
        reasons.append("A股风险闸门触发：" + str(risk_gate.get("risk_gate_reason", "")))
    elif current <= hard_stop:
        action = "SELL"; action_cn = "止损卖出"; trade_shares = round_lot(shares * float(pm.get("hard_stop_sell_pct", 1.0)), int(pm.get("min_trade_lot", 100))); mode = "urgent_sell"
        reasons.append(f"现价跌破硬止损{hard_stop:.2f}")
    elif market.target_exposure <= 0 and current < trend_stop:
        action = "SELL"; action_cn = "大盘弱势减仓/退出"; trade_shares = round_lot(shares * 0.50, int(pm.get("min_trade_lot", 100))); mode = "sell"
        reasons.append("大盘弱势且持仓跌破趋势防守线")
    elif current < trend_stop and weak_intraday:
        action = "REDUCE"; action_cn = "趋势破位减仓"; trade_shares = round_lot(shares * float(pm.get("trend_break_reduce_pct", 0.50)), int(pm.get("min_trade_lot", 100))); mode = "sell"
        reasons.append(f"跌破趋势防守线{trend_stop:.2f}且盘中弱于VWAP/分钟均线")
    elif profit_pct >= float(pm.get("profit_protect_2", 0.15)) and (weak_intraday or (np.isfinite(from_high_dd) and from_high_dd <= -0.025)):
        action = "TAKE_PROFIT"; action_cn = "盈利保护止盈"; trade_shares = round_lot(shares * 0.50, int(pm.get("min_trade_lot", 100))); mode = "sell"
        reasons.append(f"浮盈{profit_pct:.1%}，盘中转弱/从高点回落{from_high_dd:.1%}")
    elif profit_pct >= float(pm.get("profit_protect_1", 0.08)) and weak_intraday:
        action = "TAKE_PROFIT"; action_cn = "部分止盈"; trade_shares = round_lot(shares * float(pm.get("profit_reduce_pct", 0.33)), int(pm.get("min_trade_lot", 100))); mode = "sell"
        reasons.append(f"浮盈{profit_pct:.1%}但跌破VWAP/分钟均线，先锁定部分利润")
    elif profit_pct < 0 and weak_intraday and current < ma10:
        action = "REDUCE"; action_cn = "弱势减仓"; trade_shares = round_lot(shares * float(pm.get("weak_intraday_reduce_pct", 0.33)), int(pm.get("min_trade_lot", 100))); mode = "sell"
        reasons.append("持仓亏损且盘中弱势，避免亏损扩大")
    else:
        reasons.append("未触发止损/趋势破位/盈利保护条件")
        # 加仓只给低优先级建议：必须大盘允许、趋势强、盘中强于VWAP、有可用现金且当前仓位未超上限。
        if bool(pm.get("allow_add", True)) and market.target_exposure > 0 and inferred_cash > current * 100:
            add_cap = float(pm.get("add_max_position_pct", 0.18))
            trend_ok = np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and current >= ma5 >= ma10 >= ma20
            intraday_strong = np.isfinite(intraday_vwap) and current >= intraday_vwap * 1.002
            not_chasing = not np.isfinite(pct_chg) or pct_chg <= 4.8
            if has_fresh_realtime and trend_ok and intraday_strong and not_chasing and (not np.isfinite(weight) or weight < add_cap * 0.75):
                target_value = min(total_equity * add_cap, market_value + total_equity * float(pm.get("add_step_pct", 0.25)) * add_cap)
                buy_cash = max(0.0, min(target_value - market_value, inferred_cash))
                buy_shares = round_lot(buy_cash / current, int(pm.get("min_trade_lot", 100)))
                if buy_shares >= int(pm.get("min_trade_lot", 100)):
                    action = "ADD"; action_cn = "趋势加仓"; trade_shares = buy_shares; mode = "buy"
                    reasons.append("趋势强、盘中强于VWAP且仓位未到上限，可小步加仓")

    if trade_shares > 0 and not has_fresh_realtime:
        reasons.append("实时行情不可用，原交易动作降级为预警，需确认盘口后执行")
        action = "WARN"; action_cn = "实时价缺失预警"; trade_shares = 0; mode = "hold"
    if trade_shares > shares and action in {"SELL", "REDUCE", "TAKE_PROFIT"}:
        trade_shares = round_lot(shares, int(pm.get("min_trade_lot", 100)))
    result.update({
        "name": name,
        "current_price": current,
        "pct_chg": pct_chg,
        "profit_pct": profit_pct,
        "market_value": market_value,
        "weight": weight,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "hard_stop": hard_stop,
        "trend_stop": trend_stop,
        "intraday_vwap": intraday_vwap,
        "minute_ma": minute_ma,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "action": action,
        "action_cn": action_cn,
        "trade_shares": int(trade_shares),
        "price_range": price_range(current, mode) if trade_shares else "",
        "reason": "；".join(bot.unique_nonempty(reasons)),
        "intraday_note": intraday_note,
    })
    return result


def format_message(actions: pd.DataFrame, market: bot.MarketState, total_equity: float, inferred_cash: float) -> str:
    now = bot.now_cn().strftime("%Y-%m-%d %H:%M")
    lines = [f"A股持仓盘中交易建议 {now}", market.summary]
    lines.append(f"账户权益约 {total_equity:,.0f} 元；估算可用现金 {inferred_cash:,.0f} 元。")
    if actions.empty:
        lines.append("没有识别到持仓。")
        return "\n".join(lines)
    need = actions[actions["trade_shares"].fillna(0).astype(int) > 0]
    if need.empty:
        lines.append("当前持仓未触发交易动作，建议持有观察。")
    else:
        lines.append(f"触发交易建议 {len(need)} 条：")
        for _, r in need.iterrows():
            lines.append(
                f"- {r['code']} {r.get('name','')}：{r['action_cn']} {int(r['trade_shares'])}股，参考价区间 {r['price_range']}；"
                f"现价{float(r['current_price']):.2f}，浮盈{float(r['profit_pct']):.1%}。原因：{r['reason']}"
            )
    lines.append("")
    lines.append("持仓状态：")
    for _, r in actions.iterrows():
        lines.append(
            f"- {r['code']} {r.get('name','')}：{r['action_cn']}；现价{float(r['current_price']):.2f}，"
            f"浮盈{float(r['profit_pct']):.1%}，硬止损{float(r['hard_stop']):.2f}，趋势防守{float(r['trend_stop']):.2f}；{r['intraday_note']}"
        )
    lines.append("")
    lines.append("提示：这是日K+分钟K近似风控建议，不自动下单；涨跌停队列、停牌、盘口冲击需人工确认。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> Tuple[pd.DataFrame, str, Path]:
    cfg = bot.load_config(args.config)
    cfg["data"]["use_realtime_tail"] = True
    if args.refresh:
        refresh = True
    else:
        refresh = False
    pm = cfg.get("position_monitor", {})
    out_dir = Path(args.out or pm.get("out_dir", "position_output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    now = bot.now_cn()
    windows = pm.get("check_windows", ["10:00-10:30", "14:00-14:30"])
    window_only = bool(args.window_only or pm.get("window_only", False))
    if window_only and not in_allowed_window(now, windows):
        msg = f"当前 {now.strftime('%H:%M')} 不在持仓监控窗口 {', '.join(windows)}，本次不扫描。"
        (out_dir / "latest_position_message.txt").write_text(msg, encoding="utf-8")
        return pd.DataFrame(), msg, out_dir / "latest_position_message.txt"

    total_equity, cash, positions = read_portfolio(args.portfolio, account_default=args.account)
    fetcher = bot.AkshareFetcher(cfg, refresh=refresh)
    market = bot.evaluate_market(fetcher, cfg)
    if positions.empty:
        msg = format_message(pd.DataFrame(), market, total_equity, cash if np.isfinite(cash) else 0.0)
        msg_path = out_dir / "latest_position_message.txt"
        pd.DataFrame().to_csv(out_dir / "latest_position_actions.csv", index=False, encoding="utf-8-sig")
        msg_path.write_text(msg, encoding="utf-8")
        return pd.DataFrame(), msg, msg_path
    name_map = bot.fetch_name_map(fetcher)
    positions["name"] = positions.apply(lambda r: r["name"] if str(r["name"]).strip() else name_map.get(str(r["code"]).zfill(6), ""), axis=1)
    try:
        spot = fetcher.stock_spot_all()
    except Exception:
        spot = pd.DataFrame()
    # 先用实时价估算现金。如果持仓文件提供 cash，则优先使用。
    mv = 0.0
    for _, p in positions.iterrows():
        sr = current_spot_row(spot, str(p["code"]).zfill(6))
        price = bot.safe_float(sr.get("price"), p["cost_price"])
        mv += price * int(p["shares"])
    inferred_cash = cash if np.isfinite(cash) else max(0.0, total_equity - mv)
    rows: List[Dict[str, Any]] = []
    for _, pos in positions.iterrows():
        rows.append(analyze_position(pos, cfg, fetcher, market, spot, total_equity, inferred_cash))
    actions = pd.DataFrame(rows)
    msg = format_message(actions, market, total_equity, inferred_cash)
    ts = now.strftime("%Y%m%d_%H%M%S")
    actions.to_csv(out_dir / f"position_actions_{ts}.csv", index=False, encoding="utf-8-sig")
    actions.to_csv(out_dir / "latest_position_actions.csv", index=False, encoding="utf-8-sig")
    msg_path = out_dir / "latest_position_message.txt"
    msg_path.write_text(msg, encoding="utf-8")
    return actions, msg, msg_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="A股盘中持仓交易建议监控")
    ap.add_argument("--portfolio", default="portfolio.csv", help="持仓文件 CSV：股票代码、股数、买入价格，可选总资金/现金")
    ap.add_argument("--config", default="config.example.yml", help="配置文件")
    ap.add_argument("--out", default="position_output", help="输出目录")
    ap.add_argument("--account", type=float, default=200000.0, help="默认账户权益；若持仓文件有总资金列，以文件为准")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--window-only", action="store_true", help="仅在配置的盘中窗口运行")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        _, msg, _ = run(args)
        print(msg)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
