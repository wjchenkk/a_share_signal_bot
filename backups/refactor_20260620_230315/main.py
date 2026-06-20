#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股尾盘/收盘 K 线买入信号扫描器 v6.5

功能：
1) 读取股票池 CSV/TXT/XLSX；
2) 用 AkShare 拉取个股日 K 和大盘指数日 K；
3) 大盘过滤 + 板块主线 + 个股板块内强度 + 突破/回踩/天量锚点再异动 + ATR 风控；
4) 输出买入信号、目标仓位、建议股数、止损价、止盈价；
5) 可选通过企业微信/钉钉/飞书 webhook 推送文本消息；
6) 支持“小龙虾/AI Agent”通过对话命令增删查股票池、手动触发扫描；
7) 支持定时扫描后在股票池超过阈值时自动淘汰图形最弱的股票。

注意：本脚本只生成信号，不自动下单。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "data": {
        "start_date": "20220101",
        "end_date": "",
        "adjust": "qfq",  # qfq / hfq / "". 尾盘实时模式会自动改成不复权，避免复权价与实时价混用。
        "market_indices": ["sh000001", "sz399001"],  # 上证指数、深证成指
        "cache_dir": "cache",
        "cache_hours": 24,
        "sleep_seconds": 0.35,
        "use_realtime_tail": False,
        # 个股日 K 默认不再优先用东方财富，避免 stock_zh_a_hist 被限流后整池失败。
        # 免费可用的数据源：tencent=腾讯 stock_zh_a_hist_tx；sina=新浪 stock_zh_a_daily；eastmoney=东方财富 stock_zh_a_hist。
        # 同花顺专业 OpenAPI 通常需要授权和账号，本脚本先预留 provider 名称，不把未授权接口硬编码进去。
        "hist_providers": ["tencent", "sina", "eastmoney"],
        "spot_providers": ["akshare_em", "efinance", "tencent_direct"],
        "spot_cache_minutes": 2,
        "prefer_pool_spot": True,  # 扫描只需要股票池实时行情，优先用按池批量 quote，避免全市场实时源拖慢。
        "two_stage_scan": True,
        "max_scan_per_run": 45,
        "prefilter_pool_when_gt": 60,
        "prefilter_keep_pct_chg_min": -6.0,
        "prefilter_keep_pct_chg_max": 8.8,
        "intraday_buy_filter": True,
        "intraday_snapshot_dir": "cache/intraday_snapshots",
        "index_providers": ["tencent", "eastmoney", "legacy"],
        "request_retries": 3,
        "retry_backoff_seconds": [1.5, 4.0, 8.0],
        "allow_stale_cache_on_error": True,
        "max_error_rate_for_valid_run": 0.20,
    },
    "strategy": {
        "min_history_days": 160,
        "min_amount_ma20": 50_000_000,  # 20 日平均成交额，默认 5000 万
        "min_close": 3.0,
        "max_close": 300.0,
        "exclude_st": True,
        "score_threshold": 72.0,
        "max_positions": 8,
        "max_position_pct": 0.22,       # 单票最大仓位；v6.3 按信号等级动态调整
        "risk_per_trade_pct": 0.010,    # 单票止损触发时，组合最大亏损约 0.6%
        "atr_period": 14,
        "atr_mult": 2.5,
        "min_stop_pct": 0.04,
        "max_stop_pct": 0.12,
        # v6：主线-买点分型。sector 可以来自股票池 sector/板块/行业 列，也可以来自 sector_map.csv。
        "sector": {
            "enabled": True,
            "sector_map_path": "sector_map.csv",
            # v6.1：自动补全板块。concept_first=概念优先、行业托底；industry/sw=行业兜底。
            "auto_fill": True,
            "auto_source": "concept_first",   # concept_first / concept / industry / sw / none
            "auto_fallback_sources": ["industry", "sw"],
            "auto_write_back": True,          # 行业模式可写回 stock_pool.csv；概念模式只写缓存，避免题材固化
            "auto_map_path": "cache/auto_sector_map_concept_first.csv",
            "auto_scan_max_boards": 0,        # 0 表示扫描全部行业/概念板块；调试时可设 20
            "min_sector_size": 3,
            "mainline_score_threshold": 66.0,
            "strong_score_threshold": 55.0,
            # v6.3：板块主线从“加分项”升级为入场门槛。
            "use_board_hist_strength": True,
            "hard_gate_enabled": True,
            "min_score_for_any_buy": 55.0,
            "min_score_for_pullback": 66.0,
            "min_score_for_breakout": 60.0,
            "min_score_for_anchor": 64.0,
            "front_rs20_min": 0.60,
            "front_rs60_min": 0.55,
            "core_rs20_min": 0.75,
            "require_outperform_sector20": True,
            "require_mainline_for_pullback": True,
            "prefer_mainline_for_breakout": True
        },
        "setup": {
            "enabled": True,
            "allowed_types": ["breakout", "pullback", "volume_anchor_reaccumulation"],
            "breakout_score_threshold": 66.0,
            "pullback_score_threshold": 66.0,
            "anchor_score_threshold": 68.0,
            "volume_anchor_lookback": 20,
            "volume_anchor_min_days_ago": 3,
            "volume_anchor_max_days_ago": 20,
            "volume_anchor_amount_ratio": 3.0,
            "volume_anchor_min_pct": 7.0,
            "volume_anchor_hold_low_pct": -0.03,
            "volume_anchor_midline_pct": 0.50,
            "pullback_ma_options_strong": [5, 10],
            "pullback_ma_options_normal": [10, 20],
            "pullback_distance_pct": 0.035,
            "breakout_amount_min": 1.08,
            "breakout_amount_max": 2.60,
            "pullback_amount_max": 1.15,
            "reaccum_amount_min": 1.25,
            "breakout_need_contraction": True,
            "pullback_need_dryup": True,
            "strong_mainline_pullback_ma": [5, 10],
            "normal_mainline_pullback_ma": [10, 20]
        },
        "max_chase_day_pct": 7.5,       # 单日涨幅过大不追
        "min_day_pct": -5.5,            # 单日跌幅过大不接飞刀
        "avoid_upper_shadow_pct": 0.08, # 长上影过大过滤
        "total_exposure_strong": 0.95,
        "total_exposure_neutral": 0.65,
        "total_exposure_weak": 0.00,
        # A 股专用风险闸门：先排雷，再评分。用于过滤连续跌停、高位崩盘、暴涨后踩踏。
        "risk_gate": {
            "enabled": True,
            "limit_down_count_3_block": 1,
            "limit_down_count_6_block": 2,
            "limit_down_count_10_block": 3,
            "ret3_min": -0.12,
            "ret5_min": -0.18,
            "drawdown10_max": -0.22,
            "drawdown20_max": -0.30,
            "climax_ret30": 0.80,
            "climax_ret60": 1.20,
            "climax_drawdown10": -0.12,
            "climax_amount_ratio20": 2.8,
            "min_cooling_days_after_limit_down": 5
        },
    },
    "position_monitor": {
        "portfolio_path": "portfolio.csv",
        "out_dir": "position_output",
        "minute_period": "5",
        "minute_providers": ["eastmoney", "sina", "snapshot"],
        "minute_allow_snapshot_fallback": True,
        "check_windows": ["10:00-10:30", "14:00-14:30"],
        "window_only": False,
        "max_loss_pct": 0.06,
        "trend_break_reduce_pct": 0.50,
        "hard_stop_sell_pct": 1.00,
        "profit_protect_1": 0.08,
        "profit_protect_2": 0.15,
        "profit_reduce_pct": 0.33,
        "weak_intraday_reduce_pct": 0.33,
        "allow_add": True,
        "add_max_position_pct": 0.18,
        "add_step_pct": 0.25,
        "min_trade_lot": 100
    },

    "notify": {
        "webhook_url": "",
        "webhook_type": "wecom",  # wecom / dingtalk / feishu / generic
        "send": False,
    },
    "pool": {
        "auto_prune": False,          # 定时任务建议通过 --auto-prune 显式开启；手动对话扫描默认不删池子。
        "max_size": 50,               # 股票池超过该数量才触发淘汰
        "prune_count": 10,            # 每次触发时淘汰数量
        "backup_dir": "pool_backups",
    },
    "report": {
        # 默认推送/聊天只输出精简摘要；逐股原因仍会生成到文件，等你说“解释/为什么”再展示。
        "include_explanations_in_message": False,
        "show_no_signal_top": 12,      # 详细模式中展示多少只“未买入/接近达标”的解释
        "max_report_stocks": 120,      # latest_report.md 中最多写入多少只逐股解释；股票池 50 只以内会全部写入
        "show_blocker_counts": True,   # 是否统计今日未买入的主要拦截原因
    },
    "output": {
        "cleanup_on_run": True,
        "retention_days": 14,
        "max_history_files": 300,
        "keep_latest_files": True,
    },
}


@dataclass
class MarketState:
    date: str
    score: float
    regime: str
    target_exposure: float
    details: pd.DataFrame
    summary: str
    market_ret20: float
    market_ret60: float


@dataclass
class PruneReport:
    enabled: bool
    triggered: bool
    pool_size_before: int
    pool_size_after: int
    removed: pd.DataFrame
    message: str
    backup_path: str = ""


@dataclass
class ChatAction:
    kind: str  # add / remove / list / scan / explain / prune / help / unknown
    text: str
    items: List[Tuple[str, str]]
    terms: List[str]
    auto_prune: bool = False


def now_cn() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    return datetime.utcnow() + timedelta(hours=8)


def today_yyyymmdd() -> str:
    return now_cn().strftime("%Y%m%d")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".json"}:
        user_cfg = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("读取 YAML 配置需要安装 PyYAML：pip install pyyaml") from exc
        user_cfg = yaml.safe_load(text) or {}
    return deep_merge(cfg, user_cfg)


def normalize_code(value: Any) -> str:
    s = str(value).strip().upper()
    s = s.replace(" ", "")
    # 支持 600519.SH / SH600519 / sh600519 / 600519
    m = re.search(r"(\d{6})", s)
    if not m:
        raise ValueError(f"无法识别股票代码: {value}")
    return m.group(1).zfill(6)


def read_stock_pool(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"股票池文件不存在: {path}")

    if p.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(p, dtype=str)
    else:
        try:
            df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(p, dtype=str, encoding="gbk")
        except pd.errors.ParserError:
            df = pd.read_csv(p, dtype=str, header=None, encoding="utf-8-sig")

    if df.empty:
        raise ValueError("股票池为空")

    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    code_col = None
    for candidate in ["code", "symbol", "ticker", "股票代码", "证券代码", "代码"]:
        if candidate.lower() in cols_lower:
            code_col = cols_lower[candidate.lower()]
            break
    if code_col is None:
        code_col = df.columns[0]

    name_col = None
    for candidate in ["name", "股票名称", "证券简称", "名称", "简称"]:
        if candidate.lower() in cols_lower:
            name_col = cols_lower[candidate.lower()]
            break

    sector_col = None
    for candidate in ["sector", "industry", "theme", "board", "板块", "行业", "概念", "所属板块", "所属行业"]:
        if candidate.lower() in cols_lower:
            sector_col = cols_lower[candidate.lower()]
            break

    rows = []
    for _, row in df.iterrows():
        raw = row.get(code_col)
        if pd.isna(raw):
            continue
        try:
            code = normalize_code(raw)
        except ValueError:
            continue
        name = ""
        if name_col is not None and not pd.isna(row.get(name_col)):
            name = str(row.get(name_col)).strip()
        sector = ""
        if sector_col is not None and not pd.isna(row.get(sector_col)):
            sector = str(row.get(sector_col)).strip()
        rows.append({"code": code, "name": name, "sector": sector})

    out = pd.DataFrame(rows).drop_duplicates(subset=["code"]).reset_index(drop=True)
    if out.empty:
        raise ValueError("没有从股票池中识别出有效的 6 位 A 股代码")
    return out


def empty_stock_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=["code", "name", "sector"])


MISSING_SECTOR_VALUES = {"", "nan", "none", "null", "unknown", "未分组", "未分类", "未归类", "无"}


def normalize_sector_value(value: Any) -> str:
    """Treat persisted placeholders as missing so auto sector fill can retry."""
    if value is None or pd.isna(value):
        return ""
    sector = str(value).strip()
    if sector.lower() in MISSING_SECTOR_VALUES:
        return ""
    return sector


def write_stock_pool(df: pd.DataFrame, path: str | Path) -> pd.DataFrame:
    """规范化并写回股票池。默认写 code/name 两列，保证代码前导 0 不丢失。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        out = empty_stock_pool()
    else:
        rows = []
        for _, r in df.iterrows():
            try:
                code = normalize_code(r.get("code", r.iloc[0] if len(r) else ""))
            except Exception:
                continue
            name = str(r.get("name", "")).strip() if "name" in df.columns else ""
            sector = normalize_sector_value(r.get("sector", "")) if "sector" in df.columns else ""
            rows.append({"code": code, "name": name, "sector": sector})
        out = pd.DataFrame(rows, columns=["code", "name", "sector"]) if rows else empty_stock_pool()
        out = out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)

    if p.suffix.lower() in {".xlsx", ".xls"}:
        out.to_excel(p, index=False)
    else:
        out.to_csv(p, index=False, encoding="utf-8-sig")
    return out


def read_stock_pool_or_empty(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return empty_stock_pool()
    try:
        return read_stock_pool(str(p))
    except ValueError as exc:
        if "为空" in str(exc) or "没有从股票池" in str(exc):
            return empty_stock_pool()
        raise


def backup_stock_pool(path: str | Path, backup_dir: str | Path = "pool_backups") -> str:
    p = Path(path)
    if not p.exists():
        return ""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = p.parent / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = now_cn().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{p.stem}_{ts}{p.suffix or '.csv'}"
    shutil.copy2(p, backup_path)
    return str(backup_path)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_cache_fresh(path: Path, hours: float) -> bool:
    if not path.exists() or hours <= 0:
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age.total_seconds() < hours * 3600


def market_code_prefix(code: str) -> str:
    """把 6 位 A 股代码转成带市场前缀的代码：sh600519 / sz000001 / bj8xxxxx。"""
    code = normalize_code(code)
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return "sz" + code


def provider_list(cfg: Dict[str, Any], key: str, default: List[str]) -> List[str]:
    providers = cfg.get("data", {}).get(key, default)
    if isinstance(providers, str):
        providers = [x.strip() for x in providers.split(",") if x.strip()]
    out = []
    for item in providers or []:
        item = str(item).strip().lower()
        if item and item not in out:
            out.append(item)
    return out or default


def normalize_board_source(kind: str) -> str:
    source = str(kind or "sw").lower().strip()
    aliases = {
        "em": "industry",
        "eastmoney": "industry",
        "em_industry": "industry",
        "eastmoney_industry": "industry",
        "theme": "concept",
        "topic": "concept",
        "ths_concept": "concept",
        "sina_concept": "concept",
        "concept_priority": "concept_first",
        "concept_first": "concept_first",
        "concept_fallback": "concept_first",
        "em_concept": "em_concept",
        "eastmoney_concept": "em_concept",
        "sw_l1": "sw",
        "sw_level1": "sw",
        "sw_industry": "sw",
        "shenwan": "sw",
        "申万": "sw",
    }
    return aliases.get(source, source)


def non_latest_output_file(path: Path) -> bool:
    name = path.name
    if name.startswith(("latest_", "last_")):
        return False
    if name in {"stock_pool.csv"}:
        return False
    return path.is_file()


def cleanup_output_dir(out_dir: str | Path, cfg: Dict[str, Any]) -> List[str]:
    """清理 output 历史文件：保留 latest_* 和 last_*，删除过期历史文件并控制总数。"""
    o_cfg = cfg.get("output", {})
    if not o_cfg.get("cleanup_on_run", True):
        return []
    p = ensure_dir(out_dir)
    retention_days = float(o_cfg.get("retention_days", 14))
    max_history_files = int(o_cfg.get("max_history_files", 300))
    now_ts = time.time()
    removed: List[str] = []
    files = [x for x in p.iterdir() if non_latest_output_file(x)]
    for f in files:
        try:
            if retention_days > 0 and now_ts - f.stat().st_mtime > retention_days * 86400:
                f.unlink()
                removed.append(f.name)
        except Exception:
            pass
    files = sorted([x for x in p.iterdir() if non_latest_output_file(x)], key=lambda x: x.stat().st_mtime, reverse=True)
    if max_history_files > 0 and len(files) > max_history_files:
        for f in files[max_history_files:]:
            try:
                f.unlink()
                removed.append(f.name)
            except Exception:
                pass
    return removed


class AkshareFetcher:
    def __init__(self, cfg: Dict[str, Any], refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh
        self.cache_dir = ensure_dir(cfg["data"].get("cache_dir", "cache"))
        self.cache_hours = float(cfg["data"].get("cache_hours", 24))
        self.sleep_seconds = float(cfg["data"].get("sleep_seconds", 0.8))
        self.request_retries = int(cfg["data"].get("request_retries", 3))
        self.retry_backoff_seconds = list(cfg["data"].get("retry_backoff_seconds", [1.5, 4.0, 8.0]))
        self.allow_stale_cache_on_error = bool(cfg["data"].get("allow_stale_cache_on_error", True))
        self.stock_providers = provider_list(cfg, "hist_providers", ["tencent", "sina", "eastmoney"])
        self.index_providers = provider_list(cfg, "index_providers", ["tencent", "eastmoney", "legacy"])
        self._spot_cache: Optional[pd.DataFrame] = None
        self._index_spot_cache: Optional[pd.DataFrame] = None
        self._board_names_cache: Dict[str, pd.DataFrame] = {}
        self._board_symbol_cache: Dict[str, Tuple[set, Dict[str, str]]] = {}
        self._board_hist_cache: Dict[Tuple[str, str, str, str, str], pd.DataFrame] = {}
        self._active_pool_codes_cache: Optional[List[str]] = None
        self._ths_concept_hist_symbol_cache: Optional[Tuple[set, Dict[str, str]]] = None

    def _read_cache(self, path: Path, ignore_freshness: bool = False) -> Optional[pd.DataFrame]:
        if path.exists() and (ignore_freshness or ((not self.refresh) and is_cache_fresh(path, self.cache_hours))):
            try:
                return pd.read_csv(path, dtype=str)
            except Exception:
                return None
        return None

    def _write_cache(self, df: pd.DataFrame, path: Path) -> None:
        try:
            df.to_csv(path, index=False, encoding="utf-8-sig")
        except Exception:
            pass

    def _with_retry(self, label: str, func) -> pd.DataFrame:
        last_exc: Optional[Exception] = None
        attempts = max(1, self.request_retries)
        for attempt in range(attempts):
            try:
                df = func()
                if df is None or df.empty:
                    raise ValueError(f"{label} 返回空数据")
                return df
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    wait = self.retry_backoff_seconds[min(attempt, len(self.retry_backoff_seconds) - 1)] if self.retry_backoff_seconds else 1.5
                    time.sleep(float(wait))
        assert last_exc is not None
        raise last_exc

    def _fallback_stale_cache(self, pattern: str, normalizer) -> Optional[pd.DataFrame]:
        if not self.allow_stale_cache_on_error:
            return None
        files = sorted(self.cache_dir.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files:
            cached = self._read_cache(f, ignore_freshness=True)
            if cached is not None and not cached.empty:
                df = normalizer(cached)
                df.attrs["data_provider"] = "stale_cache"
                df.attrs["data_warning"] = f"实时数据源失败，使用本地旧缓存：{f.name}"
                return df
        return None

    def _board_symbol_lookup(self, kind: str) -> Tuple[set, Dict[str, str]]:
        kind = normalize_board_source(kind)
        if kind in self._board_symbol_cache:
            return self._board_symbol_cache[kind]
        names = self._read_cache(self.cache_dir / f"board_{kind}_names.csv", ignore_freshness=True) if self.allow_stale_cache_on_error else None
        if names is None:
            names = self.board_names(kind)
        valid_names: set = set()
        code_to_name: Dict[str, str] = {}
        if not names.empty:
            cols = {str(c).strip(): c for c in names.columns}
            name_col = cols.get("板块名称") or cols.get("名称") or cols.get("指数名称")
            code_col = cols.get("板块代码") or cols.get("代码") or cols.get("label") or cols.get("指数代码")
            if name_col is not None:
                valid_names = {str(x).strip() for x in names[name_col].dropna().tolist() if str(x).strip()}
            if name_col is not None and code_col is not None:
                for _, row in names[[name_col, code_col]].dropna(subset=[code_col]).iterrows():
                    name = str(row.get(name_col, "")).strip()
                    code = str(row.get(code_col, "")).strip()
                    if name and code:
                        code_to_name[code] = name
        self._board_symbol_cache[kind] = (valid_names, code_to_name)
        return valid_names, code_to_name

    def _resolve_board_hist_symbol(self, kind: str, symbol: str) -> str:
        """Fail fast for board labels that cannot belong to a provider, avoiding slow provider retries."""
        kind = normalize_board_source(kind)
        sym = str(symbol).strip()
        if not sym:
            raise ValueError(f"{kind} 板块名称为空")
        valid_names, code_to_name = self._board_symbol_lookup(kind)
        if not valid_names and not code_to_name:
            return sym
        if kind == "concept":
            if sym in valid_names:
                ths_names, ths_code_to_name = self._ths_concept_hist_lookup()
                if ths_names or ths_code_to_name:
                    if sym in ths_names:
                        return sym
                    if sym in ths_code_to_name:
                        return ths_code_to_name[sym]
                    raise ValueError(f"同花顺概念指数不支持: {symbol}")
                return sym
            if sym in code_to_name:
                concept_name = code_to_name[sym]
                ths_names, _ = self._ths_concept_hist_lookup()
                if ths_names and concept_name not in ths_names:
                    raise ValueError(f"同花顺概念指数不支持: {symbol}")
                return concept_name
            raise ValueError(f"概念板块不存在或当前数据源不支持: {symbol}")
        if kind == "sw":
            if re.fullmatch(r"\d{6}", sym):
                if sym in code_to_name or not code_to_name:
                    return sym
                raise ValueError(f"申万行业代码不存在: {symbol}")
            if sym in valid_names:
                for code, name in code_to_name.items():
                    if name == sym:
                        return code
            raise ValueError(f"申万行业不存在: {symbol}")
        if kind in {"industry", "em_concept"}:
            if sym in valid_names:
                return sym
            if sym in code_to_name:
                return code_to_name[sym]
            raise ValueError(f"{kind} 板块不存在或当前数据源不支持: {symbol}")
        return sym

    def _ths_concept_hist_lookup(self) -> Tuple[set, Dict[str, str]]:
        if self._ths_concept_hist_symbol_cache is not None:
            return self._ths_concept_hist_symbol_cache
        cache_path = self.cache_dir / "board_concept_ths_names.csv"
        names = self._read_cache(cache_path, ignore_freshness=True)
        if names is None:
            try:
                import akshare as ak
                names = self._with_retry("同花顺概念指数名录", lambda: ak.stock_board_concept_name_ths())
                self._write_cache(names, cache_path)
                time.sleep(self.sleep_seconds)
            except Exception:
                self._ths_concept_hist_symbol_cache = (set(), {})
                return self._ths_concept_hist_symbol_cache
        valid_names: set = set()
        code_to_name: Dict[str, str] = {}
        cols = {str(c).strip(): c for c in names.columns}
        name_col = cols.get("name") or cols.get("板块名称") or cols.get("概念名称")
        code_col = cols.get("code") or cols.get("板块代码") or cols.get("代码")
        if name_col is not None:
            valid_names = {str(x).strip() for x in names[name_col].dropna().tolist() if str(x).strip()}
        if name_col is not None and code_col is not None:
            for _, row in names[[name_col, code_col]].dropna(subset=[code_col]).iterrows():
                name = str(row.get(name_col, "")).strip()
                code = str(row.get(code_col, "")).strip()
                if name and code:
                    code_to_name[code] = name
        self._ths_concept_hist_symbol_cache = (valid_names, code_to_name)
        return self._ths_concept_hist_symbol_cache

    def stock_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        code = normalize_code(code)
        errors: List[str] = []
        for provider in self.stock_providers:
            provider = provider.lower().strip()
            cache_name = f"stock_{provider}_{code}_{adjust or 'none'}_{start_date}_{end_date}.csv"
            cache_path = self.cache_dir / cache_name
            cached = self._read_cache(cache_path)
            if cached is not None:
                df = normalize_stock_hist(cached)
                df.attrs["data_provider"] = provider + "_cache"
                return df
            try:
                if provider in {"tx", "tencent", "qq"}:
                    df = self._stock_hist_tencent(code, start_date, end_date, adjust)
                    provider_name = "tencent"
                elif provider in {"sina", "sinajs"}:
                    df = self._stock_hist_sina(code, start_date, end_date, adjust)
                    provider_name = "sina"
                elif provider in {"em", "eastmoney", "east"}:
                    df = self._stock_hist_eastmoney(code, start_date, end_date, adjust)
                    provider_name = "eastmoney"
                elif provider in {"ths", "tonghuashun", "10jqka"}:
                    # 同花顺 OpenAPI 通常需要授权账号；这里明确报错，让调用链自动切到后续 provider。
                    raise RuntimeError("同花顺日K接口需要授权/账号，当前免费脚本未配置 THS OpenAPI")
                else:
                    raise ValueError(f"未知个股K线数据源: {provider}")
                df = normalize_stock_hist(df)
                if df.empty:
                    raise ValueError(f"{provider} 标准化后为空")
                df.attrs["data_provider"] = provider_name
                self._write_cache(df, cache_path)
                return df
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                continue

        stale = self._fallback_stale_cache(f"stock_*_{code}_{adjust or 'none'}_*.csv", normalize_stock_hist)
        if stale is not None:
            return stale
        raise RuntimeError("所有个股K线数据源均失败：" + " | ".join(errors))

    def _stock_hist_tencent(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        import akshare as ak
        symbol = market_code_prefix(code)
        def call():
            return ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust, timeout=20)
        raw = self._with_retry(f"腾讯K线 {symbol}", call)
        time.sleep(self.sleep_seconds)
        df = raw.copy()
        # 腾讯字段 amount 文档标注为“手”，这里转成 volume，并估算成交额，避免把成交量误当成交额。
        if "amount" in df.columns and "volume" not in df.columns:
            df = df.rename(columns={"amount": "volume"})
        if {"volume", "close"}.issubset(df.columns):
            df["amount"] = pd.to_numeric(df["volume"], errors="coerce") * pd.to_numeric(df["close"], errors="coerce") * 100
        return df

    def _stock_hist_sina(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        import akshare as ak
        symbol = market_code_prefix(code)
        def call():
            return ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)
        raw = self._with_retry(f"新浪K线 {symbol}", call)
        time.sleep(self.sleep_seconds)
        return raw

    def _stock_hist_eastmoney(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        import akshare as ak
        def call():
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
                timeout=20,
            )
        raw = self._with_retry(f"东方财富K线 {code}", call)
        time.sleep(self.sleep_seconds)
        return raw

    def index_hist(self, symbol: str) -> pd.DataFrame:
        symbol = str(symbol).strip().lower()
        start_date = self.cfg["data"].get("start_date") or "20220101"
        end_date = self.cfg["data"].get("end_date") or today_yyyymmdd()
        errors: List[str] = []
        for provider in self.index_providers:
            provider = provider.lower().strip()
            cache_name = f"index_{provider}_{symbol}_{start_date}_{end_date}.csv"
            cache_path = self.cache_dir / cache_name
            cached = self._read_cache(cache_path)
            if cached is not None:
                df = normalize_index_hist(cached)
                df.attrs["data_provider"] = provider + "_cache"
                return df
            try:
                if provider in {"tx", "tencent", "qq"}:
                    df = self._index_hist_tencent(symbol, start_date, end_date)
                    provider_name = "tencent"
                elif provider in {"em", "eastmoney", "east"}:
                    df = self._index_hist_eastmoney(symbol, start_date, end_date)
                    provider_name = "eastmoney"
                elif provider in {"legacy", "sina"}:
                    df = self._index_hist_legacy(symbol)
                    provider_name = "legacy"
                else:
                    raise ValueError(f"未知指数K线数据源: {provider}")
                df = normalize_index_hist(df)
                if df.empty:
                    raise ValueError(f"{provider} 指数标准化后为空")
                df.attrs["data_provider"] = provider_name
                self._write_cache(df, cache_path)
                return df
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                continue
        stale = self._fallback_stale_cache(f"index_*_{symbol}_*.csv", normalize_index_hist)
        if stale is not None:
            return stale
        raise RuntimeError("所有指数K线数据源均失败：" + " | ".join(errors))

    def _index_hist_tencent(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        import akshare as ak
        def call():
            return ak.stock_zh_index_daily_tx(symbol=symbol, start_date=start_date, end_date=end_date)
        raw = self._with_retry(f"腾讯指数K线 {symbol}", call)
        time.sleep(self.sleep_seconds)
        if "amount" in raw.columns and "volume" not in raw.columns:
            raw = raw.rename(columns={"amount": "volume"})
        return raw

    def _index_hist_eastmoney(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        import akshare as ak
        def call():
            return ak.stock_zh_index_daily_em(symbol=symbol, start_date=start_date, end_date=end_date)
        raw = self._with_retry(f"东方财富指数K线 {symbol}", call)
        time.sleep(self.sleep_seconds)
        return raw

    def _index_hist_legacy(self, symbol: str) -> pd.DataFrame:
        import akshare as ak
        def call():
            return ak.stock_zh_index_daily(symbol=symbol)
        raw = self._with_retry(f"legacy指数K线 {symbol}", call)
        time.sleep(self.sleep_seconds)
        return raw

    def _read_spot_cache(self, path: Path) -> Optional[pd.DataFrame]:
        minutes = float(self.cfg.get("data", {}).get("spot_cache_minutes", 2))
        if path.exists() and (not self.refresh):
            try:
                age_min = (time.time() - path.stat().st_mtime) / 60.0
                if age_min <= minutes:
                    return pd.read_csv(path, dtype=str)
            except Exception:
                return None
        return None

    def _stock_spot_akshare_em(self) -> pd.DataFrame:
        import akshare as ak
        return self._with_retry("A股实时行情-东财", lambda: ak.stock_zh_a_spot_em())

    def _stock_spot_efinance(self) -> pd.DataFrame:
        import efinance as ef  # optional dependency; requirements.txt 已加入，失败会自动切后续源
        raw = ef.stock.get_realtime_quotes()
        if raw is None or len(raw) == 0:
            raise ValueError("efinance 返回空数据")
        df = raw.copy()
        rename = {
            "股票代码": "代码", "股票名称": "名称", "最新价": "最新价", "涨跌幅": "涨跌幅",
            "今开": "今开", "最高": "最高", "最低": "最低", "昨日收盘": "昨收",
            "成交额": "成交额", "成交量": "成交量", "换手率": "换手率",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "代码" not in df.columns:
            raise ValueError("efinance 缺少代码列")
        df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
        return df

    def _stock_spot_tencent_direct(self) -> pd.DataFrame:
        # 免费兜底：直接请求腾讯 quote 接口。字段格式可能随腾讯变动，因此只作为兜底实时价/涨跌幅源。
        codes = self._active_pool_codes()
        # 如果拿不到股票池，返回空让上层切其他源/缓存。
        if not codes:
            raise ValueError("腾讯兜底源需要股票池代码")
        rows = []
        session = requests.Session()
        for start in range(0, len(codes), 80):
            syms = ",".join(market_code_prefix(c) for c in codes[start:start+80])
            url = "https://qt.gtimg.cn/q=" + syms
            resp = session.get(url, timeout=8)
            resp.encoding = "gbk"
            text = resp.text or ""
            for item in text.split(';'):
                if '="' not in item:
                    continue
                data = item.split('="', 1)[1].strip().strip('"')
                parts = data.split('~')
                if len(parts) < 10:
                    continue
                code = normalize_code(parts[2])
                def f(idx, default=np.nan):
                    try:
                        return float(parts[idx])
                    except Exception:
                        return default
                price = f(3); prev = f(4); openp = f(5)
                pct = (price / prev - 1) * 100 if price > 0 and prev > 0 else np.nan
                # Tencent 常见字段：31 涨跌额，32 涨跌幅，33 最高，34 最低，37 成交量/额附近；做宽松保护。
                if len(parts) > 32:
                    cand = f(32)
                    if np.isfinite(cand) and abs(cand) < 40:
                        pct = cand
                high = f(33, max(price, openp) if price > 0 else np.nan) if len(parts) > 33 else np.nan
                low = f(34, min(price, openp) if price > 0 else np.nan) if len(parts) > 34 else np.nan
                amount = np.nan
                for idx in [37, 38, 44, 45]:
                    if len(parts) > idx:
                        val = f(idx)
                        if np.isfinite(val) and val > 0:
                            amount = val * (10000 if val < 1e7 else 1)
                            break
                rows.append({"代码": code, "名称": parts[1], "最新价": price, "涨跌幅": pct, "今开": openp, "昨收": prev, "最高": high, "最低": low, "成交额": amount, "成交量": np.nan, "换手率": np.nan})
            time.sleep(0.08)
        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError("腾讯实时兜底返回空数据")
        return df

    def _active_pool_codes(self) -> List[str]:
        if self._active_pool_codes_cache is not None:
            return list(self._active_pool_codes_cache)
        pool_path = self.cfg.get("_active_pool_path", "stock_pool.csv")
        try:
            pool = read_stock_pool(pool_path)
            codes = [normalize_code(x) for x in pool.get("code", [])]
        except Exception:
            codes = []
        codes = sorted(set([c for c in codes if c]))
        self._active_pool_codes_cache = codes
        return list(codes)

    def _pool_spot_cache_path(self) -> Optional[Path]:
        codes = self._active_pool_codes()
        if not codes:
            return None
        digest = hashlib.md5(",".join(codes).encode("utf-8")).hexdigest()[:12]
        return self.cache_dir / f"spot_pool_{digest}.csv"

    def stock_spot_all(self) -> pd.DataFrame:
        if self._spot_cache is not None:
            return self._spot_cache.copy()
        providers = provider_list(self.cfg, "spot_providers", ["akshare_em", "efinance", "tencent_direct"])
        pool_spot_mode = bool(self.cfg.get("_pool_spot_only", False)) and bool(self.cfg.get("data", {}).get("prefer_pool_spot", True))
        pool_cache_path = self._pool_spot_cache_path() if pool_spot_mode else None
        if pool_spot_mode and any(p in {"tencent", "tencent_direct", "qq"} for p in providers):
            providers = ["tencent_direct"] + [p for p in providers if p not in {"tencent", "tencent_direct", "qq"}]

        full_cache_path = self.cache_dir / "spot_all.csv"
        if not pool_spot_mode:
            cached = self._read_spot_cache(full_cache_path)
            if cached is not None:
                if "代码" in cached.columns:
                    cached["代码"] = cached["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
                self._spot_cache = cached
                return cached.copy()
        elif pool_cache_path is not None:
            cached = self._read_spot_cache(pool_cache_path)
            if cached is not None:
                if "代码" in cached.columns:
                    cached["代码"] = cached["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
                self._spot_cache = cached
                return cached.copy()

        errors: List[str] = []
        for provider in providers:
            provider = provider.lower().strip()
            try:
                if provider in {"akshare", "akshare_em", "eastmoney", "em"}:
                    df = self._stock_spot_akshare_em()
                    cache_path = full_cache_path
                elif provider in {"efinance", "ef"}:
                    df = self._stock_spot_efinance()
                    cache_path = full_cache_path
                elif provider in {"tencent", "tencent_direct", "qq"}:
                    df = self._stock_spot_tencent_direct()
                    cache_path = pool_cache_path or full_cache_path
                else:
                    continue
                if df is None or df.empty or "代码" not in df.columns:
                    raise ValueError(f"{provider} 实时行情为空或缺少代码列")
                df = df.copy()
                df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
                self._write_cache(df, cache_path)
                self._spot_cache = df
                return df.copy()
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                continue
        stale = None
        if pool_spot_mode and pool_cache_path is not None:
            stale = self._fallback_stale_cache(pool_cache_path.name, lambda x: x)
        if stale is None:
            stale = self._fallback_stale_cache("spot_all.csv", lambda x: x)
        if stale is not None:
            self._spot_cache = stale
            return stale.copy()
        raise RuntimeError("所有实时行情数据源均失败：" + " | ".join(errors))

    def index_spot_all(self) -> pd.DataFrame:
        if self._index_spot_cache is not None:
            return self._index_spot_cache.copy()
        cache_path = self.cache_dir / "index_spot_all.csv"
        cached = self._read_cache(cache_path)
        if cached is not None:
            self._index_spot_cache = cached
            return cached.copy()

        import akshare as ak

        df = self._with_retry("指数实时行情", lambda: ak.stock_zh_index_spot_sina())
        self._write_cache(df, cache_path)
        self._index_spot_cache = df
        return df.copy()

    def board_names(self, kind: str) -> pd.DataFrame:
        """获取行业/概念板块列表，并做缓存。concept=新浪概念+同花顺资金，industry=东方财富，sw=申万。"""
        kind = normalize_board_source(kind)
        if kind not in {"industry", "concept", "em_concept", "sw"}:
            raise ValueError(f"未知板块类型: {kind}")
        if kind in self._board_names_cache:
            return self._board_names_cache[kind].copy()
        cache_path = self.cache_dir / f"board_{kind}_names.csv"
        cached = self._read_cache(cache_path)
        if cached is not None:
            self._board_names_cache[kind] = cached.copy()
            return cached.copy()
        import akshare as ak
        try:
            if kind == "concept":
                spot = self._with_retry("新浪概念板块列表", lambda: ak.stock_sector_spot(indicator="概念"))
                df = pd.DataFrame({
                    "板块名称": spot.get("板块", ""),
                    "板块代码": spot.get("label", ""),
                    "涨跌幅": pd.to_numeric(spot.get("涨跌幅"), errors="coerce"),
                    "公司家数": pd.to_numeric(spot.get("公司家数"), errors="coerce"),
                    "总成交额": pd.to_numeric(spot.get("总成交额"), errors="coerce"),
                    "领涨股票": spot.get("股票名称", ""),
                    "领涨股票-涨跌幅": pd.to_numeric(spot.get("个股-涨跌幅"), errors="coerce"),
                })
                try:
                    flow = self._with_retry("同花顺概念资金流", lambda: ak.stock_fund_flow_concept(symbol="即时"))
                    flow = flow.rename(columns={"行业": "板块名称", "行业-涨跌幅": "同花顺涨跌幅", "净额": "概念净额"})
                    keep = [c for c in ["板块名称", "同花顺涨跌幅", "概念净额"] if c in flow.columns]
                    if keep:
                        df = df.merge(flow[keep], on="板块名称", how="left")
                        ths_pct = pd.to_numeric(df.get("同花顺涨跌幅"), errors="coerce")
                        df["涨跌幅"] = ths_pct.fillna(pd.to_numeric(df["涨跌幅"], errors="coerce"))
                        if "概念净额" in df.columns:
                            df["净额"] = pd.to_numeric(df["概念净额"], errors="coerce")
                except Exception:
                    pass
                df["换手率"] = 0.0
            elif kind == "sw":
                raw = self._with_retry("申万一级行业指数列表", lambda: ak.index_realtime_sw(symbol="一级行业"))
                df = raw.copy()
                df["板块名称"] = df.get("指数名称", "")
                df["板块代码"] = df.get("指数代码", "")
                latest = pd.to_numeric(df.get("最新价"), errors="coerce")
                prev = pd.to_numeric(df.get("昨收盘"), errors="coerce")
                df["涨跌幅"] = (latest / prev - 1.0) * 100.0
                if "成交额" in df.columns:
                    df["换手率"] = 0.0
            elif kind == "industry":
                df = self._with_retry("东方财富行业板块列表", lambda: ak.stock_board_industry_name_em())
            else:
                df = self._with_retry("东方财富概念板块列表", lambda: ak.stock_board_concept_name_em())
        except Exception:
            cached = self._fallback_stale_cache(f"board_{kind}_names.csv", lambda x: x)
            if cached is not None:
                self._board_names_cache[kind] = cached.copy()
                return cached.copy()
            raise
        self._write_cache(df, cache_path)
        time.sleep(self.sleep_seconds)
        self._board_names_cache[kind] = df.copy()
        return df.copy()

    def board_cons(self, kind: str, symbol: str, prefer_stale: bool = False) -> pd.DataFrame:
        """获取行业/概念板块成份股，并做缓存。symbol 可为板块名称、概念 label 或行业代码。"""
        kind = normalize_board_source(kind)
        if kind not in {"industry", "concept", "em_concept", "sw"}:
            raise ValueError(f"未知板块类型: {kind}")
        safe_symbol = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(symbol))[:80]
        cache_path = self.cache_dir / f"board_{kind}_cons_{safe_symbol}.csv"
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached.copy()
        if prefer_stale:
            cached = self._fallback_stale_cache(f"board_{kind}_cons_{safe_symbol}.csv", lambda x: x)
            if cached is not None:
                return cached.copy()
            raise ValueError(f"无可用旧缓存：{kind} 板块成份 {symbol}")
        import akshare as ak
        try:
            if kind == "concept":
                concept_symbol = str(symbol).strip()
                if not concept_symbol.startswith("gn_"):
                    names = self.board_names("concept")
                    cols = {str(c).strip(): c for c in names.columns}
                    name_col = cols.get("板块名称") or cols.get("名称")
                    code_col = cols.get("板块代码") or cols.get("代码") or cols.get("label")
                    if name_col is not None and code_col is not None:
                        m = names[names[name_col].astype(str).eq(concept_symbol)]
                        if not m.empty:
                            concept_symbol = str(m.iloc[0][code_col]).strip()
                df = self._with_retry(f"新浪概念成份 {symbol}", lambda: ak.stock_sector_detail(sector=concept_symbol))
            elif kind == "sw":
                sw_symbol = str(symbol).strip()
                if not re.fullmatch(r"\d{6}", sw_symbol):
                    names = self.board_names("sw")
                    cols = {str(c).strip(): c for c in names.columns}
                    name_col = cols.get("板块名称") or cols.get("指数名称")
                    code_col = cols.get("板块代码") or cols.get("指数代码")
                    if name_col is not None and code_col is not None:
                        m = names[names[name_col].astype(str).eq(sw_symbol)]
                        if not m.empty:
                            sw_symbol = str(m.iloc[0][code_col]).strip()
                df = self._with_retry(f"申万行业成份 {symbol}", lambda: ak.index_component_sw(symbol=sw_symbol))
            elif kind == "industry":
                df = self._with_retry(f"行业板块成份 {symbol}", lambda: ak.stock_board_industry_cons_em(symbol=symbol))
            else:
                df = self._with_retry(f"概念板块成份 {symbol}", lambda: ak.stock_board_concept_cons_em(symbol=symbol))
        except Exception:
            cached = self._fallback_stale_cache(f"board_{kind}_cons_{safe_symbol}.csv", lambda x: x)
            if cached is not None:
                return cached.copy()
            raise
        self._write_cache(df, cache_path)
        time.sleep(self.sleep_seconds)
        return df.copy()

    def board_hist(self, kind: str, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        """获取板块日K，用于 v6.3 主线强度，而不是只用股票池内部均值。"""
        kind = normalize_board_source(kind)
        if kind not in {"industry", "concept", "em_concept", "sw"}:
            raise ValueError(f"未知板块类型: {kind}")
        resolved_symbol = self._resolve_board_hist_symbol(kind, str(symbol))
        cache_symbol = resolved_symbol or str(symbol)
        mem_key = (kind, cache_symbol, start_date, end_date, adjust or "")
        if mem_key in self._board_hist_cache:
            return self._board_hist_cache[mem_key].copy()
        safe_symbol = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", cache_symbol)[:80]
        cache_path = self.cache_dir / f"board_{kind}_hist_{safe_symbol}_{adjust or 'none'}_{start_date}_{end_date}.csv"
        cached = self._read_cache(cache_path)
        if cached is not None:
            df = normalize_index_hist(cached)
            df.attrs["data_provider"] = f"board_{kind}_cache"
            self._board_hist_cache[mem_key] = df.copy()
            return df
        import akshare as ak
        try:
            if kind == "concept":
                raw = self._with_retry(
                    f"同花顺概念日K {resolved_symbol}",
                    lambda: ak.stock_board_concept_index_ths(symbol=resolved_symbol, start_date=start_date, end_date=end_date),
                )
            elif kind == "sw":
                raw = self._with_retry(f"申万行业日K {resolved_symbol}", lambda: ak.index_hist_sw(symbol=resolved_symbol, period="day"))
            elif kind == "industry":
                raw = self._with_retry(
                    f"行业板块日K {resolved_symbol}",
                    lambda: ak.stock_board_industry_hist_em(symbol=resolved_symbol, start_date=start_date, end_date=end_date, period="日k", adjust=adjust),
                )
            else:
                raw = self._with_retry(
                    f"概念板块日K {resolved_symbol}",
                    lambda: ak.stock_board_concept_hist_em(symbol=resolved_symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust),
                )
        except Exception:
            cached = self._fallback_stale_cache(f"board_{kind}_hist_{safe_symbol}_{adjust or 'none'}_{start_date}_*.csv", normalize_index_hist)
            if cached is not None:
                self._board_hist_cache[mem_key] = cached.copy()
                return cached.copy()
            raise
        time.sleep(self.sleep_seconds)
        df = normalize_index_hist(raw)
        df.attrs["data_provider"] = f"board_{kind}"
        self._write_cache(df, cache_path)
        self._board_hist_cache[mem_key] = df.copy()
        return df

    def infer_sector_map(self, target_codes: Iterable[str], kind: str = "industry", max_boards: int = 0) -> Dict[str, str]:
        """
        通过 AkShare 行业/概念板块成份反查股票所属板块。

        industry：通常一只股票只有一个主行业，速度较快，适合作默认分组。
        concept：一只股票可能属于多个概念；这里选择“当前板块强度分”最高的概念作为主板块。
        """
        targets = {normalize_code(x) for x in target_codes if str(x).strip()}
        if not targets:
            return {}
        kind = normalize_board_source(kind)
        names = self.board_names(kind)
        if names.empty:
            return {}
        prefer_stale_cons = bool(names.attrs.get("data_provider") == "stale_cache")
        cols = {str(c).strip(): c for c in names.columns}
        name_col = cols.get("板块名称") or cols.get("名称") or names.columns[0]
        code_col = cols.get("板块代码") or cols.get("代码")

        # 对概念板块：同一股票可能属于多个概念，优先选择当前更强的板块。
        def _num(col: str, default: float = 0.0) -> pd.Series:
            if col in names.columns:
                return pd.to_numeric(names[col], errors="coerce").fillna(default)
            return pd.Series([default] * len(names), index=names.index, dtype=float)
        pct = _num("涨跌幅")
        up = _num("上涨家数")
        down = _num("下跌家数")
        turnover = _num("换手率")
        breadth = up / (up + down).replace(0, np.nan)
        board_score = pct.fillna(0) + 3.0 * breadth.fillna(0.5) + 0.15 * turnover.fillna(0)
        names = names.copy()
        names["_auto_board_score"] = board_score
        names = names.sort_values("_auto_board_score", ascending=False)
        if max_boards and max_boards > 0:
            names = names.head(int(max_boards)).copy()

        best: Dict[str, Tuple[str, float]] = {}
        for _, row in names.iterrows():
            board_name = str(row.get(name_col, "")).strip()
            if not board_name:
                continue
            # 名称更稳；若名称失败再尝试板块代码。
            symbols = [board_name]
            if code_col is not None:
                board_code = str(row.get(code_col, "")).strip()
                if board_code and board_code not in symbols:
                    symbols.append(board_code)
            cons = pd.DataFrame()
            last_exc: Optional[Exception] = None
            for sym in symbols:
                try:
                    cons = self.board_cons(kind, sym, prefer_stale=prefer_stale_cons)
                    if not cons.empty:
                        break
                except Exception as exc:
                    last_exc = exc
                    continue
            if cons.empty:
                if last_exc:
                    continue
                continue
            code_column = None
            for c in ["代码", "股票代码", "证券代码", "code"]:
                if c in cons.columns:
                    code_column = c
                    break
            if code_column is None:
                continue
            score = float(row.get("_auto_board_score", 0.0))
            for raw_code in cons[code_column].dropna().astype(str):
                try:
                    code = normalize_code(raw_code)
                except Exception:
                    continue
                if code not in targets:
                    continue
                # 行业一般直接取；概念则保留当前更强的题材。
                if code not in best or score > best[code][1]:
                    best[code] = (board_name, score)
            if len(best) >= len(targets):
                # industry/sw 一股通常只有一个归属；concept 允许继续扫描以选择更强题材。
                if kind in {"industry", "sw"}:
                    break
        return {code: sector for code, (sector, _) in best.items()}


def normalize_stock_hist(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date",
        "股票代码": "code",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "pct_amount",
        "换手率": "turnover",
    }
    out = df.rename(columns=rename).copy()
    if "date" not in out.columns:
        raise ValueError("历史行情缺少日期列")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ["open", "close", "high", "low", "volume", "amount", "pct_chg", "turnover"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "amount" not in out.columns:
        out["amount"] = np.nan
    if "pct_chg" not in out.columns:
        out["pct_chg"] = out["close"].pct_change() * 100
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return out


def normalize_index_hist(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date",
        "开盘": "open",
        "开盘价": "open",
        "收盘": "close",
        "收盘价": "close",
        "最高": "high",
        "最高价": "high",
        "最低": "low",
        "最低价": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    out = df.rename(columns=rename).copy()
    # 有些缓存读出来列名已经是英文；AkShare 原始指数日线也常见英文。
    if "date" not in out.columns:
        raise ValueError("指数历史行情缺少日期列")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ["open", "close", "high", "low", "volume", "amount"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = np.nan
    if "amount" not in out.columns:
        out["amount"] = np.nan
    out["pct_chg"] = out["close"].pct_change() * 100
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return out


def merge_stock_tail_realtime(hist: pd.DataFrame, spot_all: pd.DataFrame, code: str) -> pd.DataFrame:
    """把实时行情近似合成今日 K 线。仅适合尾盘参考，不适合严肃回测。"""
    if hist.empty or spot_all.empty:
        return hist
    spot = spot_all.copy()
    if "代码" not in spot.columns:
        return hist
    row = spot[spot["代码"].astype(str).str.zfill(6) == normalize_code(code)]
    if row.empty:
        return hist
    r = row.iloc[0]
    def f(col: str) -> float:
        return pd.to_numeric(pd.Series([r.get(col, np.nan)]), errors="coerce").iloc[0]

    latest = f("最新价")
    if not np.isfinite(latest) or latest <= 0:
        return hist
    today = pd.Timestamp(now_cn().date())
    new_row = {
        "date": today,
        "code": normalize_code(code),
        "open": f("今开"),
        "high": f("最高"),
        "low": f("最低"),
        "close": latest,
        "volume": f("成交量"),
        "amount": f("成交额"),
        "pct_chg": f("涨跌幅"),
        "turnover": f("换手率"),
    }
    # 防止上午未开盘/停牌导致 0 值污染
    for k in ["open", "high", "low"]:
        if not np.isfinite(new_row[k]) or new_row[k] <= 0:
            new_row[k] = latest
    out = hist.copy()
    if not out.empty and out.iloc[-1]["date"].date() == today.date():
        out = out.iloc[:-1]
    out = pd.concat([out, pd.DataFrame([new_row])], ignore_index=True)
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    out.attrs.update(getattr(hist, "attrs", {}))
    return out


def merge_index_tail_realtime(hist: pd.DataFrame, index_spot: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if hist.empty or index_spot.empty:
        return hist
    symbol = str(symbol).lower()
    if "代码" not in index_spot.columns:
        return hist
    code_series = index_spot["代码"].astype(str).str.lower()
    row = index_spot[code_series == symbol]
    if row.empty:
        # 新浪有时用不带市场前缀的指数代码，做一个宽松匹配
        digits = re.search(r"\d{6}", symbol)
        if digits:
            row = index_spot[code_series.str.contains(digits.group(0), regex=False)]
    if row.empty:
        return hist
    r = row.iloc[0]
    def f(col: str) -> float:
        return pd.to_numeric(pd.Series([r.get(col, np.nan)]), errors="coerce").iloc[0]

    latest = f("最新价")
    if not np.isfinite(latest) or latest <= 0:
        return hist
    today = pd.Timestamp(now_cn().date())
    new_row = {
        "date": today,
        "open": f("今开"),
        "high": f("最高"),
        "low": f("最低"),
        "close": latest,
        "volume": f("成交量"),
        "amount": f("成交额"),
        "pct_chg": f("涨跌幅"),
    }
    for k in ["open", "high", "low"]:
        if not np.isfinite(new_row[k]) or new_row[k] <= 0:
            new_row[k] = latest
    out = hist.copy()
    if not out.empty and out.iloc[-1]["date"].date() == today.date():
        out = out.iloc[:-1]
    out = pd.concat([out, pd.DataFrame([new_row])], ignore_index=True)
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    out.attrs.update(getattr(hist, "attrs", {}))
    return out


def add_indicators(df: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    out = df.copy().sort_values("date").reset_index(drop=True)
    c = out["close"]
    h = out["high"]
    l = out["low"]
    o = out["open"]
    prev_c = c.shift(1)
    for w in [5, 10, 20, 30, 60, 120, 250]:
        out[f"ma{w}"] = c.rolling(w, min_periods=w).mean()
        out[f"high{w}"] = h.rolling(w, min_periods=w).max()
        out[f"low{w}"] = l.rolling(w, min_periods=w).min()
        out[f"high{w}_prev"] = h.rolling(w, min_periods=w).max().shift(1)
        out[f"low{w}_prev"] = l.rolling(w, min_periods=w).min().shift(1)
        out[f"ret{w}"] = c / c.shift(w) - 1
        rng = out[f"high{w}"] - out[f"low{w}"]
        out[f"close_pos{w}"] = (c - out[f"low{w}"]) / rng.replace(0, np.nan)
        out[f"range{w}_pct"] = rng / c.replace(0, np.nan)
    if "amount" in out.columns:
        out["amount_ma20"] = out["amount"].rolling(20, min_periods=20).mean()
        out["amount_ma60"] = out["amount"].rolling(60, min_periods=60).mean()
        out["amount_ratio20"] = out["amount"] / out["amount_ma20"]
        out["amount_ratio60"] = out["amount"] / out["amount_ma60"]
        out["amount_ma5"] = out["amount"].rolling(5, min_periods=5).mean()
        out["amount_ma10"] = out["amount"].rolling(10, min_periods=10).mean()
        out["amount_dryup20"] = out["amount"].rolling(5, min_periods=5).mean() / out["amount_ma20"]
        out["amount_ratio5"] = out["amount"] / out["amount_ma5"]
        out["amount_ratio10"] = out["amount"] / out["amount_ma10"]
        out["amount_ratio120_rank"] = out["amount"].rolling(120, min_periods=60).rank(pct=True)
    else:
        out["amount_ma5"] = np.nan
        out["amount_ma10"] = np.nan
        out["amount_ma20"] = np.nan
        out["amount_ma60"] = np.nan
        out["amount_ratio5"] = np.nan
        out["amount_ratio10"] = np.nan
        out["amount_ratio20"] = np.nan
        out["amount_ratio60"] = np.nan
        out["amount_dryup20"] = np.nan
        out["amount_ratio120_rank"] = np.nan
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr"] = tr.rolling(atr_period, min_periods=atr_period).mean()
    out["atr_pct"] = out["atr"] / c.replace(0, np.nan)
    out["ma5_slope3"] = out["ma5"] / out["ma5"].shift(3) - 1
    out["ma10_slope5"] = out["ma10"] / out["ma10"].shift(5) - 1
    out["ma20_slope10"] = out["ma20"] / out["ma20"].shift(10) - 1
    out["ma60_slope20"] = out["ma60"] / out["ma60"].shift(20) - 1
    out["ma120_slope40"] = out["ma120"] / out["ma120"].shift(40) - 1
    # 短线收益/回撤用于 A 股风险闸门：连续跌停、高位崩盘、暴涨后踩踏。
    out["ret3"] = c / c.shift(3) - 1
    out["ret6"] = c / c.shift(6) - 1
    out["drawdown10"] = c / out["high10"] - 1
    out["drawdown20"] = c / out["high20"] - 1
    out["drawdown60"] = c / out["high60"] - 1
    out["drawdown120"] = c / out["high120"] - 1
    out["drawdown250"] = c / out["high250"] - 1
    high_low = (h - l).replace(0, np.nan)
    out["upper_shadow_pct"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / c.replace(0, np.nan)
    out["lower_shadow_pct"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / c.replace(0, np.nan)
    out["body_pct"] = (c - o).abs() / c.replace(0, np.nan)
    out["candle_strength"] = (c - o) / high_low
    out["gap_pct"] = o / prev_c - 1
    out["range_contraction_20_60"] = out["range20_pct"] / out["range60_pct"].replace(0, np.nan)

    # 线性回归趋势效率：固定窗口可用闭式公式批量计算，避免逐窗 np.polyfit 的 Python 循环。
    def _rolling_reg_slope_r2(values: pd.Series, window: int) -> Tuple[np.ndarray, np.ndarray]:
        arr = values.to_numpy(dtype=float)
        n = len(arr)
        slopes = np.full(n, np.nan, dtype=float)
        r2s = np.full(n, np.nan, dtype=float)
        if n < window:
            return slopes, r2s

        finite = np.isfinite(arr)
        y = np.where(finite, arr, 0.0)
        x = np.arange(window, dtype=float)
        sum_x = float(x.sum())
        sum_x2 = float((x * x).sum())
        denom = window * sum_x2 - sum_x * sum_x
        if denom == 0:
            return slopes, r2s

        roll_count = pd.Series(finite.astype(float)).rolling(window, min_periods=window).sum().to_numpy()
        sum_y = pd.Series(y).rolling(window, min_periods=window).sum().to_numpy()
        sum_y2 = pd.Series(y * y).rolling(window, min_periods=window).sum().to_numpy()
        sum_xy = np.full(n, np.nan, dtype=float)
        sum_xy[window - 1:] = np.correlate(y, x, mode="valid")

        slope = (window * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / window
        ss_res = (
            sum_y2
            - 2 * slope * sum_xy
            - 2 * intercept * sum_y
            + slope * slope * sum_x2
            + 2 * slope * intercept * sum_x
            + window * intercept * intercept
        )
        ss_tot = sum_y2 - (sum_y * sum_y) / window
        last = arr
        first = np.full(n, np.nan, dtype=float)
        first[window - 1:] = arr[: n - window + 1]
        valid = (roll_count == window) & (first > 0) & np.isfinite(last) & (last != 0)
        slopes[valid] = slope[valid] * window / last[valid]
        r2_calc = np.where(ss_tot > 0, 1.0 - ss_res / ss_tot, 0.0)
        r2s[valid] = np.clip(r2_calc[valid], 0.0, 1.0)
        return slopes, r2s

    for w in [20, 60]:
        slopes, r2s = _rolling_reg_slope_r2(c, w)
        out[f"reg_slope{w}"] = slopes
        out[f"reg_r2_{w}"] = r2s
    return out


def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except Exception:
        return default


def limit_down_threshold_pct(code: str, name: str = "") -> float:
    """粗略识别 A 股跌停阈值。返回百分数阈值，例如 -9.5 表示接近 10% 跌停。

    主板普通股近似 -9.5%；创业板/科创板近似 -19%；ST 近似 -4.8%。
    这里用于风控闸门，不追求精确到最小报价单位，宁可保守拦截。
    """
    code = normalize_code(code)
    up_name = str(name).upper()
    if "ST" in up_name or "*ST" in up_name:
        return -4.8
    if code.startswith(("300", "301", "688")):
        return -19.0
    if code.startswith(("8", "4")):
        return -29.0
    return -9.5


def compute_risk_gate(
    ind: pd.DataFrame,
    code: str,
    name: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """A 股风险闸门：连续跌停/高位崩盘/暴涨踩踏一票否决。"""
    gate_cfg = cfg.get("strategy", {}).get("risk_gate", {})
    if not gate_cfg.get("enabled", True) or ind.empty:
        return {"risk_gate_block": False, "risk_gate_reason": "", "risk_tags": ""}

    last = ind.iloc[-1]
    pct = pd.to_numeric(ind.get("pct_chg", pd.Series(dtype=float)), errors="coerce")
    ld_th = limit_down_threshold_pct(code, name)
    limit_down = pct <= ld_th
    ldc3 = int(limit_down.tail(3).sum()) if len(limit_down) else 0
    ldc6 = int(limit_down.tail(6).sum()) if len(limit_down) else 0
    ldc10 = int(limit_down.tail(10).sum()) if len(limit_down) else 0
    days_since_ld = 999
    if bool(limit_down.any()):
        rev = list(limit_down.iloc[::-1].astype(bool))
        days_since_ld = rev.index(True) if True in rev else 999

    ret3 = safe_float(last.get("ret3"))
    ret5 = safe_float(last.get("ret5"))
    ret10 = safe_float(last.get("ret10"))
    ret20 = safe_float(last.get("ret20"))
    ret30 = safe_float(last.get("ret30")) if "ret30" in ind.columns else np.nan
    ret60 = safe_float(last.get("ret60"))
    dd10 = safe_float(last.get("drawdown10"))
    dd20 = safe_float(last.get("drawdown20"))
    dd60 = safe_float(last.get("drawdown60"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    upper_shadow = safe_float(last.get("upper_shadow_pct"), 0.0)
    close = safe_float(last.get("close"))
    ma10 = safe_float(last.get("ma10"))
    ma20 = safe_float(last.get("ma20"))

    reasons: List[str] = []
    tags: List[str] = []
    if ldc3 >= int(gate_cfg.get("limit_down_count_3_block", 1)):
        reasons.append(f"近3日出现{ldc3}个跌停/近跌停")
    if ldc6 >= int(gate_cfg.get("limit_down_count_6_block", 2)):
        reasons.append(f"近6日出现{ldc6}个跌停/近跌停")
    if ldc10 >= int(gate_cfg.get("limit_down_count_10_block", 3)):
        reasons.append(f"近10日出现{ldc10}个跌停/近跌停")
    if np.isfinite(ret3) and ret3 <= float(gate_cfg.get("ret3_min", -0.12)):
        reasons.append(f"近3日跌幅{ret3:.1%}过大")
    if np.isfinite(ret5) and ret5 <= float(gate_cfg.get("ret5_min", -0.18)):
        reasons.append(f"近5日跌幅{ret5:.1%}过大")
    if np.isfinite(dd10) and dd10 <= float(gate_cfg.get("drawdown10_max", -0.22)):
        reasons.append(f"距10日高点回撤{dd10:.1%}，高位崩盘风险")
    if np.isfinite(dd20) and dd20 <= float(gate_cfg.get("drawdown20_max", -0.30)):
        reasons.append(f"距20日高点回撤{dd20:.1%}，趋势结构破坏")

    climax = False
    if np.isfinite(ret60) and ret60 >= float(gate_cfg.get("climax_ret60", 1.20)) and np.isfinite(dd10) and dd10 <= float(gate_cfg.get("climax_drawdown10", -0.12)):
        climax = True
    if np.isfinite(ret30) and ret30 >= float(gate_cfg.get("climax_ret30", 0.80)) and np.isfinite(dd10) and dd10 <= float(gate_cfg.get("climax_drawdown10", -0.12)):
        climax = True
    if climax:
        reasons.append("短期暴涨后快速回撤，疑似高潮后踩踏")
    if np.isfinite(amount_ratio20) and amount_ratio20 >= float(gate_cfg.get("climax_amount_ratio20", 2.8)) and np.isfinite(ret5) and ret5 < -0.08:
        reasons.append("放巨量下跌，恐慌/出货量风险")
    if upper_shadow >= 0.10 and np.isfinite(amount_ratio20) and amount_ratio20 >= 2.5:
        reasons.append("巨量长上影，冲高回落风险")
    if days_since_ld < int(gate_cfg.get("min_cooling_days_after_limit_down", 5)) and not (np.isfinite(close) and np.isfinite(ma10) and close > ma10 and np.isfinite(ma20) and close > ma20):
        reasons.append(f"跌停后冷却不足{int(gate_cfg.get('min_cooling_days_after_limit_down', 5))}日")

    if ldc6 >= 1:
        tags.append("近期有跌停")
    if np.isfinite(dd10) and dd10 <= -0.15:
        tags.append("短线回撤过深")
    if climax:
        tags.append("暴涨后踩踏")
    if np.isfinite(amount_ratio20) and amount_ratio20 >= 2.8:
        tags.append("量能异常放大")

    return {
        "risk_gate_block": len(reasons) > 0,
        "risk_gate_reason": "；".join(unique_nonempty(reasons)),
        "risk_tags": "；".join(unique_nonempty(tags)),
        "limit_down_count_3": ldc3,
        "limit_down_count_6": ldc6,
        "limit_down_count_10": ldc10,
        "days_since_limit_down": days_since_ld,
        "limit_down_threshold_pct": ld_th,
        "drawdown10": dd10,
        "drawdown20": dd20,
        "drawdown60": dd60,
        "ret3": ret3,
        "ret5": ret5,
        "ret10": ret10,
    }


def evaluate_market(fetcher: AkshareFetcher, cfg: Dict[str, Any]) -> MarketState:
    index_symbols = cfg["data"].get("market_indices") or ["sh000001"]
    if isinstance(index_symbols, str):
        index_symbols = [x.strip() for x in index_symbols.split(",") if x.strip()]
    rows: List[Dict[str, Any]] = []
    latest_dates: List[pd.Timestamp] = []

    index_spot = pd.DataFrame()
    if cfg["data"].get("use_realtime_tail"):
        try:
            index_spot = fetcher.index_spot_all()
        except Exception:
            index_spot = pd.DataFrame()

    for symbol in index_symbols:
        try:
            hist = fetcher.index_hist(symbol)
            provider = hist.attrs.get("data_provider", "")
            data_warning = hist.attrs.get("data_warning", "")
            if cfg["data"].get("use_realtime_tail") and not index_spot.empty:
                hist = merge_index_tail_realtime(hist, index_spot, symbol)
            ind = add_indicators(hist, atr_period=int(cfg["strategy"].get("atr_period", 14)))
            if len(ind) < 160:
                rows.append({"symbol": symbol, "date": "", "close": np.nan, "score": 0, "error": "指数历史K线不足160日", "provider": provider})
                continue
            last = ind.iloc[-1]
            close = safe_float(last["close"])
            ma20 = safe_float(last.get("ma20"))
            ma60 = safe_float(last.get("ma60"))
            ma120 = safe_float(last.get("ma120"))
            ma250 = safe_float(last.get("ma250"))
            ret20 = safe_float(last.get("ret20"))
            ret60 = safe_float(last.get("ret60"))
            ret120 = safe_float(last.get("ret120"))
            dd120 = safe_float(last.get("drawdown120"))
            pos60 = safe_float(last.get("close_pos60"))
            pos120 = safe_float(last.get("close_pos120"))
            reg_slope20 = safe_float(last.get("reg_slope20"))
            reg_r2_20 = safe_float(last.get("reg_r2_20"))
            reg_slope60 = safe_float(last.get("reg_slope60"))
            reg_r2_60 = safe_float(last.get("reg_r2_60"))
            contraction = safe_float(last.get("range_contraction_20_60"))
            amount_ratio20 = safe_float(last.get("amount_ratio20"))
            amount_dryup20 = safe_float(last.get("amount_dryup20"))
            candle_strength = safe_float(last.get("candle_strength"))
            upper_shadow = safe_float(last.get("upper_shadow_pct"), 0.0)
            atr_pct = safe_float(last.get("atr_pct"))

            trend = 0.0
            trend += 8 if close > ma20 else 0
            trend += 10 if close > ma60 else 0
            trend += 7 if close > ma120 else 0
            trend += 5 if close > ma250 else 0
            trend += 8 if ma20 > ma60 > ma120 else 0
            trend += 6 if safe_float(last.get("ma20_slope10")) > 0 else 0
            trend += 4 if safe_float(last.get("ma60_slope20")) > 0 else 0
            trend += 5 if reg_slope20 > 0 and reg_r2_20 >= 0.25 else 0
            trend = min(trend, 53)

            momentum = 0.0
            momentum += 6 if ret20 > 0 else 0
            momentum += 7 if ret60 > 0 else 0
            momentum += 5 if ret120 > 0 else 0
            momentum += 5 if reg_slope60 > 0 and reg_r2_60 >= 0.20 else 0
            momentum += 4 if pos60 >= 0.65 else 0
            momentum = min(momentum, 27)

            structure = 0.0
            structure += 5 if np.isfinite(dd120) and dd120 > -0.08 else 0
            structure += 4 if np.isfinite(pos120) and pos120 >= 0.55 else 0
            structure += 4 if np.isfinite(contraction) and 0.35 <= contraction <= 0.85 else 0
            # 大盘也看量能：缩量整理/温和放量更健康，巨量冲高回落扣分。
            if np.isfinite(amount_ratio20):
                if 0.75 <= amount_ratio20 <= 1.35:
                    structure += 4
                elif 1.35 < amount_ratio20 <= 2.10 and candle_strength > 0:
                    structure += 3
                elif amount_ratio20 > 2.30 and candle_strength < 0:
                    structure -= 5
            if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.85 and np.isfinite(contraction) and contraction <= 0.90:
                structure += 2
            structure += 3 if np.isfinite(atr_pct) and 0.006 <= atr_pct <= 0.035 else 0
            structure += 3 if candle_strength > 0.15 else 0
            structure += 0 if upper_shadow <= 0.025 else -4
            structure = max(0.0, min(structure, 24))

            score = trend + momentum + structure
            notes: List[str] = []
            if close > ma20 > ma60 and ma60 > ma120:
                notes.append("均线多头")
            elif close > ma60:
                notes.append("中期趋势尚可")
            else:
                notes.append("指数未站稳中期均线")
            if reg_slope20 > 0 and reg_r2_20 >= 0.25:
                notes.append("20日回归趋势向上")
            if np.isfinite(contraction) and contraction <= 0.85:
                notes.append("波动收敛")
            if np.isfinite(amount_ratio20):
                if 0.75 <= amount_ratio20 <= 1.35:
                    notes.append("量能平稳")
                elif 1.35 < amount_ratio20 <= 2.10 and candle_strength > 0:
                    notes.append("温和放量上攻")
                elif amount_ratio20 > 2.30 and candle_strength < 0:
                    notes.append("放量下跌风险")
            if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.85 and np.isfinite(contraction) and contraction <= 0.90:
                notes.append("缩量整理")
            if np.isfinite(dd120) and dd120 <= -0.12:
                notes.append("距阶段高点较远")
            if upper_shadow > 0.025:
                notes.append("上影线偏长")

            rows.append({
                "symbol": symbol,
                "date": last["date"].strftime("%Y-%m-%d"),
                "close": close,
                "score": round(score, 2),
                "trend_score": round(trend, 2),
                "momentum_score": round(momentum, 2),
                "structure_score": round(structure, 2),
                "ret20": ret20,
                "ret60": ret60,
                "ret120": ret120,
                "close_gt_ma20": bool(close > ma20),
                "close_gt_ma60": bool(close > ma60),
                "close_gt_ma120": bool(close > ma120),
                "ma20_gt_ma60": bool(ma20 > ma60),
                "ma60_gt_ma120": bool(ma60 > ma120),
                "ma20_slope10": safe_float(last.get("ma20_slope10")),
                "ma60_slope20": safe_float(last.get("ma60_slope20")),
                "drawdown120": dd120,
                "close_pos60": pos60,
                "close_pos120": pos120,
                "range_contraction_20_60": contraction,
                "amount_ratio20": amount_ratio20,
                "amount_dryup20": amount_dryup20,
                "reg_slope20": reg_slope20,
                "reg_r2_20": reg_r2_20,
                "reg_slope60": reg_slope60,
                "reg_r2_60": reg_r2_60,
                "atr_pct": atr_pct,
                "upper_shadow_pct": upper_shadow,
                "candle_strength": candle_strength,
                "chart_notes": "；".join(unique_nonempty(notes)),
                "provider": provider,
                "data_warning": data_warning,
            })
            latest_dates.append(last["date"])
        except Exception as exc:
            rows.append({"symbol": symbol, "date": "", "close": np.nan, "score": 0, "error": str(exc)})

    details = pd.DataFrame(rows)
    if details.empty or details["score"].replace([np.inf, -np.inf], np.nan).dropna().empty:
        return MarketState(
            date=today_yyyymmdd(),
            score=0,
            regime="weak",
            target_exposure=0,
            details=details,
            summary="大盘数据不足，禁止新开仓",
            market_ret20=0,
            market_ret60=0,
        )

    valid = details[pd.to_numeric(details.get("close"), errors="coerce").notna()].copy()
    if valid.empty:
        valid = details.copy()
    avg_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).mean())
    min_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).min())
    market_ret20 = float(pd.to_numeric(valid.get("ret20"), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if "ret20" in valid else 0.0
    market_ret60 = float(pd.to_numeric(valid.get("ret60"), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().mean()) if "ret60" in valid else 0.0

    st_cfg = cfg["strategy"]
    # 新的大盘判定看图形结构，而不是只看“站上某条线”。
    if avg_score >= 76 and min_score >= 52:
        regime = "strong"
        exposure = float(st_cfg.get("total_exposure_strong", 0.8))
        structure_text = "强势多头"
    elif avg_score >= 58 and min_score >= 38:
        regime = "neutral"
        exposure = float(st_cfg.get("total_exposure_neutral", 0.45))
        structure_text = "震荡偏强/结构修复"
    elif avg_score >= 45:
        regime = "cautious"
        exposure = min(float(st_cfg.get("total_exposure_neutral", 0.45)), 0.25)
        structure_text = "弱修复/谨慎试错"
    else:
        regime = "weak"
        exposure = float(st_cfg.get("total_exposure_weak", 0.0))
        structure_text = "弱势防守"

    date = max(latest_dates).strftime("%Y-%m-%d") if latest_dates else today_yyyymmdd()
    note_counts: Dict[str, int] = {}
    for notes in valid.get("chart_notes", pd.Series(dtype=str)).astype(str):
        for item in split_reason_text(notes):
            note_counts[item] = note_counts.get(item, 0) + 1
    note_text = "；".join([k for k, _ in sorted(note_counts.items(), key=lambda x: (-x[1], x[0]))[:4]])
    summary = f"大盘状态={regime}，盘面结构={structure_text}，图形分={avg_score:.1f}，建议总权益仓位={exposure:.0%}"
    if note_text:
        summary += f"，盘面特征：{note_text}"
    return MarketState(date, avg_score, regime, exposure, details, summary, market_ret20, market_ret60)



def load_sector_map(pool_path: str | Path, cfg: Dict[str, Any]) -> Dict[str, str]:
    """读取可选 sector_map.csv。格式支持 code,sector 或 股票代码,板块。"""
    st = cfg.get("strategy", {}).get("sector", {})
    path = Path(st.get("sector_map_path", "sector_map.csv"))
    pool_parent = Path(pool_path).parent if pool_path else Path(".")
    if not path.is_absolute():
        path = pool_parent / path
    if not path.exists():
        return {}
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, encoding="gbk")
    except Exception:
        return {}
    if df.empty:
        return {}
    cols = {str(c).strip().lower(): c for c in df.columns}
    code_col = next((cols[x] for x in ["code", "symbol", "股票代码", "证券代码", "代码"] if x in cols), df.columns[0])
    sector_col = next((cols[x] for x in ["sector", "industry", "板块", "行业", "概念", "所属板块", "所属行业"] if x in cols), df.columns[1] if len(df.columns) > 1 else df.columns[0])
    mp: Dict[str, str] = {}
    for _, r in df.iterrows():
        try:
            code = normalize_code(r.get(code_col, ""))
        except Exception:
            continue
        sector = normalize_sector_value(r.get(sector_col, ""))
        if sector:
            mp[code] = sector
    return mp


def load_auto_sector_map(path: str | Path) -> Dict[str, str]:
    """读取自动生成的 code->sector 缓存表。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(p, dtype=str, encoding="gbk")
    except Exception:
        return {}
    if df.empty:
        return {}
    cols = {str(c).strip().lower(): c for c in df.columns}
    code_col = next((cols[x] for x in ["code", "symbol", "股票代码", "证券代码", "代码"] if x in cols), df.columns[0])
    sector_col = next((cols[x] for x in ["sector", "industry", "板块", "行业", "概念", "所属板块", "所属行业"] if x in cols), df.columns[1] if len(df.columns) > 1 else df.columns[0])
    out: Dict[str, str] = {}
    for _, r in df.iterrows():
        try:
            code = normalize_code(r.get(code_col, ""))
        except Exception:
            continue
        sector = normalize_sector_value(r.get(sector_col, ""))
        if sector:
            out[code] = sector
    return out


def write_auto_sector_map(mp: Dict[str, str], path: str | Path) -> None:
    """写入自动生成的 code->sector 缓存表。"""
    if not mp:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"code": code, "sector": sector} for code, sector in sorted(mp.items()) if sector]
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")


def enrich_pool_sectors(
    pool: pd.DataFrame,
    pool_path: str | Path,
    cfg: Dict[str, Any],
    fetcher: Optional[AkshareFetcher] = None,
) -> pd.DataFrame:
    """
    给股票池补充 sector。

    优先级：
    1) 手工 sector_map.csv（如果是概念模式，会覆盖股票池里的历史 sector）；
    2) 新鲜的自动映射缓存；
    3) AkShare 概念板块成份股反查；
    4) 概念缺失时按配置用行业/申万托底；
    5) 仍失败则标记为“未分组”。
    """
    out = pool.copy()
    if "sector" not in out.columns:
        out["sector"] = ""
    sector_cfg = cfg.get("strategy", {}).get("sector", {})
    source = normalize_board_source(sector_cfg.get("auto_source", "industry"))
    out["sector"] = out["sector"].apply(normalize_sector_value)
    if source in {"concept", "concept_first"}:
        # 概念归属随题材强弱变化，不把股票池里历史写回的 sector 当成固定事实。
        out["sector"] = ""

    manual_mp = load_sector_map(pool_path, cfg)
    if manual_mp:
        out["sector"] = out.apply(
            lambda r: normalize_sector_value(r.get("sector", "")) or manual_mp.get(str(r.get("code", "")), ""),
            axis=1,
        )

    auto_path = Path(sector_cfg.get("auto_map_path", "cache/auto_sector_map.csv"))
    if not auto_path.is_absolute():
        auto_path = Path(pool_path).parent / auto_path
    cache_hours = float(cfg.get("data", {}).get("cache_hours", 24))
    auto_map_is_fresh = (source not in {"concept", "concept_first"}) or is_cache_fresh(auto_path, cache_hours)
    auto_mp = load_auto_sector_map(auto_path) if auto_map_is_fresh else {}
    if auto_mp:
        out["sector"] = out.apply(
            lambda r: normalize_sector_value(r.get("sector", "")) or auto_mp.get(str(r.get("code", "")), ""),
            axis=1,
        )

    auto_fill = bool(sector_cfg.get("auto_fill", True)) and source not in {"", "none", "off", "false"}
    missing_codes = [str(c) for c in out.loc[out["sector"].apply(normalize_sector_value) == "", "code"].tolist()]
    if auto_fill and fetcher is not None and missing_codes:
        max_boards = int(sector_cfg.get("auto_scan_max_boards", 0) or 0)
        try:
            if source == "concept_first":
                fallback_sources = sector_cfg.get("auto_fallback_sources", ["industry", "sw"])
                if isinstance(fallback_sources, str):
                    fallback_sources = [x.strip() for x in fallback_sources.split(",") if x.strip()]
                sources = ["concept"] + [normalize_board_source(x) for x in fallback_sources]
            else:
                sources = [source]
            inferred: Dict[str, str] = {}
            remaining = [normalize_code(c) for c in missing_codes]
            for src in sources:
                if not remaining:
                    break
                try:
                    part = fetcher.infer_sector_map(remaining, kind=src, max_boards=max_boards)
                except Exception:
                    part = {}
                if not part:
                    continue
                inferred.update({code: sector for code, sector in part.items() if sector})
                remaining = [code for code in remaining if code not in inferred]
            if inferred:
                auto_mp.update(inferred)
                write_auto_sector_map(auto_mp, auto_path)
                out["sector"] = out.apply(
                    lambda r: normalize_sector_value(r.get("sector", "")) or inferred.get(str(r.get("code", "")), ""),
                    axis=1,
                )
        except Exception as exc:
            out.attrs["sector_auto_error"] = str(exc)

    out["sector"] = out["sector"].apply(normalize_sector_value).replace("", "未分组")

    if bool(sector_cfg.get("auto_write_back", True)) and source not in {"concept", "concept_first"}:
        try:
            # 只把真实板块写回；未分组保持为空，避免占位值阻止后续自动补全。
            orig = read_stock_pool_or_empty(pool_path)
            writeback = out[["code", "name", "sector"]].copy()
            writeback["sector"] = writeback["sector"].apply(normalize_sector_value)
            orig_sector = orig["sector"].apply(normalize_sector_value) if "sector" in orig.columns else pd.Series([""] * len(orig))
            if "sector" not in orig.columns or not orig_sector.reset_index(drop=True).equals(writeback["sector"].reset_index(drop=True)):
                write_stock_pool(writeback, pool_path)
        except Exception:
            pass
    return out


def detect_volume_anchor_setup(ind: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """识别“天量锚点后缩量再异动”。这是资金事件模型，不等同普通突破。"""
    setup_cfg = cfg.get("strategy", {}).get("setup", {})
    if ind is None or ind.empty or len(ind) < 80:
        return {"anchor_valid": False, "anchor_reason": "K线不足", "anchor_score": 0.0}
    lookback = int(setup_cfg.get("volume_anchor_lookback", 20))
    min_days = int(setup_cfg.get("volume_anchor_min_days_ago", 3))
    max_days = int(setup_cfg.get("volume_anchor_max_days_ago", 20))
    min_ratio = float(setup_cfg.get("volume_anchor_amount_ratio", 3.0))
    min_pct = float(setup_cfg.get("volume_anchor_min_pct", 7.0))
    n = len(ind)
    candidates: List[Tuple[int, float]] = []
    for i in range(max(0, n - max_days - 1), max(0, n - min_days)):
        row = ind.iloc[i]
        pct = safe_float(row.get("pct_chg"))
        ar = safe_float(row.get("amount_ratio20"))
        rank120 = safe_float(row.get("amount_ratio120_rank"))
        cs = safe_float(row.get("candle_strength"))
        if (np.isfinite(pct) and pct >= min_pct) and (np.isfinite(ar) and ar >= min_ratio or np.isfinite(rank120) and rank120 >= 0.95) and cs > 0.35:
            # 越近、量越大、K线越强，越优先作为锚点
            candidates.append((i, (ar if np.isfinite(ar) else 2.0) + (rank120 if np.isfinite(rank120) else 0.0)))
    if not candidates:
        return {"anchor_valid": False, "anchor_reason": "近20日无天量涨停/大阳锚点", "anchor_score": 0.0}
    anchor_i = sorted(candidates, key=lambda x: x[1], reverse=True)[0][0]
    anchor = ind.iloc[anchor_i]
    last = ind.iloc[-1]
    after = ind.iloc[anchor_i + 1:]
    if after.empty:
        return {"anchor_valid": False, "anchor_reason": "锚点后观察期不足", "anchor_score": 0.0}
    a_low = safe_float(anchor.get("low"))
    a_high = safe_float(anchor.get("high"))
    a_open = safe_float(anchor.get("open"))
    a_close = safe_float(anchor.get("close"))
    a_mid = min(a_open, a_close) + (max(a_open, a_close) - min(a_open, a_close)) * float(setup_cfg.get("volume_anchor_midline_pct", 0.50))
    hold_low_pct = float(setup_cfg.get("volume_anchor_hold_low_pct", -0.03))
    min_after_low = safe_float(after["low"].min())
    close = safe_float(last.get("close"))
    ma5 = safe_float(last.get("ma5"))
    ma10 = safe_float(last.get("ma10"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    amount_dryup20 = safe_float(last.get("amount_dryup20"))
    high_since_anchor = safe_float(after.iloc[:-1]["high"].max()) if len(after) > 1 else np.nan
    reasons: List[str] = []
    blockers: List[str] = []
    score = 0.0
    days_ago = n - 1 - anchor_i
    if np.isfinite(min_after_low) and np.isfinite(a_low) and min_after_low >= a_low * (1.0 + hold_low_pct):
        score += 18; reasons.append("未有效跌破天量锚点低点")
    else:
        blockers.append("跌破天量锚点低点，锚点资金疑似失效")
    if np.isfinite(close) and np.isfinite(a_mid) and close >= a_mid:
        score += 10; reasons.append("收盘守住锚点实体半分位")
    else:
        blockers.append("未守住锚点实体半分位")
    if np.isfinite(amount_dryup20) and amount_dryup20 <= 0.90:
        score += 12; reasons.append("锚点后缩量调整")
    if np.isfinite(amount_ratio20) and amount_ratio20 >= float(setup_cfg.get("reaccum_amount_min", 1.25)):
        score += 15; reasons.append("今日重新放量异动")
    else:
        blockers.append("今日尚未重新放量")
    if np.isfinite(high_since_anchor) and np.isfinite(close) and close >= high_since_anchor * 0.995:
        score += 18; reasons.append("突破锚点后整理区间高点")
    elif np.isfinite(a_high) and np.isfinite(close) and close >= a_high * 0.985:
        score += 10; reasons.append("接近天量锚点高点")
    if np.isfinite(close) and ((np.isfinite(ma5) and close > ma5) or (np.isfinite(ma10) and close > ma10)):
        score += 12; reasons.append("重新站上5/10日线")
    else:
        blockers.append("未重新站上5/10日线")
    if 3 <= days_ago <= 20:
        score += 5
    valid = score >= float(setup_cfg.get("anchor_score_threshold", 72.0)) and len([b for b in blockers if "跌破" in b or "未重新" in b]) == 0
    return {
        "anchor_valid": bool(valid),
        "anchor_score": round(min(score, 100.0), 2),
        "anchor_date": anchor.get("date").strftime("%Y-%m-%d") if hasattr(anchor.get("date"), "strftime") else str(anchor.get("date", "")),
        "anchor_days_ago": days_ago,
        "anchor_reason": "；".join(unique_nonempty(reasons)),
        "anchor_blockers": "；".join(unique_nonempty(blockers)),
        "anchor_low": a_low,
        "anchor_high": a_high,
    }


def compute_sector_context(metrics: pd.DataFrame, cfg: Dict[str, Any], fetcher: Optional[AkshareFetcher] = None) -> pd.DataFrame:
    """v6.3 板块主线：优先用对应板块指数日K计算主线强度；失败时回退到股票池内部均值。"""
    if metrics is None or metrics.empty:
        return metrics
    df = metrics.copy()
    if "sector" not in df.columns:
        df["sector"] = "未分组"
    df["sector"] = df["sector"].apply(normalize_sector_value).replace("", "未分组")
    for c in ["ret5", "ret10", "ret20", "ret60", "ret120", "amount_ratio20", "close_pos60", "pct_chg"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    sector_cfg = cfg.get("strategy", {}).get("sector", {})
    g = df.groupby("sector", dropna=False)
    pool_sector = pd.DataFrame({
        "sector": list(g.groups.keys()),
        "sector_size": g["code"].count().values,
        "pool_sector_ret20": g["ret20"].mean().values,
        "pool_sector_ret60": g["ret60"].mean().values,
        "pool_sector_ret5": g["ret5"].mean().values,
        "sector_up_rate20": g["ret20"].apply(lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())).values,
        "sector_high_pos_rate": g["close_pos60"].apply(lambda x: float((pd.to_numeric(x, errors="coerce") >= 0.65).mean())).values,
        "pool_sector_amount_ratio20": g["amount_ratio20"].median().values,
    })

    board_rows: List[Dict[str, Any]] = []
    use_board = bool(sector_cfg.get("use_board_hist_strength", True)) and fetcher is not None
    source_kind = normalize_board_source(sector_cfg.get("auto_source", "industry") or "industry")
    if source_kind == "concept_first":
        fallback_sources = sector_cfg.get("auto_fallback_sources", ["industry", "sw"])
        if isinstance(fallback_sources, str):
            fallback_sources = [x.strip() for x in fallback_sources.split(",") if x.strip()]
        source_candidates = ["concept"] + [normalize_board_source(x) for x in fallback_sources]
    else:
        source_candidates = [source_kind]
    source_candidates = [x for x in source_candidates if x in {"concept", "industry", "em_concept", "sw"}]
    if not source_candidates:
        source_kind = "industry"
        source_candidates = ["industry"]
    if use_board:
        start_date = cfg.get("data", {}).get("start_date") or "20220101"
        end_date = cfg.get("data", {}).get("end_date") or today_yyyymmdd()
        adjust = ""
        for sec in pool_sector["sector"].astype(str).tolist():
            if not sec or sec == "未分组":
                continue
            try:
                hist = pd.DataFrame()
                hist_kind = ""
                last_exc: Optional[Exception] = None
                for candidate_kind in source_candidates:
                    try:
                        hist = fetcher.board_hist(candidate_kind, sec, start_date, end_date, adjust=adjust)
                        hist_kind = candidate_kind
                        break
                    except Exception as exc:
                        last_exc = exc
                        continue
                if hist.empty:
                    if last_exc:
                        raise last_exc
                    raise ValueError("板块日K为空")
                ind = add_indicators(hist, atr_period=int(cfg.get("strategy", {}).get("atr_period", 14)))
                if len(ind) < 80:
                    raise ValueError("板块日K不足")
                last = ind.iloc[-1]
                close = safe_float(last.get("close"))
                ma5 = safe_float(last.get("ma5")); ma10 = safe_float(last.get("ma10")); ma20 = safe_float(last.get("ma20")); ma60 = safe_float(last.get("ma60")); ma120 = safe_float(last.get("ma120"))
                board_rows.append({
                    "sector": sec,
                    "board_ret5": safe_float(last.get("ret5")),
                    "board_ret10": safe_float(last.get("ret10")),
                    "board_ret20": safe_float(last.get("ret20")),
                    "board_ret60": safe_float(last.get("ret60")),
                    "board_ret120": safe_float(last.get("ret120")),
                    "board_close_pos60": safe_float(last.get("close_pos60")),
                    "board_close_pos120": safe_float(last.get("close_pos120")),
                    "board_amount_ratio20": safe_float(last.get("amount_ratio20")),
                    "board_amount_dryup20": safe_float(last.get("amount_dryup20")),
                    "board_reg_slope20": safe_float(last.get("reg_slope20")),
                    "board_reg_r2_20": safe_float(last.get("reg_r2_20")),
                    "board_ma5_gt10_gt20": bool(np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and close > ma5 >= ma10 >= ma20),
                    "board_ma20_gt60": bool(np.isfinite(ma20) and np.isfinite(ma60) and close > ma20 and ma20 > ma60),
                    "board_ma60_gt120": bool(np.isfinite(ma60) and np.isfinite(ma120) and ma60 > ma120),
                    "sector_strength_source": f"{hist_kind}_board_hist",
                })
            except Exception:
                continue

    board = pd.DataFrame(board_rows)
    sector = pool_sector.merge(board, on="sector", how="left") if not board.empty else pool_sector.copy()
    for c in ["board_ret5", "board_ret10", "board_ret20", "board_ret60", "board_ret120", "board_close_pos60", "board_close_pos120", "board_amount_ratio20", "board_reg_slope20", "board_reg_r2_20"]:
        if c not in sector.columns:
            sector[c] = np.nan
        sector[c] = pd.to_numeric(sector[c], errors="coerce")

    sector["sector_ret5"] = sector["board_ret5"].fillna(sector["pool_sector_ret5"])
    sector["sector_ret20"] = sector["board_ret20"].fillna(sector["pool_sector_ret20"])
    sector["sector_ret60"] = sector["board_ret60"].fillna(sector["pool_sector_ret60"])
    sector["sector_amount_ratio20"] = sector["board_amount_ratio20"].fillna(sector["pool_sector_amount_ratio20"])
    if "sector_strength_source" not in sector.columns:
        sector["sector_strength_source"] = "pool_internal"
    sector["sector_strength_source"] = sector["sector_strength_source"].fillna("pool_internal")

    sector["sector_rs_rank5"] = pd.to_numeric(sector["sector_ret5"], errors="coerce").rank(pct=True).fillna(0.0)
    sector["sector_rs_rank20"] = pd.to_numeric(sector["sector_ret20"], errors="coerce").rank(pct=True).fillna(0.0)
    sector["sector_rs_rank60"] = pd.to_numeric(sector["sector_ret60"], errors="coerce").rank(pct=True).fillna(0.0)
    score = 0.0
    score = score + 18 * sector["sector_rs_rank5"].fillna(0)
    score = score + 24 * sector["sector_rs_rank20"].fillna(0)
    score = score + 16 * sector["sector_rs_rank60"].fillna(0)
    score = score + 12 * sector["sector_up_rate20"].fillna(0)
    score = score + 10 * sector["sector_high_pos_rate"].fillna(0)
    board_trend = (
        sector.get("board_ma5_gt10_gt20", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 8
        + sector.get("board_ma20_gt60", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 7
        + sector.get("board_ma60_gt120", pd.Series(False, index=sector.index)).fillna(False).astype(bool).astype(float) * 4
    )
    score = score + board_trend
    reg_bonus = np.where((sector["board_reg_slope20"] > 0) & (sector["board_reg_r2_20"] >= 0.20), 5, 0)
    score = score + reg_bonus
    ar = pd.to_numeric(sector["sector_amount_ratio20"], errors="coerce")
    score = score + np.where((ar >= 1.05) & (ar <= 2.2), 6, np.where((ar > 0.85) & (ar < 1.05), 3, np.where(ar > 2.8, -4, 0)))
    sector["sector_strength_score"] = pd.Series(score).clip(lower=0, upper=100).round(2)

    unclassified = sector["sector"].astype(str).eq("未分组")
    if bool(unclassified.any()):
        sector.loc[unclassified, "sector_strength_score"] = 0.0
        sector.loc[unclassified, "sector_strength_source"] = "unclassified"

    threshold = float(sector_cfg.get("mainline_score_threshold", 72.0))
    strong_threshold = float(sector_cfg.get("strong_score_threshold", 62.0))
    sector["sector_is_mainline"] = sector["sector_strength_score"] >= threshold
    sector["sector_is_strong"] = sector["sector_strength_score"] >= strong_threshold

    df = df.merge(sector, on="sector", how="left")
    df["stock_sector_rs20"] = df.groupby("sector")["ret20"].rank(pct=True).fillna(0.0)
    df["stock_sector_rs60"] = df.groupby("sector")["ret60"].rank(pct=True).fillna(0.0)
    df["outperform_sector20"] = df["ret20"] - df["sector_ret20"]
    df["outperform_sector60"] = df["ret60"] - df["sector_ret60"]
    return df


def classify_setup_row(r: pd.Series, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v6.3 买点分型：先板块主线/个股前排门槛，再识别突破、回踩、天量锚点再异动。"""
    setup_cfg = cfg.get("strategy", {}).get("setup", {})
    sector_cfg = cfg.get("strategy", {}).get("sector", {})
    close = safe_float(r.get("close")); low = safe_float(r.get("low"))
    ma5 = safe_float(r.get("ma5")); ma10 = safe_float(r.get("ma10")); ma20 = safe_float(r.get("ma20")); ma60 = safe_float(r.get("ma60"))
    ret5 = safe_float(r.get("ret5")); amount_ratio20 = safe_float(r.get("amount_ratio20"))
    high20_prev = safe_float(r.get("high20_prev")); high60_prev = safe_float(r.get("high60_prev")); high120_prev = safe_float(r.get("high120_prev"))
    close_pos60 = safe_float(r.get("close_pos60")); close_pos120 = safe_float(r.get("close_pos120"))
    contraction = safe_float(r.get("range_contraction_20_60")); candle_strength = safe_float(r.get("candle_strength"))
    upper_shadow = safe_float(r.get("upper_shadow_pct"), 0.0); lower_shadow = safe_float(r.get("lower_shadow_pct"), 0.0)
    amount_dryup20 = safe_float(r.get("amount_dryup20")); risk_pct = safe_float(r.get("risk_pct"))
    sector_score = safe_float(r.get("sector_strength_score"), 0.0)
    sector_mainline = bool(r.get("sector_is_mainline", False)); sector_strong = bool(r.get("sector_is_strong", False))
    stock_sector_rs20 = safe_float(r.get("stock_sector_rs20"), 0.0); stock_sector_rs60 = safe_float(r.get("stock_sector_rs60"), 0.0)
    outperform_sector20 = safe_float(r.get("outperform_sector20"), 0.0)

    front_min20 = float(sector_cfg.get("front_rs20_min", 0.60)); front_min60 = float(sector_cfg.get("front_rs60_min", 0.55))
    require_outperform = bool(sector_cfg.get("require_outperform_sector20", True))
    common_blocks: List[str] = []
    if bool(sector_cfg.get("hard_gate_enabled", True)) and sector_score < float(sector_cfg.get("min_score_for_any_buy", 60.0)):
        common_blocks.append(f"板块主线分{sector_score:.1f}低于入场门槛")
    if stock_sector_rs20 < front_min20 and stock_sector_rs60 < front_min60:
        common_blocks.append("个股不是板块前排")
    if require_outperform and np.isfinite(outperform_sector20) and outperform_sector20 < -0.02:
        common_blocks.append("20日明显跑输所属板块")
    candidates: List[Dict[str, Any]] = []

    # A. 平台/阶段突破：强板块 + 前排 + 平台收敛 + 温和放量 + 收盘强。
    b_score = 0.0; b_reasons: List[str] = []; b_blocks: List[str] = list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_breakout", 64.0)):
        b_score += 15; b_reasons.append("板块强度允许做突破")
    else:
        b_blocks.append("板块强度不足，普通突破容易一日游")
    if stock_sector_rs20 >= front_min20 or stock_sector_rs60 >= 0.65:
        b_score += 14; b_reasons.append("个股在板块内前排")
    if require_outperform and np.isfinite(outperform_sector20) and outperform_sector20 >= 0:
        b_score += 8; b_reasons.append("20日跑赢所属板块")
    breakout_hit = False
    if np.isfinite(high20_prev) and close >= high20_prev * 0.995:
        breakout_hit = True; b_score += 16; b_reasons.append("突破/接近20日平台高点")
    if np.isfinite(high60_prev) and close >= high60_prev * 0.990:
        breakout_hit = True; b_score += 14; b_reasons.append("突破/接近60日平台高点")
    if np.isfinite(high120_prev) and close >= high120_prev * 0.990:
        b_score += 8; b_reasons.append("接近120日阶段高点")
    if not breakout_hit:
        b_blocks.append("没有有效突破20/60日平台高点")
    if np.isfinite(close_pos60) and close_pos60 >= 0.70:
        b_score += 8; b_reasons.append("价格处于60日强势区")
    if np.isfinite(close_pos120) and close_pos120 >= 0.60:
        b_score += 4
    if np.isfinite(contraction) and 0.28 <= contraction <= 0.92:
        b_score += 12; b_reasons.append("突破前波动收敛")
    elif bool(setup_cfg.get("breakout_need_contraction", True)):
        b_blocks.append("突破前没有明显平台收敛")
    if np.isfinite(amount_ratio20) and float(setup_cfg.get("breakout_amount_min", 1.20)) <= amount_ratio20 <= float(setup_cfg.get("breakout_amount_max", 2.60)):
        b_score += 14; b_reasons.append("突破量能温和放大")
    else:
        b_blocks.append("突破量能不在健康区间")
    if candle_strength > 0.25 and upper_shadow <= 0.065:
        b_score += 7; b_reasons.append("收盘强且上影不长")
    else:
        b_blocks.append("突破K线收盘强度不足或上影偏长")
    if np.isfinite(ret5) and ret5 > 0.18:
        b_score -= 10; b_blocks.append("近5日涨幅过大，疑似追涨末端")
    candidates.append({"setup_type": "breakout", "setup_score": round(max(0, min(100, b_score)), 2), "setup_reason": "；".join(unique_nonempty(b_reasons)), "setup_blockers": "；".join(unique_nonempty(b_blocks))})

    # B. 主线回踩：只在强主线/前排里做，强主线优先看 MA5/MA10，普通主线看 MA10/MA20。
    p_score = 0.0; p_reasons: List[str] = []; p_blocks: List[str] = list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_pullback", 72.0)) and (sector_mainline or sector_strong):
        p_score += 20; p_reasons.append("板块主线强，允许做短均线回踩")
    else:
        p_blocks.append("板块不是强主线，不做回踩低吸")
    if stock_sector_rs20 >= float(sector_cfg.get("core_rs20_min", 0.75)) or (stock_sector_rs20 >= front_min20 and stock_sector_rs60 >= 0.65):
        p_score += 16; p_reasons.append("个股为板块核心/前排")
    else:
        p_blocks.append("个股板块内强度不足，不做回踩")
    if np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20) and close >= ma5 >= ma10 >= ma20:
        p_score += 14; p_reasons.append("5/10/20日线强多头")
    elif np.isfinite(ma10) and np.isfinite(ma20) and np.isfinite(ma60) and close >= ma10 >= ma20 >= ma60:
        p_score += 9; p_reasons.append("10/20/60日线趋势保持")
    else:
        p_blocks.append("短中期均线结构不够强")
    mas = setup_cfg.get("strong_mainline_pullback_ma", [5, 10]) if sector_score >= 82 else setup_cfg.get("normal_mainline_pullback_ma", [10, 20])
    ma_map = {5: ma5, 10: ma10, 20: ma20}; dist = float(setup_cfg.get("pullback_distance_pct", 0.035))
    touched = False; reclaimed = False; line_name = ""
    for w in mas:
        m = ma_map.get(int(w), np.nan)
        if np.isfinite(m) and np.isfinite(close):
            touched_by_close = abs(close / m - 1.0) <= dist
            touched_by_low = np.isfinite(low) and low <= m * (1.0 + dist) and close >= m * 0.997
            if touched_by_close or touched_by_low:
                touched = True; line_name = f"MA{int(w)}"
            if close >= m:
                reclaimed = True
    if touched:
        p_score += 16; p_reasons.append(f"回踩{line_name}附近")
    else:
        p_blocks.append("没有回踩到关键均线附近")
    if reclaimed:
        p_score += 12; p_reasons.append("收盘收回关键均线")
    else:
        p_blocks.append("未收回关键均线")
    if np.isfinite(amount_dryup20) and amount_dryup20 <= float(setup_cfg.get("pullback_amount_max", 1.15)):
        p_score += 12; p_reasons.append("回踩过程缩量")
    elif bool(setup_cfg.get("pullback_need_dryup", True)):
        p_blocks.append("回踩没有缩量")
    if lower_shadow > upper_shadow and lower_shadow > 0.018:
        p_score += 5; p_reasons.append("下影承接")
    if np.isfinite(ret5) and -0.10 <= ret5 <= 0.08:
        p_score += 5
    if np.isfinite(risk_pct) and risk_pct <= 0.10:
        p_score += 5
    candidates.append({"setup_type": "pullback", "setup_score": round(max(0, min(100, p_score)), 2), "setup_reason": "；".join(unique_nonempty(p_reasons)), "setup_blockers": "；".join(unique_nonempty(p_blocks))})

    # C. 天量锚点再异动：锚点有效 + 主线未退潮 + 再次异动确认。
    a_score = safe_float(r.get("anchor_score"), 0.0)
    a_reasons = split_reason_text(str(r.get("anchor_reason", "")))
    a_blocks = split_reason_text(str(r.get("anchor_blockers", ""))) + list(common_blocks)
    if sector_score >= float(sector_cfg.get("min_score_for_anchor", 68.0)):
        a_score += 8; a_reasons.append("板块强度支持二次异动")
    else:
        a_blocks.append("板块强度不足，天量锚点二次异动不做")
    if stock_sector_rs20 >= front_min20:
        a_score += 8; a_reasons.append("个股板块内仍在前排")
    else:
        a_blocks.append("锚点后个股已掉出板块前排")
    candidates.append({"setup_type": "volume_anchor_reaccumulation", "setup_score": round(max(0, min(100, a_score)), 2), "setup_reason": "；".join(unique_nonempty(a_reasons)), "setup_blockers": "；".join(unique_nonempty(a_blocks))})

    best = sorted(candidates, key=lambda x: x["setup_score"], reverse=True)[0]
    if best["setup_type"] == "breakout":
        th = float(setup_cfg.get("breakout_score_threshold", 72.0))
    elif best["setup_type"] == "pullback":
        th = float(setup_cfg.get("pullback_score_threshold", 72.0))
    else:
        th = float(setup_cfg.get("anchor_score_threshold", 74.0))
    btxt = str(best.get("setup_blockers", ""))
    critical_block = any(key in btxt for key in ["入场门槛", "不是板块前排", "不做回踩", "没有有效突破", "未收回", "跌破天量", "二次异动不做"])
    best["setup_ok"] = bool(best["setup_score"] >= th and not critical_block)
    best["setup_threshold"] = th
    best["setup_all_scores"] = "；".join([f"{x['setup_type']}={x['setup_score']}" for x in candidates])
    return best


def compute_raw_metrics(
    code: str,
    name: str,
    hist: pd.DataFrame,
    cfg: Dict[str, Any],
    market: MarketState,
) -> Dict[str, Any]:
    st = cfg["strategy"]
    min_days = int(st.get("min_history_days", 160))
    atr_period = int(st.get("atr_period", 14))
    result: Dict[str, Any] = {
        "code": code,
        "name": name,
        "ok_base": False,
        "filter_reason": "",
        "data_provider": getattr(hist, "attrs", {}).get("data_provider", "") if hist is not None else "",
        "data_warning": getattr(hist, "attrs", {}).get("data_warning", "") if hist is not None else "",
    }
    if hist is None or hist.empty or len(hist) < min_days:
        result["filter_reason"] = f"历史K线不足{min_days}日"
        return result

    ind = add_indicators(hist, atr_period=atr_period)
    last = ind.iloc[-1]
    close = safe_float(last.get("close"))
    if not np.isfinite(close) or close <= 0:
        result["filter_reason"] = "收盘价无效"
        return result

    risk_gate = compute_risk_gate(ind, code, name, cfg)
    anchor_info = detect_volume_anchor_setup(ind, cfg)

    # 计算止损价：2.5ATR、MA20 下方、20日低点下方三者取更靠近当前价的位置，再套最小止损。
    atr = safe_float(last.get("atr"))
    ma5 = safe_float(last.get("ma5"))
    ma10 = safe_float(last.get("ma10"))
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    ma120 = safe_float(last.get("ma120"))
    ma250 = safe_float(last.get("ma250"))
    low20 = safe_float(last.get("low20"))
    atr_mult = float(st.get("atr_mult", 2.5))
    min_stop_pct = float(st.get("min_stop_pct", 0.04))
    max_stop_pct = float(st.get("max_stop_pct", 0.12))
    stop_candidates = []
    if np.isfinite(atr) and atr > 0:
        stop_candidates.append(close - atr_mult * atr)
    if np.isfinite(ma20) and ma20 > 0:
        stop_candidates.append(ma20 * 0.97)
    if np.isfinite(low20) and low20 > 0:
        stop_candidates.append(low20 * 0.98)
    stop_candidates = [x for x in stop_candidates if np.isfinite(x) and 0 < x < close]
    stop_loss = max(stop_candidates) if stop_candidates else close * (1 - min_stop_pct)
    if (close - stop_loss) / close < min_stop_pct:
        stop_loss = close * (1 - min_stop_pct)
    risk_pct = (close - stop_loss) / close

    pct_chg = safe_float(last.get("pct_chg"))
    ret3 = safe_float(last.get("ret3"))
    ret5 = safe_float(last.get("ret5"))
    ret6 = safe_float(last.get("ret6"))
    ret10 = safe_float(last.get("ret10"))
    ret20 = safe_float(last.get("ret20"))
    ret30 = safe_float(last.get("ret30"))
    ret60 = safe_float(last.get("ret60"))
    ret120 = safe_float(last.get("ret120"))
    ma5_slope3 = safe_float(last.get("ma5_slope3"))
    ma10_slope5 = safe_float(last.get("ma10_slope5"))
    ma20_slope10 = safe_float(last.get("ma20_slope10"))
    ma60_slope20 = safe_float(last.get("ma60_slope20"))
    ma120_slope40 = safe_float(last.get("ma120_slope40"))
    high20 = safe_float(last.get("high20"))
    high20_prev = safe_float(last.get("high20_prev"))
    high60 = safe_float(last.get("high60"))
    high120 = safe_float(last.get("high120"))
    high120_prev = safe_float(last.get("high120_prev"))
    high60_prev = safe_float(last.get("high60_prev"))
    amount_ma20 = safe_float(last.get("amount_ma20"))
    amount_ratio5 = safe_float(last.get("amount_ratio5"))
    amount_ratio10 = safe_float(last.get("amount_ratio10"))
    amount_ratio20 = safe_float(last.get("amount_ratio20"))
    amount_ratio120_rank = safe_float(last.get("amount_ratio120_rank"))
    amount_dryup20 = safe_float(last.get("amount_dryup20"))
    atr_pct = safe_float(last.get("atr_pct"))
    drawdown10 = safe_float(last.get("drawdown10"))
    drawdown20 = safe_float(last.get("drawdown20"))
    drawdown60 = safe_float(last.get("drawdown60"))
    drawdown120 = safe_float(last.get("drawdown120"))
    drawdown250 = safe_float(last.get("drawdown250"))
    upper_shadow_pct = safe_float(last.get("upper_shadow_pct"), 0.0)
    lower_shadow_pct = safe_float(last.get("lower_shadow_pct"), 0.0)
    body_pct = safe_float(last.get("body_pct"))
    candle_strength = safe_float(last.get("candle_strength"))
    close_pos20 = safe_float(last.get("close_pos20"))
    close_pos60 = safe_float(last.get("close_pos60"))
    close_pos120 = safe_float(last.get("close_pos120"))
    range_contraction = safe_float(last.get("range_contraction_20_60"))
    reg_slope20 = safe_float(last.get("reg_slope20"))
    reg_r2_20 = safe_float(last.get("reg_r2_20"))
    reg_slope60 = safe_float(last.get("reg_slope60"))
    reg_r2_60 = safe_float(last.get("reg_r2_60"))
    turnover = safe_float(last.get("turnover")) if "turnover" in ind.columns else np.nan

    filters = []
    if st.get("exclude_st", True) and ("ST" in str(name).upper() or "*ST" in str(name).upper()):
        filters.append("ST/*ST")
    if not (float(st.get("min_close", 3.0)) <= close <= float(st.get("max_close", 300.0))):
        filters.append("价格区间不合适")
    if np.isfinite(amount_ma20) and amount_ma20 < float(st.get("min_amount_ma20", 50_000_000)):
        filters.append("成交额不足")
    if np.isfinite(pct_chg) and pct_chg > float(st.get("max_chase_day_pct", 7.5)):
        filters.append("当日涨幅过大不追")
    if np.isfinite(pct_chg) and pct_chg < float(st.get("min_day_pct", -5.5)):
        filters.append("当日跌幅过大不接")
    if upper_shadow_pct > float(st.get("avoid_upper_shadow_pct", 0.08)):
        filters.append("上影线过长")
    if bool(risk_gate.get("risk_gate_block", False)):
        filters.append("A股风险闸门：" + str(risk_gate.get("risk_gate_reason", "高风险结构")))

    # 个股硬过滤从“简单多头”升级成图形结构过滤：中期均线、回归趋势、价格所处区间都要配合。
    trend_ok = close > ma60 and ma20 > ma60 and ma20_slope10 > 0 and (not np.isfinite(reg_slope20) or reg_slope20 > -0.02)
    structure_ok = (not np.isfinite(close_pos60)) or close_pos60 >= 0.45
    if not (trend_ok and structure_ok):
        filters.append("趋势/图形结构未达标")
    if not (ret60 > 0 and ret60 > market.market_ret60):
        filters.append("60日相对强度不足")
    if risk_pct > max_stop_pct:
        filters.append("止损距离过宽")
    if market.target_exposure <= 0:
        filters.append("大盘弱势禁止新开仓")

    setup_tags: List[str] = []
    if close > ma5 > ma10 > ma20:
        setup_tags.append("强主线短均线多头")
    elif close > ma20 > ma60:
        setup_tags.append("短中期均线多头")
    if np.isfinite(reg_slope20) and reg_slope20 > 0 and np.isfinite(reg_r2_20) and reg_r2_20 >= 0.25:
        setup_tags.append("回归趋势向上")
    if np.isfinite(high120_prev) and close >= high120_prev * 0.995:
        setup_tags.append("120日突破")
    elif np.isfinite(high60_prev) and close >= high60_prev * 0.995:
        setup_tags.append("60日突破")
    elif np.isfinite(close_pos60) and close_pos60 >= 0.72:
        setup_tags.append("处于60日区间强势区")
    if np.isfinite(range_contraction) and 0.35 <= range_contraction <= 0.80:
        setup_tags.append("波动收敛")
    if np.isfinite(amount_ratio20) and 1.05 <= amount_ratio20 <= 2.80:
        setup_tags.append("量能温和放大")
    if lower_shadow_pct > upper_shadow_pct and lower_shadow_pct > 0.025:
        setup_tags.append("下影承接")
    if anchor_info.get("anchor_valid"):
        setup_tags.append("天量锚点后缩量再异动")

    result.update({
        "date": last["date"].strftime("%Y-%m-%d"),
        "open": safe_float(last.get("open")),
        "high": safe_float(last.get("high")),
        "low": safe_float(last.get("low")),
        "close": close,
        "pct_chg": pct_chg,
        "ret3": ret3,
        "ret5": ret5,
        "ret6": ret6,
        "ret10": ret10,
        "ret20": ret20,
        "ret30": ret30,
        "ret60": ret60,
        "ret120": ret120,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "ma250": ma250,
        "ma5_slope3": ma5_slope3,
        "ma10_slope5": ma10_slope5,
        "ma20_slope10": ma20_slope10,
        "ma60_slope20": ma60_slope20,
        "ma120_slope40": ma120_slope40,
        "high20": high20,
        "high20_prev": high20_prev,
        "high60": high60,
        "high120": high120,
        "high60_prev": high60_prev,
        "high120_prev": high120_prev,
        "low5": safe_float(last.get("low5")),
        "low10": safe_float(last.get("low10")),
        "low20": safe_float(last.get("low20")),
        "low5_prev": safe_float(last.get("low5_prev")),
        "low10_prev": safe_float(last.get("low10_prev")),
        "low20_prev": safe_float(last.get("low20_prev")),
        "drawdown10": drawdown10,
        "drawdown20": drawdown20,
        "drawdown60": drawdown60,
        "drawdown120": drawdown120,
        "drawdown250": drawdown250,
        "close_pos20": close_pos20,
        "close_pos60": close_pos60,
        "close_pos120": close_pos120,
        "range_contraction_20_60": range_contraction,
        "amount_ma20": amount_ma20,
        "amount_ratio5": amount_ratio5,
        "amount_ratio10": amount_ratio10,
        "amount_ratio20": amount_ratio20,
        "amount_ratio120_rank": amount_ratio120_rank,
        "amount_dryup20": amount_dryup20,
        "turnover": turnover,
        "atr": atr,
        "atr_pct": atr_pct,
        "stop_loss": stop_loss,
        "risk_pct": risk_pct,
        "upper_shadow_pct": upper_shadow_pct,
        "lower_shadow_pct": lower_shadow_pct,
        "body_pct": body_pct,
        "candle_strength": candle_strength,
        "reg_slope20": reg_slope20,
        "reg_r2_20": reg_r2_20,
        "reg_slope60": reg_slope60,
        "reg_r2_60": reg_r2_60,
        "market_ret60": market.market_ret60,
        "market_regime": market.regime,
        "market_target_exposure": market.target_exposure,
        "setup_tags": "；".join(unique_nonempty(setup_tags)),
    })
    result.update(anchor_info)
    result.update(risk_gate)
    result["ok_base"] = len(filters) == 0
    result["filter_reason"] = "；".join(filters)
    return result


def score_candidates(metrics: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if metrics.empty:
        return metrics
    df = metrics.copy()
    numeric_cols = [
        "close", "pct_chg", "ret3", "ret5", "ret6", "ret10", "ret20", "ret30", "ret60", "ret120", "ma20", "ma60", "ma120", "ma250",
        "ma20_slope10", "ma60_slope20", "ma120_slope40", "high20", "high60", "high120",
        "high60_prev", "high120_prev", "drawdown10", "drawdown20", "drawdown60", "drawdown120", "drawdown250", "close_pos20", "close_pos60", "close_pos120",
        "range_contraction_20_60", "amount_ma20", "amount_ratio20", "amount_dryup20", "atr_pct", "risk_pct",
        "turnover", "upper_shadow_pct", "lower_shadow_pct", "body_pct", "candle_strength",
        "reg_slope20", "reg_r2_20", "reg_slope60", "reg_r2_60",
        "limit_down_count_3", "limit_down_count_6", "limit_down_count_10", "days_since_limit_down",
        "ma5", "ma10", "ma5_slope3", "ma10_slope5", "high20_prev", "amount_ratio5", "amount_ratio10", "amount_ratio120_rank",
        "sector_strength_score", "sector_ret20", "sector_ret60", "sector_up_rate20", "sector_high_pos_rate",
        "stock_sector_rs20", "stock_sector_rs60", "outperform_sector20", "outperform_sector60",
        "anchor_score", "anchor_low", "anchor_high", "anchor_days_ago",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for required_col in ["ret60", "ret120", "score", "ok_base", "filter_reason"]:
        if required_col not in df.columns:
            df[required_col] = np.nan if required_col != "ok_base" else False

    # 缺失收益率不能参与相对强弱排名，避免数据错误股票出现假分数。
    df["rs_rank60"] = pd.to_numeric(df["ret60"], errors="coerce").rank(pct=True).fillna(0.0)
    df["rs_rank120"] = pd.to_numeric(df["ret120"], errors="coerce").rank(pct=True).fillna(0.0)

    scores = []
    reasons_all = []
    trend_scores = []
    momentum_scores = []
    breakout_scores = []
    risk_scores = []
    sector_scores = []
    setup_scores = []
    setup_types = []
    setup_reasons = []
    setup_blockers = []
    setup_ok_list = []
    score_details_all = []
    score_weaknesses_all = []
    for _, r in df.iterrows():
        filter_reason = str(r.get("filter_reason", ""))
        close = safe_float(r.get("close"))
        ret60 = safe_float(r.get("ret60"))
        ret120 = safe_float(r.get("ret120"))
        if ("数据错误" in filter_reason) or (not np.isfinite(close)) or (not np.isfinite(ret60)):
            scores.append(0.0)
            reasons_all.append("")
            trend_scores.append(0.0)
            momentum_scores.append(0.0)
            breakout_scores.append(0.0)
            risk_scores.append(0.0)
            sector_scores.append(0.0)
            setup_scores.append(0.0)
            setup_types.append("none")
            setup_reasons.append("")
            setup_blockers.append("数据无效或K线缺失")
            setup_ok_list.append(False)
            score_details_all.append("数据无效，未评分")
            score_weaknesses_all.append("数据无效或K线缺失")
            continue
        risk_gate_reason = str(r.get("risk_gate_reason", ""))
        if bool(r.get("risk_gate_block", False)) or risk_gate_reason:
            scores.append(0.0)
            reasons_all.append("")
            trend_scores.append(0.0)
            momentum_scores.append(0.0)
            breakout_scores.append(0.0)
            risk_scores.append(0.0)
            sector_scores.append(0.0)
            setup_scores.append(0.0)
            setup_types.append("risk_gate_blocked")
            setup_reasons.append("")
            setup_blockers.append(risk_gate_reason or "A股风险闸门触发")
            setup_ok_list.append(False)
            score_details_all.append("A股风险闸门触发，禁止评分为买点")
            score_weaknesses_all.append(risk_gate_reason or "连续跌停/高位崩盘/暴涨后踩踏风险")
            continue

        ma20 = safe_float(r.get("ma20"))
        ma60 = safe_float(r.get("ma60"))
        ma120 = safe_float(r.get("ma120"))
        ma250 = safe_float(r.get("ma250"))
        ret20 = safe_float(r.get("ret20"))
        rs60 = safe_float(r.get("rs_rank60"), 0)
        rs120 = safe_float(r.get("rs_rank120"), 0)
        high20 = safe_float(r.get("high20"))
        high60 = safe_float(r.get("high60"))
        high120 = safe_float(r.get("high120"))
        high60_prev = safe_float(r.get("high60_prev"))
        high120_prev = safe_float(r.get("high120_prev"))
        amount_ratio = safe_float(r.get("amount_ratio20"))
        amount_dryup = safe_float(r.get("amount_dryup20"))
        atr_pct = safe_float(r.get("atr_pct"))
        drawdown120 = safe_float(r.get("drawdown120"))
        risk_pct = safe_float(r.get("risk_pct"))
        ma20_slope10 = safe_float(r.get("ma20_slope10"))
        ma60_slope20 = safe_float(r.get("ma60_slope20"))
        ma120_slope40 = safe_float(r.get("ma120_slope40"))
        close_pos60 = safe_float(r.get("close_pos60"))
        close_pos120 = safe_float(r.get("close_pos120"))
        contraction = safe_float(r.get("range_contraction_20_60"))
        reg_slope20 = safe_float(r.get("reg_slope20"))
        reg_r2_20 = safe_float(r.get("reg_r2_20"))
        reg_slope60 = safe_float(r.get("reg_slope60"))
        reg_r2_60 = safe_float(r.get("reg_r2_60"))
        candle_strength = safe_float(r.get("candle_strength"))
        upper_shadow = safe_float(r.get("upper_shadow_pct"), 0.0)
        lower_shadow = safe_float(r.get("lower_shadow_pct"), 0.0)
        sector_strength = safe_float(r.get("sector_strength_score"), 0.0)
        sector_ret20 = safe_float(r.get("sector_ret20"))
        sector_ret60 = safe_float(r.get("sector_ret60"))
        sector_up_rate20 = safe_float(r.get("sector_up_rate20"), 0.0)
        sector_high_pos_rate = safe_float(r.get("sector_high_pos_rate"), 0.0)
        stock_sector_rs20 = safe_float(r.get("stock_sector_rs20"), 0.0)
        stock_sector_rs60 = safe_float(r.get("stock_sector_rs60"), 0.0)
        outperform_sector20 = safe_float(r.get("outperform_sector20"))
        outperform_sector60 = safe_float(r.get("outperform_sector60"))

        setup_info = classify_setup_row(r, cfg)
        setup_type = str(setup_info.get("setup_type", "none"))
        setup_score = safe_float(setup_info.get("setup_score"), 0.0)
        setup_ok = bool(setup_info.get("setup_ok", False))
        setup_reason = str(setup_info.get("setup_reason", ""))
        setup_block = str(setup_info.get("setup_blockers", ""))

        trend = 0.0
        trend += 4 if close > ma20 else 0
        trend += 6 if close > ma60 else 0
        trend += 5 if close > ma120 else 0
        trend += 2 if np.isfinite(ma250) and close > ma250 else 0
        trend += 5 if ma20 > ma60 else 0
        trend += 4 if ma60 > ma120 else 0
        trend += 3 if np.isfinite(ma250) and ma120 > ma250 else 0
        trend += 3 if ma20_slope10 > 0 else 0
        trend += 2 if ma60_slope20 > 0 else 0
        trend += 2 if ma120_slope40 > 0 else 0
        trend += 4 if reg_slope20 > 0 and reg_r2_20 >= 0.25 else 0
        trend += 2 if reg_slope60 > 0 and reg_r2_60 >= 0.20 else 0
        trend = min(trend, 35)

        momentum = 0.0
        momentum += 10 * max(0, min(1, rs60))
        momentum += 6 * max(0, min(1, rs120))
        momentum += 3 if ret20 > 0 else 0
        momentum += 3 if ret60 > 0 else 0
        momentum += 2 if ret120 > 0 else 0
        momentum += 1 if (np.isfinite(ret20) and np.isfinite(ret60) and ret20 > ret60 / 3) else 0
        momentum = min(momentum, 25)

        structure = 0.0
        if np.isfinite(high120_prev) and high120_prev > 0:
            if close >= high120_prev * 0.995:
                structure += 7
            elif np.isfinite(high120) and close >= high120 * 0.97:
                structure += 5
        if np.isfinite(high60_prev) and high60_prev > 0:
            if close >= high60_prev * 0.995:
                structure += 5
            elif np.isfinite(high60) and close >= high60 * 0.97:
                structure += 3
        if np.isfinite(high20) and high20 > 0:
            if close >= high20 * 0.995:
                structure += 3
            elif close >= high20 * 0.97:
                structure += 2
        if np.isfinite(close_pos60):
            if close_pos60 >= 0.75:
                structure += 3
            elif close_pos60 >= 0.55:
                structure += 2
        if np.isfinite(close_pos120) and close_pos120 >= 0.55:
            structure += 2
        if np.isfinite(contraction) and 0.35 <= contraction <= 0.80:
            structure += 3
        elif np.isfinite(contraction) and contraction < 0.35:
            structure += 1
        if np.isfinite(amount_ratio):
            if 1.05 <= amount_ratio <= 2.80:
                structure += 4
            elif 0.80 <= amount_ratio < 1.05:
                structure += 2
            elif amount_ratio > 3.50:
                structure -= 2
        if np.isfinite(amount_dryup) and amount_dryup <= 0.85 and np.isfinite(contraction) and contraction <= 0.85:
            structure += 1
        if candle_strength > 0.20:
            structure += 2
        if upper_shadow > 0.08:
            structure -= 3
        if lower_shadow > upper_shadow and lower_shadow > 0.025:
            structure += 1
        structure = max(0.0, min(structure, 25))

        risk = 0.0
        if np.isfinite(atr_pct) and 0.012 <= atr_pct <= 0.075:
            risk += 4
        elif np.isfinite(atr_pct) and 0.075 < atr_pct <= 0.10:
            risk += 1
        if np.isfinite(drawdown120) and -0.20 <= drawdown120 <= -0.00:
            risk += 4
        elif np.isfinite(drawdown120) and drawdown120 > -0.02:
            risk += 3
        if np.isfinite(risk_pct) and 0.04 <= risk_pct <= 0.10:
            risk += 5
        elif np.isfinite(risk_pct) and risk_pct <= 0.12:
            risk += 2
        if np.isfinite(amount_ratio) and amount_ratio <= 3.0:
            risk += 2
        risk = min(risk, 15)

        # v6 不再把“突破/回踩/锚点”揉成一个结构分，而是先分型，再把板块主线和买点分型纳入总分。
        sector_component = 0.0
        sector_component += min(20.0, sector_strength * 0.20)
        sector_component += 5.0 if bool(r.get("sector_is_strong", False)) else 0.0
        sector_component += 5.0 if bool(r.get("sector_is_mainline", False)) else 0.0
        sector_component += 5.0 * max(0.0, min(1.0, stock_sector_rs20))
        sector_component += 5.0 * max(0.0, min(1.0, stock_sector_rs60))
        sector_component = min(30.0, sector_component)
        trend_component = min(20.0, trend / 35.0 * 20.0)
        momentum_component = min(15.0, momentum / 25.0 * 15.0)
        setup_component = min(25.0, setup_score / 100.0 * 25.0)
        risk_component = min(10.0, risk / 15.0 * 10.0)
        total = trend_component + momentum_component + sector_component + setup_component + risk_component
        reasons = split_reason_text(r.get("setup_tags", ""))
        if setup_reason:
            reasons.extend(split_reason_text(setup_reason))
        weaknesses = []
        if sector_strength >= float(cfg.get("strategy", {}).get("sector", {}).get("mainline_score_threshold", 68.0)):
            reasons.append("所属板块为主线/强主线")
        elif sector_strength >= float(cfg.get("strategy", {}).get("sector", {}).get("strong_score_threshold", 58.0)):
            reasons.append("所属板块偏强")
        else:
            weaknesses.append("所属板块主线强度不足")
        if stock_sector_rs20 >= 0.70 or stock_sector_rs60 >= 0.70:
            reasons.append("个股在板块内前排")
        else:
            weaknesses.append("个股板块内强度不靠前")
        if setup_ok:
            reasons.append(f"买点分型={setup_type}")
        else:
            weaknesses.append(f"买点分型未达标：{setup_type} {setup_score:.1f}/{safe_float(setup_info.get('setup_threshold'), 0):.1f}")
            if setup_block:
                weaknesses.extend(split_reason_text(setup_block))

        if trend >= 27:
            reasons.append("趋势模板优秀")
        else:
            if not (close > ma20): weaknesses.append("未站上20日线")
            if not (close > ma60): weaknesses.append("未站上60日线")
            if not (close > ma120): weaknesses.append("未站上120日线")
            if not (ma20 > ma60): weaknesses.append("20日线未高于60日线")
            if not (ma60 > ma120): weaknesses.append("60日线未高于120日线")
            if not (ma20_slope10 > 0): weaknesses.append("20日线斜率未转正")
            if not (reg_slope20 > 0 and reg_r2_20 >= 0.25): weaknesses.append("20日趋势推进效率不足")
        if rs60 >= 0.70:
            reasons.append("股票池内60日相对强")
        else:
            weaknesses.append("股票池内60日强度排名不靠前")
        if structure >= 17:
            reasons.append("突破/平台形态较好")
        else:
            if np.isfinite(high120) and high120 > 0 and close < high120 * 0.94:
                weaknesses.append("距离120日高点较远")
            if np.isfinite(close_pos60) and close_pos60 < 0.55:
                weaknesses.append("价格不在60日区间强势区")
            if (not np.isfinite(amount_ratio)) or amount_ratio < 0.80:
                weaknesses.append("量能未放大")
            elif amount_ratio > 3.50:
                weaknesses.append("量能过热，追高风险变大")
            if np.isfinite(contraction) and contraction > 1.05:
                weaknesses.append("波动未收敛")
        if np.isfinite(risk_pct):
            reasons.append(f"止损{risk_pct:.1%}")
        if np.isfinite(risk_pct) and risk_pct > float(cfg["strategy"].get("max_stop_pct", 0.12)):
            weaknesses.append("止损距离过宽")

        scores.append(round(total, 2))
        reasons_all.append("；".join(unique_nonempty(reasons)))
        trend_scores.append(round(trend_component, 2))
        momentum_scores.append(round(momentum_component, 2))
        breakout_scores.append(round(setup_component, 2))
        risk_scores.append(round(risk_component, 2))
        sector_scores.append(round(sector_component, 2))
        setup_scores.append(round(setup_score, 2))
        setup_types.append(setup_type)
        setup_reasons.append(setup_reason)
        setup_blockers.append(setup_block)
        setup_ok_list.append(setup_ok)
        score_details_all.append(
            f"趋势{trend_component:.1f}/20；动量/市场RS{momentum_component:.1f}/15；板块主线{sector_component:.1f}/30；买点分型{setup_component:.1f}/25；风控{risk_component:.1f}/10；"
            f"原始买点={setup_type} {setup_score:.1f}/{safe_float(setup_info.get('setup_threshold'), 0):.1f}"
        )
        score_weaknesses_all.append("；".join(unique_nonempty(weaknesses)))

    df["score"] = scores
    df["reason"] = reasons_all
    df["trend_score"] = trend_scores
    df["momentum_score"] = momentum_scores
    df["breakout_score"] = breakout_scores
    df["risk_score"] = risk_scores
    df["sector_score"] = sector_scores
    df["setup_score"] = setup_scores
    df["setup_type"] = setup_types
    df["setup_reason"] = setup_reasons
    df["setup_blockers"] = setup_blockers
    df["setup_ok"] = setup_ok_list
    df["score_detail"] = score_details_all
    df["score_weakness"] = score_weaknesses_all
    threshold = float(cfg["strategy"].get("score_threshold", 72))
    def _signal_grade(row: pd.Series) -> str:
        score = safe_float(row.get("score"), 0.0)
        sector = safe_float(row.get("sector_strength_score"), 0.0)
        setup = safe_float(row.get("setup_score"), 0.0)
        rs20 = safe_float(row.get("stock_sector_rs20"), 0.0)
        if score >= 84 and sector >= 82 and setup >= 82 and rs20 >= 0.75:
            return "S"
        if score >= 78 and sector >= 72 and setup >= 76 and rs20 >= 0.65:
            return "A"
        if score >= threshold:
            return "B"
        return "C"
    df["signal_grade"] = df.apply(_signal_grade, axis=1)
    df["is_signal"] = df["ok_base"].fillna(False) & df["setup_ok"].fillna(False) & (df["score"] >= threshold)
    return df


def allocate_positions(signals: pd.DataFrame, cfg: Dict[str, Any], market: MarketState, account: float) -> pd.DataFrame:
    if signals.empty:
        return signals
    st = cfg["strategy"]
    max_positions = int(st.get("max_positions", 5))
    max_position_pct = float(st.get("max_position_pct", 0.15))
    risk_per_trade_pct = float(st.get("risk_per_trade_pct", 0.005))
    total_exposure = float(market.target_exposure)

    out = signals.sort_values(["score", "rs_rank60"], ascending=[False, False]).head(max_positions).copy()
    if out.empty or total_exposure <= 0:
        out["target_weight"] = 0.0
        out["target_cash"] = 0.0
        out["target_shares"] = 0
        return out

    score_sum = out["score"].sum()
    if not np.isfinite(score_sum) or score_sum <= 0:
        raw_weights = np.full(len(out), total_exposure / len(out))
    else:
        raw_weights = total_exposure * out["score"].values / score_sum

    weights = []
    for raw_w, (_, r) in zip(raw_weights, out.iterrows()):
        risk_pct = safe_float(r.get("risk_pct"))
        if np.isfinite(risk_pct) and risk_pct > 0:
            risk_cap_weight = risk_per_trade_pct / risk_pct
        else:
            risk_cap_weight = max_position_pct
        grade = str(r.get("signal_grade", "B")).upper()
        grade_cap = {"S": max_position_pct, "A": max_position_pct * 0.95, "B": max_position_pct * 0.75}.get(grade, max_position_pct * 0.45)
        weights.append(max(0.0, min(float(raw_w), max_position_pct, grade_cap, risk_cap_weight)))

    out["target_weight"] = weights
    if account and account > 0:
        out["target_cash"] = out["target_weight"] * account
        shares = []
        for _, r in out.iterrows():
            close = safe_float(r.get("close"))
            cash = safe_float(r.get("target_cash"), 0.0)
            if close > 0:
                # A 股一手 100 股，不足一手记为 0
                shares.append(int(math.floor(cash / close / 100) * 100))
            else:
                shares.append(0)
        out["target_shares"] = shares
        out["actual_weight_by_lot"] = out["target_shares"] * out["close"] / account
    else:
        out["target_cash"] = np.nan
        out["target_shares"] = np.nan
        out["actual_weight_by_lot"] = np.nan

    # 以 R 倍数给止盈参考；实际执行用移动止盈更合理。
    out["take_profit_1"] = out["close"] + 1.5 * (out["close"] - out["stop_loss"])
    out["take_profit_2"] = out["close"] + 3.0 * (out["close"] - out["stop_loss"])
    return out


def fetch_name_map(fetcher: AkshareFetcher) -> Dict[str, str]:
    try:
        spot = fetcher.stock_spot_all()
        if {"代码", "名称"}.issubset(spot.columns):
            return {str(c).zfill(6): str(n) for c, n in zip(spot["代码"], spot["名称"])}
    except Exception:
        pass
    try:
        import akshare as ak
        info = ak.stock_info_a_code_name()
        if {"code", "name"}.issubset(info.columns):
            return {str(c).zfill(6): str(n) for c, n in zip(info["code"], info["name"])}
    except Exception:
        pass
    return {}


def resolve_stock_by_name(term: str, fetcher: Optional[AkshareFetcher] = None, name_map: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """把“贵州茅台”或“600519”解析成 (code, name)。名称模糊匹配到多只时会报错。"""
    term = str(term).strip()
    m = re.search(r"(\d{6})", term)
    if m:
        code = normalize_code(m.group(1))
        name = ""
        if name_map:
            name = name_map.get(code, "")
        return code, name

    if not term:
        raise ValueError("股票名称为空")
    if name_map is None:
        if fetcher is None:
            raise ValueError("没有可用的股票名称表，无法按名称匹配")
        name_map = fetch_name_map(fetcher)
    if not name_map:
        raise ValueError("未能获取 A 股代码名称表，建议直接发送 6 位股票代码")

    exact = [(c, n) for c, n in name_map.items() if str(n).strip() == term]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [(c, n) for c, n in name_map.items() if term in str(n)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(exact) > 1 or len(fuzzy) > 1:
        sample = (exact or fuzzy)[:8]
        choices = "、".join([f"{c} {n}" for c, n in sample])
        raise ValueError(f"名称“{term}”匹配到多只股票：{choices}。请改发 6 位代码。")
    raise ValueError(f"没有找到名称为“{term}”的 A 股股票，建议直接发送 6 位代码。")


def clean_stock_name(raw: str) -> str:
    s = str(raw)
    s = re.sub(r"\d{6}", " ", s)
    s = re.sub(r"(加入|添加|新增|加到|放入|放进|移入|删除|移除|剔除|去掉|踢出|查看|列表|解释|为什么|为啥|原因|逻辑|详细|复盘|有没有|没有|没买|不买|买入|信号|生成|扫描|筛选|代码|股票代码|证券代码|股票名称|证券简称|名称|股票池|池子|里面|里|中|到|今天|今日|请|帮我|把|这个|这只|一只)", " ", s)
    s = re.sub(r"[，,。；;：:/\\|\[\]（）(){}<>《》【】\"'`]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:30]


def extract_stock_items(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """从对话中提取股票代码/名称。返回 [(code, name)] 和需要按名称解析的 term。"""
    items: List[Tuple[str, str]] = []
    terms: List[str] = []
    seen_codes = set()

    # 优先按行解析，支持：加入 600519 贵州茅台 / 600519,贵州茅台
    lines = [x.strip() for x in re.split(r"[\n\r]+", text) if x.strip()]
    for line in lines:
        codes = list(re.finditer(r"\d{6}", line))
        if not codes:
            continue
        if len(codes) == 1:
            code = normalize_code(codes[0].group(0))
            name = clean_stock_name(line)
            if code not in seen_codes:
                items.append((code, name))
                seen_codes.add(code)
        else:
            # 多个代码在同一行时，先保证代码全部加入；名称可后续用名称表补齐。
            for m in codes:
                code = normalize_code(m.group(0))
                if code not in seen_codes:
                    items.append((code, ""))
                    seen_codes.add(code)

    if not items:
        term = clean_stock_name(text)
        # 去掉常见动词后剩下的内容，如果没有 6 位代码，就当作股票名尝试解析。
        if term and not re.search(r"(生成|扫描|筛选|信号|查看|列表|删除|移除|剔除|清理|整理|帮助|help)", term, re.I):
            terms.append(term)
    return items, terms


def parse_chat_action(text: str) -> ChatAction:
    raw = (text or "").strip()
    t = re.sub(r"\s+", " ", raw.lower())
    add_words = ["加入", "添加", "新增", "加到", "放入", "放进", "add"]
    remove_words = ["删除", "移除", "剔除", "去掉", "踢出", "del", "remove"]
    scan_words = ["生成买入信号", "买入信号", "生成信号", "扫描", "筛选", "跑一下", "跑一遍", "看信号", "主动生成", "signal"]
    list_words = ["查看股票池", "股票池列表", "股票池", "池子里", "多少只", "list"]
    prune_words = ["清理股票池", "整理股票池", "淘汰弱势", "删除图形差", "删除图形较差", "剔除弱势", "prune"]
    explain_words = ["解释", "为什么", "为啥", "原因", "逻辑", "详细", "复盘", "没信号", "没有信号", "为什么没买", "explain"]
    help_words = ["帮助", "怎么用", "help"]

    if any(w in raw for w in add_words) or any(w in t for w in ["add"]):
        items, terms = extract_stock_items(raw)
        return ChatAction("add", raw, items, terms)
    if any(w in raw for w in prune_words) or any(w in t for w in ["prune"]):
        return ChatAction("prune", raw, [], [], auto_prune=True)
    if any(w in raw for w in remove_words) or any(w in t for w in ["del", "remove"]):
        items, terms = extract_stock_items(raw)
        return ChatAction("remove", raw, items, terms)
    if any(w in raw for w in explain_words) or any(w in t for w in ["explain"]):
        items, terms = extract_stock_items(raw)
        auto_prune = any(w in raw for w in prune_words) or ("清理" in raw) or ("淘汰" in raw)
        return ChatAction("explain", raw, items, terms, auto_prune=auto_prune)
    if any(w in raw for w in scan_words) or any(w in t for w in ["signal"]):
        auto_prune = any(w in raw for w in prune_words) or ("清理" in raw) or ("淘汰" in raw)
        return ChatAction("scan", raw, [], [], auto_prune=auto_prune)
    if any(w in raw for w in list_words) or any(w in t for w in ["list"]):
        return ChatAction("list", raw, [], [])
    if any(w in raw for w in help_words) or any(w in t for w in ["help"]):
        return ChatAction("help", raw, [], [])
    return ChatAction("unknown", raw, [], [])


def add_items_to_stock_pool(pool_path: str, raw_items: List[Tuple[str, str]], terms: List[str], cfg: Dict[str, Any], refresh: bool = False) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    fetcher = AkshareFetcher(cfg, refresh=refresh)
    name_map: Dict[str, str] = {}

    resolved: List[Tuple[str, str]] = []
    errors: List[str] = []
    for code, name in raw_items:
        try:
            code = normalize_code(code)
            if not name:
                if not name_map:
                    name_map = fetch_name_map(fetcher)
                name = name_map.get(code, "")
            resolved.append((code, name))
        except Exception as exc:
            errors.append(str(exc))
    for term in terms:
        try:
            if not name_map:
                name_map = fetch_name_map(fetcher)
            resolved.append(resolve_stock_by_name(term, fetcher=fetcher, name_map=name_map))
        except Exception as exc:
            errors.append(str(exc))

    if not resolved:
        return "没有识别到要加入的股票。示例：加入 600519 贵州茅台 到股票池。" + ("\n" + "\n".join(errors) if errors else "")

    before = len(pool)
    existing = set(pool["code"].astype(str)) if not pool.empty else set()
    added, updated, duplicated = [], [], []
    rows = pool.to_dict("records") if not pool.empty else []
    row_by_code = {str(r.get("code", "")): r for r in rows}

    for code, name in resolved:
        if code in existing:
            if name and not str(row_by_code.get(code, {}).get("name", "")).strip():
                row_by_code[code]["name"] = name
                updated.append((code, name))
            else:
                duplicated.append((code, name or row_by_code.get(code, {}).get("name", "")))
            continue
        rows.append({"code": code, "name": name})
        existing.add(code)
        added.append((code, name))

    if added or updated:
        backup_dir = cfg.get("pool", {}).get("backup_dir", "pool_backups")
        backup_stock_pool(pool_path, backup_dir)
        write_stock_pool(pd.DataFrame(rows), pool_path)

    after = len(read_stock_pool_or_empty(pool_path))
    parts = [f"股票池更新完成：原来 {before} 只，现在 {after} 只。"]
    if added:
        parts.append("已加入：" + "、".join([f"{c} {n}".strip() for c, n in added]))
    if updated:
        parts.append("已补全名称：" + "、".join([f"{c} {n}".strip() for c, n in updated]))
    if duplicated:
        parts.append("已存在，未重复加入：" + "、".join([f"{c} {n}".strip() for c, n in duplicated]))
    if errors:
        parts.append("未处理成功：" + "；".join(errors))
    return "\n".join(parts)


def remove_items_from_stock_pool(pool_path: str, raw_items: List[Tuple[str, str]], terms: List[str], cfg: Dict[str, Any]) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    if pool.empty:
        return "股票池为空，不需要删除。"

    codes = {normalize_code(c) for c, _ in raw_items if re.search(r"\d{6}", str(c))}
    name_terms = [n for _, n in raw_items if n] + terms
    if not codes and not name_terms:
        # 没提取到代码时，退一步从原文清洗出来一个名称。
        fallback = clean_stock_name(" ".join([str(x) for pair in raw_items for x in pair]))
        if fallback:
            name_terms.append(fallback)

    mask = pool["code"].astype(str).isin(codes) if codes else pd.Series(False, index=pool.index)
    for term in name_terms:
        term = str(term).strip()
        if not term:
            continue
        mask = mask | pool["name"].astype(str).str.contains(re.escape(term), na=False)

    removed = pool[mask].copy()
    if removed.empty:
        return "股票池里没有找到要删除的股票。建议发送：删除 600519。"

    backup_dir = cfg.get("pool", {}).get("backup_dir", "pool_backups")
    backup_stock_pool(pool_path, backup_dir)
    remain = pool[~mask].copy().reset_index(drop=True)
    write_stock_pool(remain, pool_path)
    removed_text = "、".join([f"{r.code} {r.name}".strip() for r in removed.itertuples(index=False)])
    return f"已从股票池删除 {len(removed)} 只：{removed_text}\n当前股票池剩余 {len(remain)} 只。"


def format_pool_list(pool_path: str, max_show: int = 80) -> str:
    pool = read_stock_pool_or_empty(pool_path)
    if pool.empty:
        return "股票池为空。你可以发送：加入 600519 贵州茅台 到股票池。"
    lines = [f"股票池当前共 {len(pool)} 只。"]
    show = pool.head(max_show)
    for _, r in show.iterrows():
        lines.append(f"- {r['code']} {r.get('name', '')}".rstrip())
    if len(pool) > max_show:
        lines.append(f"……还有 {len(pool) - max_show} 只未显示。")
    return "\n".join(lines)


def help_message() -> str:
    return """小龙虾可用命令示例：
1) 加入股票：加入 600519 贵州茅台 到股票池
2) 按名称加入：加入 贵州茅台 到股票池（名称重名时会提示改用代码）
3) 删除股票：删除 600519
4) 查看股票池：查看股票池
5) 手动生成信号：生成买入信号
6) 手动扫描并清理弱势股：生成买入信号并清理股票池
7) 查看详细解释：解释今天为什么没有信号 / 为什么没买 600519

定时任务建议使用：python main.py --pool stock_pool.csv --config config.example.yml --out output --account 100000 --tail --auto-prune --send ...
默认只输出精简摘要；要看逐股原因，先生成信号后再问：解释今天为什么没有信号 / 为什么没买 600519。
""".strip()


def fmt_decimal_pct(x: Any, digits: int = 1, empty: str = "") -> str:
    """把 0.123 这种收益率/仓位格式化为 12.3%。"""
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}%}"


def fmt_point_pct(x: Any, digits: int = 1, empty: str = "") -> str:
    """把 AkShare 的 2.3 这种涨跌幅点数格式化为 2.3%。"""
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}%"


def fmt_price(x: Any, digits: int = 2, empty: str = "") -> str:
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}"


def fmt_ratio(x: Any, digits: int = 2, empty: str = "") -> str:
    v = safe_float(x)
    if not np.isfinite(v):
        return empty
    return f"{v:.{digits}f}倍"


def unique_nonempty(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        s = str(item or "").strip().strip("；;，, ")
        if not s or s.lower() == "nan":
            continue
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def split_reason_text(text: Any) -> List[str]:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return []
    return unique_nonempty(re.split(r"[；;]", str(text)))


def build_metric_snapshot(r: pd.Series, compact: bool = False) -> str:
    close = fmt_price(r.get("close"))
    pct_chg = fmt_point_pct(r.get("pct_chg"))
    ret3 = fmt_decimal_pct(r.get("ret3"))
    ret5 = fmt_decimal_pct(r.get("ret5"))
    ret20 = fmt_decimal_pct(r.get("ret20"))
    ret60 = fmt_decimal_pct(r.get("ret60"))
    ret120 = fmt_decimal_pct(r.get("ret120"))
    rs60 = fmt_decimal_pct(r.get("rs_rank60"), 0)
    dd10 = fmt_decimal_pct(r.get("drawdown10"))
    dd20 = fmt_decimal_pct(r.get("drawdown20"))
    dd120 = fmt_decimal_pct(r.get("drawdown120"))
    risk_gate_reason = str(r.get("risk_gate_reason", "")).strip()
    ldc6 = safe_float(r.get("limit_down_count_6"), 0.0)
    amount_ratio = fmt_ratio(r.get("amount_ratio20"))
    risk_pct = fmt_decimal_pct(r.get("risk_pct"))
    stop_loss = fmt_price(r.get("stop_loss"))
    ma20 = fmt_price(r.get("ma20"))
    ma60 = fmt_price(r.get("ma60"))
    close_pos60 = fmt_decimal_pct(r.get("close_pos60"), 0)
    contraction = fmt_ratio(r.get("range_contraction_20_60"))
    setup_tags = str(r.get("setup_tags", "")).strip()
    provider = str(r.get("data_provider", "")).strip()

    if compact:
        parts = [
            f"收盘{close}" if close else "",
            f"涨跌{pct_chg}" if pct_chg else "",
            f"5日{ret5}" if ret5 else "",
            f"60日{ret60}" if ret60 else "",
            f"RS{rs60}" if rs60 else "",
            f"近6日跌停{int(ldc6)}次" if ldc6 else "",
            f"位置{close_pos60}" if close_pos60 else "",
            f"量比{amount_ratio}" if amount_ratio else "",
            f"止损{risk_pct}" if risk_pct else "",
        ]
        return "，".join(unique_nonempty(parts))

    parts = [
        f"收盘{close}" if close else "",
        f"当日涨跌{pct_chg}" if pct_chg else "",
        f"3/5/20/60/120日收益={ret3}/{ret5}/{ret20}/{ret60}/{ret120}" if (ret3 or ret5 or ret20 or ret60 or ret120) else "",
        f"股票池内60日强度排名={rs60}" if rs60 else "",
        f"近6日跌停/近跌停={int(ldc6)}次" if ldc6 else "",
        f"风险闸门={risk_gate_reason}" if risk_gate_reason else "",
        f"MA20={ma20}，MA60={ma60}" if (ma20 or ma60) else "",
        f"距10/20/120日高点={dd10}/{dd20}/{dd120}" if (dd10 or dd20 or dd120) else "",
        f"60日区间位置={close_pos60}" if close_pos60 else "",
        f"20/60日波动收敛={contraction}" if contraction else "",
        f"成交额/20日均额={amount_ratio}" if amount_ratio else "",
        f"形态标签={setup_tags}" if setup_tags else "",
        f"K线数据源={provider}" if provider else "",
        f"计划止损={stop_loss}，止损幅度={risk_pct}" if (stop_loss or risk_pct) else "",
    ]
    return "；".join(unique_nonempty(parts))


def add_decision_explanations(
    candidates: pd.DataFrame,
    allocated: pd.DataFrame,
    market: MarketState,
    cfg: Dict[str, Any],
    account: float = 0.0,
) -> pd.DataFrame:
    """给每只股票加上“买/不买”的可解释字段。"""
    if candidates is None or candidates.empty:
        return candidates

    df = candidates.copy()
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    max_positions = int(cfg["strategy"].get("max_positions", 5))

    allocated_codes = set()
    alloc_by_code: Dict[str, Dict[str, Any]] = {}
    if allocated is not None and not allocated.empty and "code" in allocated.columns:
        for _, ar in allocated.iterrows():
            code = str(ar.get("code", "")).zfill(6)
            allocated_codes.add(code)
            alloc_by_code[code] = ar.to_dict()

    decisions: List[str] = []
    decision_reasons: List[str] = []
    positive_factors_all: List[str] = []
    negative_factors_all: List[str] = []
    metric_snapshots: List[str] = []
    compact_snapshots: List[str] = []
    buy_logic_all: List[str] = []

    for _, r in df.iterrows():
        code = str(r.get("code", "")).zfill(6)
        score = safe_float(r.get("score"), 0.0)
        ok_base = bool(r.get("ok_base", False))
        is_signal = bool(r.get("is_signal", False))
        filter_reasons = split_reason_text(r.get("filter_reason", ""))
        score_weaknesses = split_reason_text(r.get("score_weakness", ""))
        positives = split_reason_text(r.get("reason", ""))
        block_reasons: List[str] = []

        if filter_reasons:
            block_reasons.extend(filter_reasons)
        if score < threshold:
            block_reasons.append(f"综合分{score:.1f}低于阈值{threshold:.1f}")
        if score_weaknesses:
            block_reasons.extend(score_weaknesses)
        block_reasons = unique_nonempty(block_reasons)

        if code in allocated_codes:
            alloc = alloc_by_code.get(code, {})
            target_weight = safe_float(alloc.get("target_weight"), 0.0)
            target_shares = safe_float(alloc.get("target_shares"), 0.0)
            if target_weight > 0 and target_shares > 0:
                decision = "买入"
            elif target_weight > 0:
                decision = "买入信号但不足一手"
                block_reasons.append("按账户金额和一手100股规则计算，不足一手")
            else:
                decision = "买入信号但仓位为0"
                block_reasons.append("大盘/风险预算导致仓位为0")
            reason = f"通过硬过滤，综合分{score:.1f}≥{threshold:.1f}；" + ("；".join(positives) if positives else "形态评分达标")
        elif is_signal:
            decision = "达标未配置"
            reason = f"达到买入信号，但本策略最多配置前{max_positions}只；本票没有进入最终仓位列表"
            block_reasons.append(reason)
        else:
            decision = "不买"
            if not ok_base:
                reason = "未通过硬过滤：" + ("；".join(filter_reasons) if filter_reasons else "基础条件不足")
            elif score < threshold:
                reason = f"硬过滤通过，但综合分{score:.1f}低于阈值{threshold:.1f}"
            else:
                reason = "未形成最终买入信号"

        snapshot = build_metric_snapshot(r, compact=False)
        compact_snapshot = build_metric_snapshot(r, compact=True)
        score_detail = str(r.get("score_detail", "")).strip()
        positive_text = "；".join(unique_nonempty(positives)) or "暂无明显加分项"
        negative_text = "；".join(unique_nonempty(block_reasons)) or "无明显阻断项"

        if decision.startswith("买入"):
            logic = f"{decision}：{reason}。评分拆解：{score_detail}。关键指标：{snapshot}。"
        elif decision == "达标未配置":
            logic = f"暂不配置：{reason}。评分拆解：{score_detail}。关键指标：{snapshot}。"
        else:
            logic = f"不买：{reason}。主要阻断：{negative_text}。评分拆解：{score_detail}。关键指标：{snapshot}。"

        decisions.append(decision)
        decision_reasons.append(reason)
        positive_factors_all.append(positive_text)
        negative_factors_all.append(negative_text)
        metric_snapshots.append(snapshot)
        compact_snapshots.append(compact_snapshot)
        buy_logic_all.append(logic)

    df["decision"] = decisions
    df["decision_reason"] = decision_reasons
    df["positive_factors"] = positive_factors_all
    df["negative_factors"] = negative_factors_all
    df["metric_snapshot"] = metric_snapshots
    df["compact_snapshot"] = compact_snapshots
    df["buy_logic"] = buy_logic_all
    return df


def attach_explanations_to_allocated(allocated: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    if allocated is None or allocated.empty or candidates is None or candidates.empty:
        return allocated
    out = allocated.copy()
    explain_cols = [
        "decision", "decision_reason", "positive_factors", "negative_factors", "metric_snapshot",
        "compact_snapshot", "buy_logic", "score_detail", "score_weakness",
        "trend_score", "momentum_score", "breakout_score", "risk_score",
    ]
    lookup = candidates[["code"] + [c for c in explain_cols if c in candidates.columns]].copy()
    lookup["code"] = lookup["code"].astype(str).str.zfill(6)
    out["code"] = out["code"].astype(str).str.zfill(6)
    for col in explain_cols:
        if col in out.columns:
            out = out.drop(columns=[col])
    return out.merge(lookup, on="code", how="left")


def blocker_counts(candidates: pd.DataFrame, cfg: Dict[str, Any], top_n: int = 8) -> List[Tuple[str, int]]:
    if candidates is None or candidates.empty:
        return []
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    counts: Dict[str, int] = {}
    for _, r in candidates.iterrows():
        if bool(r.get("is_signal", False)):
            continue
        reasons = []
        reasons.extend(split_reason_text(r.get("filter_reason", "")))
        if safe_float(r.get("score"), 0.0) < threshold:
            reasons.append("综合分未达阈值")
        reasons.extend(split_reason_text(r.get("score_weakness", ""))[:3])
        for reason in unique_nonempty(reasons):
            counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:top_n]


def format_blocker_counts(candidates: pd.DataFrame, cfg: Dict[str, Any], top_n: int = 8) -> str:
    rows = blocker_counts(candidates, cfg, top_n=top_n)
    if not rows:
        return ""
    return "；".join([f"{reason}×{count}" for reason, count in rows])


def rank_no_signal_rows(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    df = candidates.copy()
    if "decision" not in df.columns:
        df["decision"] = np.where(df.get("is_signal", False), "买入", "不买")
    df = df[~df["decision"].astype(str).str.startswith("买入")].copy()
    if df.empty:
        return df
    for c in ["score", "rs_rank60", "ret60", "amount_ratio20"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = np.nan
    return df.sort_values(["score", "rs_rank60", "ret60", "amount_ratio20"], ascending=[False, False, False, False])


def format_one_stock_explanation(r: pd.Series, detail: bool = True) -> str:
    code = str(r.get("code", "")).zfill(6)
    name = str(r.get("name", "")).strip()
    decision = str(r.get("decision", "")).strip() or ("买入" if bool(r.get("is_signal", False)) else "不买")
    score = safe_float(r.get("score"), 0.0)
    line1 = f"{code} {name}：{decision}，综合分{score:.1f}。"
    reason = str(r.get("decision_reason", "")).strip()
    compact = str(r.get("compact_snapshot", "")).strip()
    if not detail:
        tail = "；".join(unique_nonempty([reason, compact]))
        return line1 + (tail if tail else "")
    positives = str(r.get("positive_factors", "")).strip()
    negatives = str(r.get("negative_factors", "")).strip()
    score_detail = str(r.get("score_detail", "")).strip()
    snapshot = str(r.get("metric_snapshot", "")).strip()
    parts = [
        line1,
        f"结论原因：{reason}" if reason else "",
        f"加分项：{positives}" if positives else "",
        f"阻断/扣分项：{negatives}" if negatives else "",
        f"评分拆解：{score_detail}" if score_detail else "",
        f"关键指标：{snapshot}" if snapshot else "",
    ]
    return "\n".join(unique_nonempty(parts))


def filter_explanations_for_action(candidates: pd.DataFrame, action: ChatAction) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    df = candidates.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    codes = {normalize_code(c) for c, _ in action.items if re.search(r"\d{6}", str(c))}
    terms = [str(n).strip() for _, n in action.items if str(n).strip()] + [str(t).strip() for t in action.terms if str(t).strip()]
    mask = pd.Series(False, index=df.index)
    if codes:
        mask = mask | df["code"].isin(codes)
    for term in terms:
        mask = mask | df["name"].astype(str).str.contains(re.escape(term), na=False)
    return df[mask].copy() if mask.any() else pd.DataFrame()


def format_detailed_report(
    candidates: pd.DataFrame,
    signals: pd.DataFrame,
    market: MarketState,
    cfg: Dict[str, Any],
    account: float,
    prune_report: Optional[PruneReport] = None,
) -> str:
    lines: List[str] = []
    threshold = float(cfg["strategy"].get("score_threshold", 70))
    max_positions = int(cfg["strategy"].get("max_positions", 5))
    report_cfg = cfg.get("report", {})
    max_report_stocks = int(report_cfg.get("max_report_stocks", 120))

    lines.append(f"# A股K线策略逐股解释报告（{now_cn().strftime('%Y-%m-%d %H:%M')}）")
    lines.append("")
    lines.append("## 1. 今日大盘过滤")
    lines.append(f"- {market.summary}")
    if market.details is not None and not market.details.empty:
        for _, mr in market.details.iterrows():
            lines.append(
                f"- {mr.get('symbol', '')}：分数{safe_float(mr.get('score'), 0):.1f}，"
                f"20日收益{fmt_decimal_pct(mr.get('ret20'))}，60日收益{fmt_decimal_pct(mr.get('ret60'))}，"
                f"{'收盘站上20日线' if bool(mr.get('close_gt_ma20', False)) else '未站上20日线'}，"
                f"{'收盘站上60日线' if bool(mr.get('close_gt_ma60', False)) else '未站上60日线'}"
            )
    lines.append("")
    lines.append("## 2. 策略判定规则")
    lines.append(f"- 先看大盘：不是只看强弱，而是综合指数均线结构、回归趋势效率、位置、波动收敛、K线实体/上影线等盘面图形。大盘弱势时不新开仓；中性/强势时才允许配置，总仓位由盘面状态控制。")
    lines.append(
        "- 再看硬过滤：非ST、价格区间合适、20日均成交额达标、当日不追过大涨幅、不接过大跌幅、上影线不过长、趋势/图形结构达标、60日收益强于大盘、止损距离不过宽。"
    )
    lines.append(f"- 最后看评分：趋势模板35分 + 动量/相对强弱25分 + 形态/量能25分 + 风险结构15分；综合分 ≥ {threshold:.1f} 才算买入候选。")
    lines.append(f"- 仓位配置：候选达标后最多取前{max_positions}只，再用 ATR/均线止损距离反推目标仓位，单票亏损风险由配置里的 risk_per_trade_pct 控制。")
    lines.append("")

    n_total = len(candidates) if candidates is not None else 0
    n_buy = 0 if signals is None or signals.empty else len(signals)
    n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if candidates is not None and not candidates.empty and "is_signal" in candidates.columns else n_buy
    lines.append("## 3. 今日总体结果")
    lines.append(f"- 股票池扫描 {n_total} 只；达到买入候选 {n_signal_all} 只；最终配置 {n_buy} 只。")
    blocker_text = format_blocker_counts(candidates, cfg, top_n=10)
    if blocker_text:
        lines.append(f"- 未买入主要原因：{blocker_text}")
    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append(f"- 股票池维护：{prune_text}")
    lines.append("")

    lines.append("## 4. 买入/配置标的解释")
    if signals is None or signals.empty:
        lines.append("- 今日没有最终买入配置。原因通常是大盘过滤、硬过滤未通过、综合分不达阈值，或仓位/名额限制。")
    else:
        for _, r in signals.iterrows():
            lines.append(format_one_stock_explanation(r, detail=True))
            lines.append("")

    lines.append("## 5. 未买入标的逐股解释")
    no_sig = rank_no_signal_rows(candidates)
    if no_sig.empty:
        lines.append("- 没有未买入标的。")
    else:
        show = no_sig.head(max_report_stocks)
        for _, r in show.iterrows():
            lines.append(format_one_stock_explanation(r, detail=True))
            lines.append("")
        if len(no_sig) > len(show):
            lines.append(f"- 还有 {len(no_sig) - len(show)} 只未写入文本报告，可在 latest_explanations.csv 查看。")

    lines.append("\n> 本脚本只生成信号和风控参考，不自动下单；执行前仍需你确认流动性、公告、财报和个人风险承受能力。")
    return "\n".join(lines).strip() + "\n"


def prune_stock_pool_by_candidates(
    pool_path: str,
    full_pool: pd.DataFrame,
    candidates: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: Dict[str, Any],
    out_path: Path,
    run_date: str,
    skipped_reason: str = "",
) -> PruneReport:
    p_cfg = cfg.get("pool", {})
    max_size = int(p_cfg.get("max_size", 50))
    prune_count = int(p_cfg.get("prune_count", 10))
    backup_dir = p_cfg.get("backup_dir", "pool_backups")
    size_before = len(full_pool) if full_pool is not None else 0

    if skipped_reason:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), f"股票池自动淘汰已跳过：{skipped_reason}")
    if size_before <= max_size:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), f"股票池 {size_before} 只，未超过 {max_size}，不淘汰。")
    if candidates is None or candidates.empty:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "股票池超过阈值，但候选评分为空，本次不淘汰。")

    cand = candidates.copy()
    if "code" not in cand.columns:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "候选结果缺少股票代码，本次不淘汰。")
    cand["code"] = cand["code"].astype(str).str.zfill(6)
    pool_codes = set(full_pool["code"].astype(str).str.zfill(6))
    signal_codes = set()
    if signals is not None and not signals.empty and "code" in signals.columns:
        signal_codes = set(signals["code"].astype(str).str.zfill(6))

    cand = cand[cand["code"].isin(pool_codes) & (~cand["code"].isin(signal_codes))].copy()
    if "filter_reason" in cand.columns:
        cand = cand[~cand["filter_reason"].astype(str).str.contains("数据错误", na=False)].copy()
    if cand.empty:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "股票池超过阈值，但除买入信号/数据错误股票外没有可淘汰标的。")

    for c in ["score", "ret20", "ret60", "ret120", "drawdown120", "ma20_slope10", "ma60_slope20"]:
        if c in cand.columns:
            cand[c] = pd.to_numeric(cand[c], errors="coerce")
        else:
            cand[c] = np.nan
    if "filter_reason" not in cand.columns:
        cand["filter_reason"] = ""
    if "reason" not in cand.columns:
        cand["reason"] = ""
    if "name" not in cand.columns:
        cand["name"] = ""

    reason_text = cand["filter_reason"].astype(str)
    # 大盘弱势会让所有股票都被过滤，不应单独作为淘汰依据；个股趋势/相对强度才是“图形差”的核心。
    cand["bad_trend_penalty"] = reason_text.str.contains("趋势未达标", na=False).astype(int) * 12
    cand["bad_rs_penalty"] = reason_text.str.contains("相对强度不足", na=False).astype(int) * 8
    cand["bad_liq_penalty"] = reason_text.str.contains("成交额不足", na=False).astype(int) * 3
    cand["invalid_penalty"] = reason_text.str.contains("历史K线不足|收盘价无效", na=False).astype(int) * 30
    cand["prune_score"] = cand["score"].fillna(0) - cand["bad_trend_penalty"] - cand["bad_rs_penalty"] - cand["bad_liq_penalty"] - cand["invalid_penalty"]
    cand["ret60_prune"] = cand["ret60"].fillna(-9)
    cand["ret120_prune"] = cand["ret120"].fillna(-9)
    cand["drawdown_prune"] = cand["drawdown120"].fillna(-9)

    n_remove = min(prune_count, len(cand))
    removed = cand.sort_values(
        ["prune_score", "ret60_prune", "ret120_prune", "drawdown_prune"],
        ascending=[True, True, True, True],
    ).head(n_remove).copy()
    remove_codes = set(removed["code"].astype(str).str.zfill(6))
    if not remove_codes:
        return PruneReport(True, False, size_before, size_before, pd.DataFrame(), "没有可淘汰标的。")

    backup_path = backup_stock_pool(pool_path, backup_dir)
    new_pool = full_pool[~full_pool["code"].astype(str).str.zfill(6).isin(remove_codes)].copy().reset_index(drop=True)
    write_stock_pool(new_pool, pool_path)
    size_after = len(new_pool)

    removed = removed.copy()
    removed["淘汰原因"] = removed.apply(
        lambda r: str(r.get("filter_reason", "")).replace("大盘弱势禁止新开仓", "").strip("；") or str(r.get("reason", "")) or "综合评分靠后",
        axis=1,
    )
    removed_path = out_path / f"pruned_{run_date}.csv"
    latest_removed_path = out_path / "latest_pruned.csv"
    to_chinese_columns(removed).to_csv(removed_path, index=False, encoding="utf-8-sig")
    to_chinese_columns(removed).to_csv(latest_removed_path, index=False, encoding="utf-8-sig")
    write_stock_pool(new_pool, out_path / "latest_pool.csv")

    show = "、".join([f"{r.code} {r.name}".strip() for r in removed[["code", "name"]].itertuples(index=False)])
    msg = f"股票池超过 {max_size} 只，已自动淘汰图形较差的 {len(removed)} 只；{size_before} → {size_after}。淘汰：{show}"
    return PruneReport(True, True, size_before, size_after, removed, msg, backup_path=backup_path)


def format_prune_report(report: Optional[PruneReport]) -> str:
    if report is None or not report.enabled:
        return ""
    return report.message or ""


def read_latest_candidates_raw(out_path: Path) -> pd.DataFrame:
    raw_path = out_path / "latest_explanations_raw.csv"
    if raw_path.exists():
        try:
            df = pd.read_csv(raw_path, dtype={"code": str})
            if "code" in df.columns:
                df["code"] = df["code"].astype(str).str.zfill(6)
            return df
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def explain_from_latest_output(action: ChatAction, out_path: Path) -> str:
    candidates = read_latest_candidates_raw(out_path)
    if not candidates.empty:
        selected = filter_explanations_for_action(candidates, action)
        if not selected.empty:
            return "\n\n".join([format_one_stock_explanation(r, detail=True) for _, r in selected.iterrows()])
    report_path = out_path / "latest_report.md"
    if report_path.exists():
        return report_path.read_text(encoding="utf-8")
    msg_path = out_path / "latest_message.txt"
    if msg_path.exists():
        return msg_path.read_text(encoding="utf-8")
    return "还没有找到最近一次扫描结果。请先发送：生成买入信号。"


def handle_chat_command(text: str, args: argparse.Namespace, cfg: Dict[str, Any]) -> Tuple[str, Optional[Path]]:
    action = parse_chat_action(text)
    out_path = ensure_dir(args.out)

    if action.kind == "add":
        reply = add_items_to_stock_pool(args.pool, action.items, action.terms, cfg, refresh=args.refresh)
    elif action.kind == "remove":
        reply = remove_items_from_stock_pool(args.pool, action.items, action.terms, cfg)
    elif action.kind == "list":
        reply = format_pool_list(args.pool)
    elif action.kind in {"scan", "prune"}:
        manual_prune = action.auto_prune or (args.auto_prune is True)
        signals, candidates, market, msg_path, _ = scan(
            args.pool,
            cfg,
            args.out,
            args.account,
            refresh=args.refresh,
            limit=args.limit,
            auto_prune=manual_prune,
        )
        reply = msg_path.read_text(encoding="utf-8")
    elif action.kind == "explain":
        # 默认不重新拉行情：读取最近一次扫描生成的 latest_explanations_raw.csv / latest_report.md。
        # 这样你可以先“生成买入信号”，再按需“解释原因”，不会因为查看解释又触发一轮数据源限流。
        reply = explain_from_latest_output(action, out_path)
    elif action.kind == "help":
        reply = help_message()
    else:
        reply = "我没有识别这个命令。\n\n" + help_message()

    reply_path = out_path / "latest_chat_reply.txt"
    reply_path.write_text(reply, encoding="utf-8")
    return reply, reply_path



def prefilter_pool_for_stability(pool: pd.DataFrame, stock_spot: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, str]:
    """股票池过大时先用实时行情做轻量预筛，避免 100+ 全量历史K线扫描被系统 kill。

    这是稳定性策略：它不删除股票池，只决定本次优先扫描哪些股票。
    """
    data_cfg = cfg.get("data", {})
    if not bool(data_cfg.get("two_stage_scan", True)) or pool.empty:
        return pool, ""
    trigger = int(data_cfg.get("prefilter_pool_when_gt", 60) or 0)
    max_scan = int(data_cfg.get("max_scan_per_run", 45) or 0)
    if trigger <= 0 or max_scan <= 0 or len(pool) <= trigger:
        return pool, ""
    out = pool.copy()
    if stock_spot is not None and not stock_spot.empty and "代码" in stock_spot.columns:
        spot = stock_spot.copy()
        spot["code"] = spot["代码"].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
        cols = [c for c in ["code", "最新价", "涨跌幅", "成交额", "换手率", "名称"] if c in spot.columns]
        out = out.merge(spot[cols], on="code", how="left")
        pct = pd.to_numeric(out.get("涨跌幅"), errors="coerce")
        amount = pd.to_numeric(out.get("成交额"), errors="coerce")
        turnover = pd.to_numeric(out.get("换手率"), errors="coerce")
        min_pct = float(data_cfg.get("prefilter_keep_pct_chg_min", -6.0))
        max_pct = float(data_cfg.get("prefilter_keep_pct_chg_max", 8.8))
        tradable = pct.between(min_pct, max_pct) | pct.isna()
        # 轻量 rank：优先今天活跃但不过热、成交额高、换手适中的股票。
        pct_score = (100 - (pct.fillna(0).clip(-8, 9) - 3.0).abs() * 8).clip(lower=0)
        amt_score = amount.rank(pct=True).fillna(0.2) * 100
        turn_score = turnover.rank(pct=True).fillna(0.2) * 20
        out["_prefilter_score"] = pct_score + 0.8 * amt_score + turn_score
        out = out[tradable].sort_values("_prefilter_score", ascending=False)
        if out.empty:
            out = pool.copy()
    else:
        # 没有实时行情时，不让程序崩，取前 max_scan 只；用户可用 --limit 或缩池进一步控制。
        out = pool.copy()
    selected = out.head(max_scan)[[c for c in pool.columns if c in out.columns]].copy()
    msg = f"股票池{len(pool)}只，启用两阶段稳定扫描：本次优先扫描{len(selected)}只；未扫描股票不会被删除。"
    return selected, msg


def _snapshot_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg.get("data", {}).get("intraday_snapshot_dir", "cache/intraday_snapshots"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def update_intraday_snapshot_from_spot(code: str, spot_all: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    """用实时行情快照构造稳定的伪分时序列。

    当东方财富/新浪分钟K失效时，10分钟定时调用会不断积累快照，足够判断VWAP近似、日内高低位、弱于快照均线等。
    """
    code = normalize_code(code)
    if spot_all is None or spot_all.empty or "代码" not in spot_all.columns:
        return pd.DataFrame()
    row = spot_all[spot_all["代码"].astype(str).str.zfill(6) == code]
    if row.empty:
        return pd.DataFrame()
    r = row.iloc[0]
    now = now_cn().replace(second=0, microsecond=0)
    def f(col: str, default: float = np.nan) -> float:
        return safe_float(r.get(col), default)
    price = f("最新价")
    if not np.isfinite(price) or price <= 0:
        return pd.DataFrame()
    rec = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "open": f("今开", price),
        "high": f("最高", price),
        "low": f("最低", price),
        "close": price,
        "volume": f("成交量"),
        "amount": f("成交额"),
        "pct_chg": f("涨跌幅"),
    }
    path = _snapshot_dir(cfg) / f"{code}.csv"
    try:
        old = pd.read_csv(path)
    except Exception:
        old = pd.DataFrame()
    df = pd.concat([old, pd.DataFrame([rec])], ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    # 只保留近5天快照，避免文件无限增长。
    cutoff = pd.Timestamp(now.date() - timedelta(days=5))
    df = df.dropna(subset=["datetime", "close"]).drop_duplicates(subset=["datetime"], keep="last")
    df = df[df["datetime"] >= cutoff].sort_values("datetime").reset_index(drop=True)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df


def intraday_buy_confirmation(code: str, spot_all: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """尾盘/盘中买入信号的日内确认。

    免费分钟K源经常失败，所以这里优先使用实时快照构造的伪分时；如果快照不够，也至少用实时价在日内区间的位置做保护。
    """
    if not bool(cfg.get("data", {}).get("intraday_buy_filter", True)):
        return True, "未开启日内确认"
    snap = update_intraday_snapshot_from_spot(code, spot_all, cfg)
    if spot_all is None or spot_all.empty or "代码" not in spot_all.columns:
        return True, "实时行情缺失，跳过日内确认"
    row = spot_all[spot_all["代码"].astype(str).str.zfill(6) == normalize_code(code)]
    if row.empty:
        return True, "实时行情无该股，跳过日内确认"
    r = row.iloc[0]
    price = safe_float(r.get("最新价")); high = safe_float(r.get("最高")); low = safe_float(r.get("最低")); pct = safe_float(r.get("涨跌幅")); amount = safe_float(r.get("成交额"))
    reasons = []
    ok = True
    if np.isfinite(pct) and pct < -1.8:
        ok = False; reasons.append(f"日内跌幅{pct:.2f}%偏弱")
    if np.isfinite(pct) and pct > 8.8:
        ok = False; reasons.append(f"日内涨幅{pct:.2f}%过热，避免追高")
    if np.isfinite(price) and np.isfinite(high) and high > 0 and price / high - 1 < -0.035:
        ok = False; reasons.append("从日内高点回落超过3.5%")
    if np.isfinite(price) and np.isfinite(high) and np.isfinite(low) and high > low:
        pos = (price - low) / (high - low)
        if pos < 0.45:
            ok = False; reasons.append(f"收盘/当前价处于日内区间偏低位置{pos:.0%}")
        else:
            reasons.append(f"日内区间位置{pos:.0%}")
    if not snap.empty and len(snap) >= 3:
        today = now_cn().date()
        td = snap[pd.to_datetime(snap["datetime"]).dt.date == today].copy()
        if len(td) >= 3:
            ma = pd.to_numeric(td["close"], errors="coerce").rolling(3, min_periods=2).mean().iloc[-1]
            if np.isfinite(price) and np.isfinite(ma) and price < ma * 0.995:
                ok = False; reasons.append("弱于盘中快照均线")
            else:
                reasons.append("强于盘中快照均线")
    else:
        reasons.append("分钟源不可用，使用实时快照/日内高低位确认")
    return ok, "；".join(unique_nonempty(reasons))

def scan(
    pool_path: str,
    cfg: Dict[str, Any],
    out_dir: str,
    account: float,
    refresh: bool = False,
    limit: int = 0,
    auto_prune: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, MarketState, Path, Optional[PruneReport]]:
    out_path = ensure_dir(out_dir)
    removed_old = cleanup_output_dir(out_path, cfg)
    if removed_old:
        print(f"[清理] output 已清理历史文件 {len(removed_old)} 个")
    cfg["_active_pool_path"] = pool_path
    cfg["_pool_spot_only"] = bool(cfg.get("data", {}).get("prefer_pool_spot", True))
    fetcher = AkshareFetcher(cfg, refresh=refresh)
    full_pool = enrich_pool_sectors(read_stock_pool(pool_path), pool_path, cfg, fetcher=fetcher)
    pool = full_pool.copy()
    limit_skip_prune_reason = ""
    if limit and limit > 0:
        pool = pool.head(limit).copy()
        limit_skip_prune_reason = "本次使用了 --limit 测试扫描，避免误删未扫描股票"

    if cfg["data"].get("use_realtime_tail") and cfg["data"].get("adjust", "qfq") != "":
        # 实时行情是不复权价，和 qfq/hfq 混用会造成指标跳变。
        cfg["data"]["adjust"] = ""
        print("[提示] 尾盘实时模式已自动改用不复权日K，避免复权历史价与实时价混用。")

    pool_missing_name = pool["name"].astype(str).str.strip().eq("") if "name" in pool.columns else pd.Series(True, index=pool.index)
    full_missing_name = full_pool["name"].astype(str).str.strip().eq("") if "name" in full_pool.columns else pd.Series(True, index=full_pool.index)
    if bool(pool_missing_name.any() or full_missing_name.any()):
        name_map = fetch_name_map(fetcher)
        if name_map:
            pool.loc[pool_missing_name, "name"] = pool.loc[pool_missing_name, "code"].map(name_map).fillna("")
            full_pool.loc[full_missing_name, "name"] = full_pool.loc[full_missing_name, "code"].map(name_map).fillna("")

    market = evaluate_market(fetcher, cfg)
    print(f"[市场] {market.summary}")

    stock_spot = pd.DataFrame()
    # v6.5: 无论是否尾盘实时模式，都尽量先取一次实时行情；用于稳定预筛和日内买点确认。
    try:
        stock_spot = fetcher.stock_spot_all()
    except Exception as exc:
        print(f"[实时行情] 获取失败，继续使用日K扫描：{exc}")
        stock_spot = pd.DataFrame()

    prefilter_msg = ""
    if not limit:
        pool, prefilter_msg = prefilter_pool_for_stability(pool, stock_spot, cfg)
        if prefilter_msg:
            print(f"[预筛] {prefilter_msg}")
            limit_skip_prune_reason = prefilter_msg

    end_date = cfg["data"].get("end_date") or today_yyyymmdd()
    start_date = cfg["data"].get("start_date") or "20220101"
    adjust = cfg["data"].get("adjust", "qfq")

    raw_rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    total = len(pool)
    for n, (_, r) in enumerate(pool.iterrows(), start=1):
        code = r["code"]
        name = r.get("name", "")
        try:
            hist = fetcher.stock_hist(code, start_date, end_date, adjust)
            if cfg["data"].get("use_realtime_tail") and not stock_spot.empty:
                hist = merge_stock_tail_realtime(hist, stock_spot, code)
            metrics = compute_raw_metrics(code, name, hist, cfg, market)
            metrics["sector"] = str(r.get("sector", "未分组") or "未分组")
            raw_rows.append(metrics)
        except Exception as exc:
            errors.append({"code": code, "name": name, "error": str(exc)})
            raw_rows.append({"code": code, "name": name, "sector": str(r.get("sector", "未分组") or "未分组"), "ok_base": False, "filter_reason": f"数据错误：{exc}"})
        if n % 10 == 0 or n == total:
            print(f"[进度] 已扫描 {n}/{total}")

    metrics_df = pd.DataFrame(raw_rows)
    metrics_df = compute_sector_context(metrics_df, cfg, fetcher=fetcher)
    candidates = score_candidates(metrics_df, cfg)
    # 数据源错误率过高时，本次信号只做记录，不应触发买入或自动淘汰。
    data_error_count = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if not candidates.empty and "filter_reason" in candidates.columns else len(errors)
    data_error_rate = (data_error_count / total) if total else 0.0
    max_error_rate = float(cfg.get("data", {}).get("max_error_rate_for_valid_run", 0.20))
    data_quality_skip_reason = ""
    if data_error_rate > max_error_rate:
        data_quality_skip_reason = f"数据源失败率 {data_error_rate:.0%} 超过阈值 {max_error_rate:.0%}，本次结果仅供检查，不执行买入/淘汰"
        print(f"[数据] {data_quality_skip_reason}")
        if not candidates.empty:
            candidates["is_signal"] = False
            candidates["data_quality_warning"] = data_quality_skip_reason
    # v6.5: 对已经达标或接近达标的股票做日内确认，避免日K信号在盘中明显走弱时仍然推送。
    if not candidates.empty and bool(cfg.get("data", {}).get("intraday_buy_filter", True)) and not stock_spot.empty:
        intraday_notes = []
        intraday_oks = []
        threshold = float(cfg["strategy"].get("score_threshold", 68))
        for _, rr in candidates.iterrows():
            code = str(rr.get("code", "")).zfill(6)
            needs_check = bool(rr.get("is_signal", False)) or safe_float(rr.get("score"), 0) >= threshold - 4
            if needs_check:
                ok, note = intraday_buy_confirmation(code, stock_spot, cfg)
            else:
                ok, note = True, "未进入买点候选，不做日内确认"
            intraday_oks.append(ok)
            intraday_notes.append(note)
        candidates["intraday_ok"] = intraday_oks
        candidates["intraday_reason"] = intraday_notes
        weak_mask = candidates["is_signal"].fillna(False) & (~candidates["intraday_ok"].fillna(True))
        if weak_mask.any():
            candidates.loc[weak_mask, "is_signal"] = False
            candidates.loc[weak_mask, "filter_reason"] = candidates.loc[weak_mask, "filter_reason"].astype(str).replace("", np.nan).fillna("日内买点确认未通过") + "；日内买点确认未通过"
    signals = candidates[candidates.get("is_signal", False) == True].copy() if not candidates.empty else pd.DataFrame()
    allocated = allocate_positions(signals, cfg, market, account)
    candidates = add_decision_explanations(candidates, allocated, market, cfg, account=account)
    allocated = attach_explanations_to_allocated(allocated, candidates)

    run_date = now_cn().strftime("%Y%m%d_%H%M%S")
    candidates_path = out_path / f"candidates_{run_date}.csv"
    signals_path = out_path / f"signals_{run_date}.csv"
    explanations_path = out_path / f"explanations_{run_date}.csv"
    report_path = out_path / f"report_{run_date}.md"
    market_path = out_path / f"market_{run_date}.csv"
    errors_path = out_path / f"errors_{run_date}.csv"

    prune_report: Optional[PruneReport] = None
    if auto_prune:
        prune_report = prune_stock_pool_by_candidates(
            pool_path,
            full_pool,
            candidates,
            allocated,
            cfg,
            out_path,
            run_date,
            skipped_reason=limit_skip_prune_reason or data_quality_skip_reason,
        )

    candidates_out = to_chinese_columns(candidates)
    signals_out = to_chinese_columns(allocated)
    explanations_out = to_chinese_columns(candidates)
    candidates_out.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    signals_out.to_csv(signals_path, index=False, encoding="utf-8-sig")
    explanations_out.to_csv(explanations_path, index=False, encoding="utf-8-sig")
    # raw 文件保留英文字段，方便“解释/为什么”对话不重新扫描，直接读取最近一次结果。
    candidates.to_csv(out_path / f"explanations_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / f"signals_raw_{run_date}.csv", index=False, encoding="utf-8-sig")
    market.details.to_csv(market_path, index=False, encoding="utf-8-sig")
    if errors:
        pd.DataFrame(errors).to_csv(errors_path, index=False, encoding="utf-8-sig")

    # 额外写 latest，方便小龙虾/定时任务固定读取
    candidates_out.to_csv(out_path / "latest_candidates.csv", index=False, encoding="utf-8-sig")
    signals_out.to_csv(out_path / "latest_signals.csv", index=False, encoding="utf-8-sig")
    explanations_out.to_csv(out_path / "latest_explanations.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(out_path / "latest_explanations_raw.csv", index=False, encoding="utf-8-sig")
    allocated.to_csv(out_path / "latest_signals_raw.csv", index=False, encoding="utf-8-sig")
    market.details.to_csv(out_path / "latest_market.csv", index=False, encoding="utf-8-sig")
    if prune_report is None:
        # 即使没有启用淘汰，也把最新股票池镜像写到 output，方便外部 Agent 查看。
        write_stock_pool(full_pool, out_path / "latest_pool.csv")

    report = format_detailed_report(candidates, allocated, market, cfg, account, prune_report=prune_report)
    report_path.write_text(report, encoding="utf-8")
    (out_path / "latest_report.md").write_text(report, encoding="utf-8")

    msg = format_message(allocated, market, account, prune_report=prune_report, candidates=candidates, cfg=cfg)
    msg_path = out_path / f"message_{run_date}.txt"
    msg_path.write_text(msg, encoding="utf-8")
    (out_path / "latest_message.txt").write_text(msg, encoding="utf-8")

    print(f"[输出] {signals_path}")
    print(f"[输出] {candidates_path}")
    print(f"[输出] {explanations_path}")
    print(f"[输出] {report_path}")
    if prune_report is not None:
        print(f"[股票池] {prune_report.message}")
    return allocated, candidates, market, msg_path, prune_report

def to_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        columns = ["日期", "股票代码", "股票名称", "判定", "结论原因", "收盘价", "综合分", "评分拆解", "是否买入信号", "目标仓位", "建议买入金额", "建议股数", "止损价", "止损幅度", "止盈1", "止盈2", "60日收益", "相对强度排名", "加分项", "阻断/扣分项", "关键指标", "完整解释", "过滤原因"]
        return pd.DataFrame(columns=columns)
    mapping = {
        "date": "日期",
        "code": "股票代码",
        "name": "股票名称",
        "close": "收盘价",
        "pct_chg": "当日涨跌幅%",
        "score": "综合分",
        "trend_score": "趋势分",
        "momentum_score": "动量相对强弱分",
        "breakout_score": "买点分型分",
        "sector_score": "板块主线分",
        "setup_score": "买点原始分",
        "setup_type": "买点类型",
        "setup_ok": "买点是否达标",
        "setup_reason": "买点依据",
        "setup_blockers": "买点阻断",
        "sector": "板块",
        "sector_strength_score": "板块强度分",
        "sector_is_mainline": "是否主线板块",
        "sector_is_strong": "是否强势板块",
        "sector_ret20": "板块20日收益",
        "sector_ret60": "板块60日收益",
        "sector_up_rate20": "板块20日上涨比例",
        "sector_high_pos_rate": "板块强势区比例",
        "stock_sector_rs20": "个股板块内20日排名",
        "stock_sector_rs60": "个股板块内60日排名",
        "outperform_sector20": "20日超额板块收益",
        "outperform_sector60": "60日超额板块收益",
        "anchor_score": "天量锚点分",
        "anchor_date": "天量锚点日期",
        "anchor_days_ago": "距锚点天数",
        "anchor_reason": "锚点有效依据",
        "anchor_blockers": "锚点阻断",
        "risk_score": "风险结构分",
        "data_provider": "K线数据源",
        "data_warning": "数据提醒",
        "data_quality_warning": "数据质量提醒",
        "setup_tags": "形态标签",
        "ma250": "MA250",
        "ma120_slope40": "120日线斜率40",
        "drawdown250": "距250日高点",
        "close_pos60": "60日区间位置",
        "close_pos120": "120日区间位置",
        "range_contraction_20_60": "20/60日波动收敛",
        "amount_dryup20": "短期缩量比",
        "lower_shadow_pct": "下影线幅度",
        "body_pct": "实体幅度",
        "candle_strength": "K线强度",
        "reg_slope20": "20日回归斜率",
        "reg_r2_20": "20日趋势效率R2",
        "reg_slope60": "60日回归斜率",
        "reg_r2_60": "60日趋势效率R2",
        "score_detail": "评分拆解",
        "score_weakness": "评分扣分点",
        "risk_gate_block": "A股风险闸门",
        "risk_gate_reason": "风险闸门原因",
        "risk_tags": "风险标签",
        "limit_down_count_3": "近3日跌停数",
        "limit_down_count_6": "近6日跌停数",
        "limit_down_count_10": "近10日跌停数",
        "days_since_limit_down": "距最近跌停天数",
        "ret3": "3日收益",
        "ret5": "5日收益",
        "ret10": "10日收益",
        "drawdown10": "距10日高点",
        "drawdown20": "距20日高点",
        "drawdown60": "距60日高点",
        "is_signal": "是否买入信号",
        "decision": "判定",
        "decision_reason": "结论原因",
        "positive_factors": "加分项",
        "negative_factors": "阻断/扣分项",
        "metric_snapshot": "关键指标",
        "compact_snapshot": "关键指标摘要",
        "buy_logic": "完整解释",
        "target_weight": "目标仓位",
        "target_cash": "建议买入金额",
        "target_shares": "建议股数",
        "actual_weight_by_lot": "按整手实际仓位",
        "stop_loss": "止损价",
        "risk_pct": "止损幅度",
        "take_profit_1": "止盈1_1.5R",
        "take_profit_2": "止盈2_3R",
        "ret20": "20日收益",
        "ret60": "60日收益",
        "ret120": "120日收益",
        "rs_rank60": "60日相对强度排名",
        "drawdown120": "距120日高点",
        "amount_ma20": "20日均成交额",
        "amount_ratio20": "成交额/20日均额",
        "atr_pct": "ATR波动率",
        "reason": "原始加分原因",
        "filter_reason": "过滤原因",
        "prune_score": "淘汰评分",
        "淘汰原因": "淘汰原因",
    }
    preferred = [
        "date", "code", "name", "sector", "decision", "decision_reason", "close", "pct_chg", "score", "score_detail",
        "trend_score", "momentum_score", "sector_score", "breakout_score", "setup_type", "setup_score", "setup_ok", "signal_grade", "risk_score", "is_signal", "target_weight", "target_cash",
        "target_shares", "actual_weight_by_lot", "stop_loss", "risk_pct", "take_profit_1", "take_profit_2",
        "risk_gate_block", "risk_gate_reason", "risk_tags", "limit_down_count_3", "limit_down_count_6", "limit_down_count_10", "days_since_limit_down",
        "sector_strength_score", "sector_strength_source", "sector_is_mainline", "sector_is_strong", "sector_ret20", "sector_ret60", "sector_up_rate20", "sector_high_pos_rate", "stock_sector_rs20", "stock_sector_rs60", "outperform_sector20", "outperform_sector60",
        "setup_reason", "setup_blockers", "anchor_score", "anchor_date", "anchor_days_ago", "anchor_reason", "anchor_blockers",
        "ret3", "ret5", "ret10", "ret20", "ret60", "ret120", "rs_rank60", "drawdown10", "drawdown20", "drawdown60", "drawdown120", "drawdown250", "close_pos60", "close_pos120",
        "range_contraction_20_60", "amount_ma20", "amount_ratio20", "amount_dryup20", "atr_pct",
        "data_provider", "data_warning", "data_quality_warning", "setup_tags",
        "positive_factors", "negative_factors", "metric_snapshot", "buy_logic", "reason", "score_weakness", "filter_reason", "淘汰原因", "prune_score",
    ]
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    out = df[cols].copy().rename(columns=mapping)
    for c in ["目标仓位", "按整手实际仓位", "止损幅度", "3日收益", "5日收益", "10日收益", "20日收益", "60日收益", "120日收益", "60日相对强度排名", "距10日高点", "距20日高点", "距60日高点", "距120日高点", "距250日高点", "ATR波动率", "20/60日波动收敛", "短期缩量比", "下影线幅度", "实体幅度", "20日回归斜率", "60日回归斜率", "板块20日收益", "板块60日收益", "板块20日上涨比例", "板块强势区比例", "个股板块内20日排名", "个股板块内60日排名", "20日超额板块收益", "60日超额板块收益"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2%}" if pd.notna(x) else "")
    for c in ["收盘价", "止损价", "止盈1_1.5R", "止盈2_3R"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    for c in ["综合分", "趋势分", "动量相对强弱分", "买点分型分", "板块主线分", "买点原始分", "风险结构分", "板块强度分", "天量锚点分"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
    for c in ["建议买入金额", "20日均成交额"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    return out


def format_compact_message(
    signals: pd.DataFrame,
    market: MarketState,
    account: float,
    prune_report: Optional[PruneReport] = None,
    candidates: Optional[pd.DataFrame] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = cfg or DEFAULT_CONFIG
    lines: List[str] = []
    lines.append(f"A股K线策略信号 {now_cn().strftime('%Y-%m-%d %H:%M')}")
    lines.append(market.summary)
    lines.append("模型：大盘模式 + 板块主线硬门槛 + 个股板块前排 + 突破/回踩/天量锚点分型 + ATR风控仓位")
    if candidates is not None and not candidates.empty:
        n_total = len(candidates)
        n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if "is_signal" in candidates.columns else 0
        n_error = int(candidates["filter_reason"].astype(str).str.contains("数据错误", na=False).sum()) if "filter_reason" in candidates.columns else 0
        error_note = f"；数据失败 {n_error} 只" if n_error else ""
        lines.append(f"股票池扫描：{n_total}只；买入候选：{n_signal_all}只；最终配置：{0 if signals is None or signals.empty else len(signals)}只{error_note}。")
    lines.append("")
    if signals is None or signals.empty:
        lines.append("今日无最终买入配置。逐股原因已生成，但默认不在推送里展开。")
        lines.append("需要看原因时，对小龙虾说：解释今天为什么没有信号；或：为什么没买 600519。")
    else:
        total_weight = float(signals["target_weight"].sum()) if "target_weight" in signals.columns else 0.0
        lines.append(f"买入信号 {len(signals)} 只，建议合计仓位约 {total_weight:.0%}：")
        for _, r in signals.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            close = safe_float(r.get("close"))
            score = safe_float(r.get("score"))
            w = safe_float(r.get("target_weight"), 0)
            stop = safe_float(r.get("stop_loss"))
            shares = r.get("target_shares", "")
            cash = safe_float(r.get("target_cash"), np.nan)
            if account and account > 0 and np.isfinite(cash):
                pos_text = f"仓位{w:.1%}，约{cash:,.0f}元，{shares}股"
            else:
                pos_text = f"仓位{w:.1%}"
            lines.append(f"- {code} {name}：收盘{close:.2f}，分数{score:.1f}，{pos_text}，止损{stop:.2f}")
        lines.append("需要看买入逻辑时，对小龙虾说：解释今天买入逻辑；或：解释 600519。")

    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append("")
        lines.append("股票池维护：" + prune_text)
    lines.append("")
    lines.append("已生成：latest_signals.csv、latest_candidates.csv、latest_explanations.csv、latest_report.md。")
    lines.append("提示：本脚本只给信号和风控参考，不自动下单。")
    return "\n".join(lines)


def format_message(
    signals: pd.DataFrame,
    market: MarketState,
    account: float,
    prune_report: Optional[PruneReport] = None,
    candidates: Optional[pd.DataFrame] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = cfg or DEFAULT_CONFIG
    if not cfg.get("report", {}).get("include_explanations_in_message", False):
        return format_compact_message(signals, market, account, prune_report=prune_report, candidates=candidates, cfg=cfg)
    lines: List[str] = []
    report_cfg = cfg.get("report", {})
    show_no_signal_top = int(report_cfg.get("show_no_signal_top", 12))
    threshold = float(cfg["strategy"].get("score_threshold", 70))

    lines.append(f"A股K线策略信号 {now_cn().strftime('%Y-%m-%d %H:%M')}")
    lines.append(market.summary)
    lines.append("策略：大盘过滤 + 个股均线多头 + 相对强弱 + 阶段新高/量能 + ATR止损仓位")

    if candidates is not None and not candidates.empty:
        n_total = len(candidates)
        n_signal_all = int(candidates["is_signal"].fillna(False).sum()) if "is_signal" in candidates.columns else 0
        blocker_text = format_blocker_counts(candidates, cfg, top_n=6) if report_cfg.get("show_blocker_counts", True) else ""
        lines.append(f"股票池扫描：{n_total}只；达到买入候选：{n_signal_all}只；最终配置：{0 if signals is None or signals.empty else len(signals)}只。")
        if blocker_text:
            lines.append(f"未买入主要原因Top：{blocker_text}")

    lines.append("")
    if signals is None or signals.empty:
        lines.append("今日没有新的买入信号。不是简单地“没信号”，而是下面这些规则没有同时满足。")
        if market.target_exposure <= 0:
            lines.append(f"- 大盘过滤：{market.summary}，策略禁止新开仓。")
        if candidates is not None and not candidates.empty:
            near = rank_no_signal_rows(candidates).head(show_no_signal_top)
            if not near.empty:
                lines.append(f"- 最接近但仍未买入的 {len(near)} 只：")
                for _, r in near.iterrows():
                    lines.append("  - " + format_one_stock_explanation(r, detail=False))
            else:
                lines.append("- 股票池里没有接近达标的候选。")
    else:
        total_weight = float(signals["target_weight"].sum()) if "target_weight" in signals.columns else 0.0
        lines.append(f"买入信号 {len(signals)} 只，建议合计仓位约 {total_weight:.0%}")
        for _, r in signals.iterrows():
            code = str(r.get("code", ""))
            name = str(r.get("name", ""))
            close = safe_float(r.get("close"))
            score = safe_float(r.get("score"))
            w = safe_float(r.get("target_weight"), 0)
            stop = safe_float(r.get("stop_loss"))
            tp1 = safe_float(r.get("take_profit_1"))
            shares = r.get("target_shares", "")
            cash = safe_float(r.get("target_cash"), np.nan)
            decision_reason = str(r.get("decision_reason", "")).strip()
            score_detail = str(r.get("score_detail", "")).strip()
            compact = str(r.get("compact_snapshot", "")).strip()
            if account and account > 0 and np.isfinite(cash):
                pos_text = f"仓位{w:.1%}，约{cash:,.0f}元，{shares}股"
            else:
                pos_text = f"仓位{w:.1%}"
            lines.append(f"- {code} {name}：收盘{close:.2f}，分数{score:.1f}，{pos_text}，止损{stop:.2f}，止盈1 {tp1:.2f}")
            if decision_reason:
                lines.append(f"  买入逻辑：{decision_reason}")
            if score_detail:
                lines.append(f"  评分拆解：{score_detail}")
            if compact:
                lines.append(f"  关键指标：{compact}")

        if candidates is not None and not candidates.empty:
            near = rank_no_signal_rows(candidates).head(show_no_signal_top)
            if not near.empty:
                lines.append("")
                lines.append(f"其他未买入股票的主要原因，展示最接近的 {len(near)} 只：")
                for _, r in near.iterrows():
                    lines.append("  - " + format_one_stock_explanation(r, detail=False))

    prune_text = format_prune_report(prune_report)
    if prune_text:
        lines.append("")
        lines.append("股票池维护：" + prune_text)

    lines.append("")
    lines.append("完整逐股解释已写入输出目录：latest_explanations.csv 和 latest_report.md。")
    lines.append(f"判断阈值：综合分 ≥ {threshold:.1f} 且必须通过硬过滤；未通过任一硬过滤，即使分数高也不买。")
    lines.append("执行纪律：不追高于计划仓位；跌破止损先退出；盈利后用10/20日线或2ATR移动止盈。")
    return "\n".join(lines)

def send_webhook(msg: str, url: str, webhook_type: str = "wecom") -> None:
    if not url:
        print("[推送] webhook_url 为空，跳过推送")
        return
    webhook_type = (webhook_type or "wecom").lower()
    headers = {"Content-Type": "application/json"}
    if webhook_type in {"wecom", "dingtalk", "generic"}:
        payload = {"msgtype": "text", "text": {"content": msg}}
    elif webhook_type == "feishu":
        payload = {"msg_type": "text", "content": {"text": msg}}
    else:
        payload = {"text": msg}
    resp = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"推送失败: HTTP {resp.status_code} {resp.text[:200]}")
    print("[推送] 已发送 webhook")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股个股/大盘K线尾盘买入信号扫描器")
    parser.add_argument("--pool", required=True, help="股票池文件：CSV/TXT/XLSX，至少包含 code/代码 列，或第一列为股票代码")
    parser.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    parser.add_argument("--out", default="output", help="输出目录")
    parser.add_argument("--account", type=float, default=100000.0, help="账户权益，用于换算买入金额/股数，默认 100000")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新拉取数据")
    parser.add_argument("--tail", action="store_true", help="尾盘实时模式：用实时行情近似合成今日K线；会自动使用不复权日K")
    parser.add_argument("--send", action="store_true", help="扫描/对话处理后发送 webhook")
    parser.add_argument("--webhook-url", default="", help="企业微信/钉钉/飞书 webhook URL；也可用环境变量 SIGNAL_WEBHOOK_URL")
    parser.add_argument("--webhook-type", default="", choices=["", "wecom", "dingtalk", "feishu", "generic"], help="webhook 类型")
    parser.add_argument("--market-index", default="", help="大盘指数，逗号分隔，例如 sh000001,sz399001,sh000300")
    parser.add_argument("--limit", type=int, default=0, help="只扫描前 N 只，测试用；使用该参数时不会自动淘汰股票池")
    parser.add_argument("--chat", default="", help="小龙虾/AI Agent 传入的自然语言命令，例如：加入 600519 贵州茅台 到股票池")
    parser.add_argument("--chat-file", default="", help="从文本文件读取自然语言命令")
    parser.add_argument("--auto-prune", dest="auto_prune", action="store_true", help="扫描完成后，如果股票池超过阈值，自动淘汰图形最弱的股票")
    parser.add_argument("--detail", action="store_true", help="把逐股原因也写入 latest_message.txt/推送；默认只输出精简摘要，原因按需查询")
    parser.add_argument("--no-auto-prune", dest="auto_prune", action="store_false", help="禁用自动淘汰；优先级高于配置文件")
    parser.set_defaults(auto_prune=None)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    if args.tail:
        cfg["data"]["use_realtime_tail"] = True
    if args.market_index:
        cfg["data"]["market_indices"] = [x.strip() for x in args.market_index.split(",") if x.strip()]
    if args.webhook_url:
        cfg["notify"]["webhook_url"] = args.webhook_url
    elif os.environ.get("SIGNAL_WEBHOOK_URL"):
        cfg["notify"]["webhook_url"] = os.environ["SIGNAL_WEBHOOK_URL"]
    if args.webhook_type:
        cfg["notify"]["webhook_type"] = args.webhook_type
    if args.send:
        cfg["notify"]["send"] = True
    if getattr(args, "detail", False):
        cfg.setdefault("report", {})["include_explanations_in_message"] = True

    chat_text = args.chat or ""
    if args.chat_file:
        chat_text = Path(args.chat_file).read_text(encoding="utf-8")

    try:
        if chat_text.strip():
            reply, reply_path = handle_chat_command(chat_text, args, cfg)
            print("\n" + reply + "\n")
            if cfg["notify"].get("send"):
                send_webhook(reply, cfg["notify"].get("webhook_url", ""), cfg["notify"].get("webhook_type", "wecom"))
            return 0

        auto_prune = bool(cfg.get("pool", {}).get("auto_prune", False)) if args.auto_prune is None else bool(args.auto_prune)
        signals, candidates, market, msg_path, prune_report = scan(
            args.pool,
            cfg,
            args.out,
            args.account,
            refresh=args.refresh,
            limit=args.limit,
            auto_prune=auto_prune,
        )
        msg = msg_path.read_text(encoding="utf-8")
        print("\n" + msg + "\n")
        if cfg["notify"].get("send"):
            send_webhook(msg, cfg["notify"].get("webhook_url", ""), cfg["notify"].get("webhook_type", "wecom"))
        return 0
    except KeyboardInterrupt:
        print("用户中断")
        return 130
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
