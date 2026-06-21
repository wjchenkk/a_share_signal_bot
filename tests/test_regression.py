from __future__ import annotations

import copy
import importlib.util
import io
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

import main as bot
from a_share_signal_bot.market_data import AkshareFetcher
from a_share_signal_bot import market_data
from a_share_signal_bot import hot_pool
from a_share_signal_bot import base as bot_base
from a_share_signal_bot import etf_strategy
from a_share_signal_bot import etf_rotation
from a_share_signal_bot import etf_pool
from a_share_signal_bot import position_monitor
from a_share_signal_bot import trade_manager
import a_share_signal_bot.scanner as scanner


ROOT = Path(__file__).resolve().parents[1]
OLD_MAIN_PATH = ROOT / "backups" / "refactor_20260620_230315" / "main.py"


def load_old_main_module():
    spec = importlib.util.spec_from_file_location("old_main_golden_master", OLD_MAIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载旧版 main.py: {OLD_MAIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def deterministic_hist(start_price: float, end_price: float, periods: int = 280, breakout: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=periods, freq="B")
    x = np.linspace(0.0, 1.0, periods)
    wave = 0.35 * np.sin(np.linspace(0.0, 8.0 * np.pi, periods))
    close = start_price + (end_price - start_price) * x + wave
    if breakout:
        base = close[-31]
        close[-30:] = np.linspace(base * 0.98, base * 1.13, 30)
    open_ = close * (1.0 + 0.004 * np.sin(np.linspace(0.0, 5.0 * np.pi, periods)))
    high = np.maximum(open_, close) * (1.014 + 0.002 * np.cos(np.linspace(0.0, 4.0 * np.pi, periods)))
    low = np.minimum(open_, close) * (0.986 - 0.001 * np.sin(np.linspace(0.0, 6.0 * np.pi, periods)))
    amount = 120_000_000 * (1.0 + 0.15 * np.sin(np.linspace(0.0, 7.0 * np.pi, periods)))
    if breakout:
        amount[-1] = float(pd.Series(amount).tail(20).mean() * 1.45)
    volume = amount / close / 100.0
    df = pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "turnover": 2.5 + 0.2 * np.sin(np.linspace(0.0, 3.0 * np.pi, periods)),
        }
    )
    df["pct_chg"] = df["close"].pct_change().fillna(0.0) * 100.0
    return df


class DeterministicFetcher:
    stock_data = {
        "600519": deterministic_hist(11.0, 24.0, breakout=True),
        "300750": deterministic_hist(18.0, 31.0, breakout=False),
        "000001": deterministic_hist(22.0, 16.0, breakout=False),
    }
    index_data = {
        "sh000001": deterministic_hist(3000.0, 3600.0, breakout=False),
        "sz399001": deterministic_hist(9000.0, 10800.0, breakout=False),
    }
    board_data = {
        "科技": deterministic_hist(1000.0, 1500.0, breakout=True),
        "消费": deterministic_hist(1000.0, 1320.0, breakout=False),
        "银行": deterministic_hist(1000.0, 920.0, breakout=False),
    }
    names = {"600519": "测试科技", "300750": "测试消费", "000001": "测试银行"}

    def __init__(self, cfg, refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh

    def stock_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        df = self.stock_data[str(code).zfill(6)].copy()
        df.attrs["data_provider"] = "deterministic"
        return df

    def index_hist(self, symbol: str) -> pd.DataFrame:
        df = self.index_data[str(symbol).lower()].copy()
        df.attrs["data_provider"] = "deterministic"
        return df

    def board_hist(self, kind: str, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        df = self.board_data[str(symbol)].copy()
        df.attrs["data_provider"] = f"deterministic_{kind}"
        return df

    def stock_spot_all(self) -> pd.DataFrame:
        rows = []
        for code, df in self.stock_data.items():
            r = df.iloc[-1]
            rows.append(
                {
                    "代码": code,
                    "名称": self.names.get(code, ""),
                    "最新价": r["close"],
                    "涨跌幅": r["pct_chg"],
                    "今开": r["open"],
                    "最高": r["high"],
                    "最低": r["low"],
                    "成交额": r["amount"],
                    "成交量": r["volume"],
                    "换手率": r["turnover"],
                }
            )
        return pd.DataFrame(rows)

    def index_spot_all(self) -> pd.DataFrame:
        return pd.DataFrame()

    def board_names(self, kind: str) -> pd.DataFrame:
        return pd.DataFrame({"板块名称": list(self.board_data.keys()), "板块代码": list(self.board_data.keys())})

    def board_cons(self, kind: str, symbol: str, prefer_stale: bool = False) -> pd.DataFrame:
        code_by_sector = {"科技": "600519", "消费": "300750", "银行": "000001"}
        code = code_by_sector[str(symbol)]
        return pd.DataFrame({"代码": [code], "名称": [self.names.get(code, "")]})


class DeterministicEtfFetcher:
    etf_data = {
        "510300": deterministic_hist(3.0, 5.3, periods=280, breakout=True),
        "159915": deterministic_hist(4.5, 3.2, periods=280, breakout=False),
    }

    def __init__(self, cfg, refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh

    def etf_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        df = self.etf_data[str(code).zfill(6)].copy()
        df.attrs["data_provider"] = "deterministic_etf"
        return df


class FailingEtfFetcher:
    def __init__(self, cfg, refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh

    def etf_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        raise RuntimeError("NameResolutionError: Failed to resolve ETF source")


class PartiallyFailingEtfFetcher:
    def __init__(self, cfg, refresh: bool = False):
        self.cfg = cfg
        self.refresh = refresh

    def etf_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        code = str(code).zfill(6)
        if code == "510300":
            df = deterministic_hist(3.0, 5.3, periods=280, breakout=True)
            df.attrs["data_provider"] = "deterministic_etf"
            return df
        raise RuntimeError("NameResolutionError: Failed to resolve ETF source")


class DeterministicEtfPoolFetcher:
    def __init__(self, cfg, cache_dir: str = "", refresh: bool = False):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.refresh = refresh

    def fetch_all(self, sources):
        raw = pd.DataFrame(
            [
                {"代码": "510300", "名称": "沪深300ETF", "最新价": 4.2, "成交额": "10亿", "涨跌幅": 1.2},
                {"代码": "510310", "名称": "沪深300增强ETF", "最新价": 3.9, "成交额": "9亿", "涨跌幅": 1.0},
                {"代码": "512880", "名称": "证券ETF", "最新价": 1.1, "成交额": "8亿", "涨跌幅": 0.8},
                {"代码": "511010", "名称": "国债ETF", "最新价": 1.1, "成交额": "7亿", "涨跌幅": 0.1},
                {"代码": "513100", "名称": "纳指ETF", "最新价": 1.5, "成交额": "6亿", "涨跌幅": 0.5},
                {"代码": "511990", "名称": "货币ETF", "最新价": 100.0, "成交额": "20亿", "涨跌幅": 0.0},
            ]
        )
        return etf_pool.normalize_etf_spot(raw, "deterministic"), []


class FailingEtfPoolFetcher:
    def __init__(self, cfg, cache_dir: str = "", refresh: bool = False):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.refresh = refresh

    def fetch_all(self, sources):
        return pd.DataFrame(), [{"source": "eastmoney", "error": "NameResolutionError"}]


def comparable_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "code" in out.columns:
        out["code"] = out["code"].astype(str).str.zfill(6)
        out = out.sort_values("code").reset_index(drop=True)
    return out.reindex(sorted(out.columns), axis=1)


class OfflineRegressionTests(unittest.TestCase):
    def test_normalize_code_variants(self) -> None:
        cases = {
            "600519.SH": "600519",
            "sh600519": "600519",
            " 000001 ": "000001",
            "SZ300750": "300750",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bot.normalize_code(raw), expected)

    def test_deep_merge_keeps_base_unchanged(self) -> None:
        base = {"data": {"cache_hours": 24, "providers": ["a"]}, "flag": True}
        merged = bot.deep_merge(base, {"data": {"cache_hours": 1}})
        self.assertEqual(merged["data"]["cache_hours"], 1)
        self.assertEqual(merged["data"]["providers"], ["a"])
        self.assertEqual(base["data"]["cache_hours"], 24)

    def test_write_or_clear_error_csv_removes_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "latest_errors.csv"
            path.write_text("stale", encoding="utf-8")
            bot_base.write_or_clear_error_csv(path, [])
            self.assertFalse(path.exists())
            bot_base.write_or_clear_error_csv(path, [{"code": "510300", "error": "failed"}])
            self.assertTrue(path.exists())
            self.assertIn("510300", path.read_text(encoding="utf-8-sig"))

    def test_stock_pool_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool.csv"
            path.write_text(
                "股票代码,股票名称,所属板块\n"
                "600519.SH,贵州茅台,白酒\n"
                "SZ300750,宁德时代,电池\n"
                "bad,无效,忽略\n",
                encoding="utf-8-sig",
            )
            pool = bot.read_stock_pool(str(path))
            self.assertEqual(pool["code"].tolist(), ["600519", "300750"])
            self.assertEqual(pool["sector"].tolist(), ["白酒", "电池"])

            out_path = Path(td) / "pool_out.csv"
            written = bot.write_stock_pool(pool, out_path)
            reread = bot.read_stock_pool(str(out_path))
            pd.testing.assert_frame_equal(written, reread)

    def test_stock_hist_uses_stale_cache_on_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["data"]["cache_dir"] = td
            stale = deterministic_hist(10.0, 12.0, periods=220)
            stale.to_csv(Path(td) / "stock_tencent_600519_qfq_20220101_20260619.csv", index=False)
            fetcher = AkshareFetcher(cfg)
            calls = []

            def fail_tencent(*args, **kwargs):
                calls.append("tencent")
                raise RuntimeError("NameResolutionError: Failed to resolve 'web.ifzq.gtimg.cn'")

            def fail_if_called(*args, **kwargs):
                calls.append("unexpected")
                raise AssertionError("stale cache should short-circuit other providers")

            fetcher._stock_hist_tencent = fail_tencent  # type: ignore[method-assign]
            fetcher._stock_hist_sina = fail_if_called  # type: ignore[method-assign]
            fetcher._stock_hist_eastmoney = fail_if_called  # type: ignore[method-assign]
            df = fetcher.stock_hist("600519", "20220101", "20260621", "qfq")
            self.assertEqual(calls, ["tencent"])
            self.assertEqual(df.attrs.get("data_provider"), "stale_cache")
            self.assertGreaterEqual(len(df), 200)

    def test_index_spot_cache_uses_realtime_freshness_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["data"]["cache_dir"] = td
            cfg["data"]["spot_cache_minutes"] = 0.01
            stale_path = Path(td) / "index_spot_all.csv"
            pd.DataFrame([{"代码": "sh000001", "最新价": 3000.0}]).to_csv(stale_path, index=False)
            old_ts = time.time() - 600
            os.utime(stale_path, (old_ts, old_ts))

            fetcher = AkshareFetcher(cfg)
            calls = []
            fresh = pd.DataFrame([{"代码": "sh000001", "最新价": 3100.0}])
            old_ak = sys.modules.get("akshare")
            sys.modules["akshare"] = types.SimpleNamespace(stock_zh_index_spot_sina=lambda: fresh)
            try:
                fetcher._with_retry = lambda label, func: calls.append(label) or fresh  # type: ignore[method-assign]
                out = fetcher.index_spot_all()
            finally:
                if old_ak is None:
                    sys.modules.pop("akshare", None)
                else:
                    sys.modules["akshare"] = old_ak

            self.assertEqual(calls, ["指数实时行情"])
            self.assertEqual(float(out.iloc[0]["最新价"]), 3100.0)

    def test_index_tail_skips_stale_snapshot_after_last_trade_day(self) -> None:
        hist = deterministic_hist(3000.0, 3600.0, periods=220)
        hist["date"] = pd.date_range(end="2026-06-18", periods=len(hist), freq="B")
        last = hist.iloc[-1]
        spot = pd.DataFrame(
            [
                {
                    "代码": "sh000001",
                    "最新价": last["close"],
                    "今开": last["open"],
                    "最高": last["high"],
                    "最低": last["low"],
                    "成交量": last["volume"],
                    "成交额": last["amount"],
                }
            ]
        )
        old_now = market_data.now_cn
        try:
            market_data.now_cn = lambda: pd.Timestamp("2026-06-19 14:55")  # type: ignore[assignment]
            out = market_data.merge_index_tail_realtime(hist, spot, "sh000001")
        finally:
            market_data.now_cn = old_now  # type: ignore[assignment]
        self.assertEqual(len(out), len(hist))
        self.assertEqual(pd.Timestamp(out.iloc[-1]["date"]).strftime("%Y-%m-%d"), "2026-06-18")

    def test_market_date_mismatch_forces_defensive_regime(self) -> None:
        fresh = deterministic_hist(3000.0, 3800.0, periods=260)
        fresh["date"] = pd.date_range(end="2026-06-19", periods=len(fresh), freq="B")
        stale = deterministic_hist(9000.0, 11500.0, periods=260)
        stale["date"] = pd.date_range(end="2026-06-16", periods=len(stale), freq="B")

        class SplitDateFetcher:
            def index_hist(self, symbol: str) -> pd.DataFrame:
                return fresh.copy() if symbol == "idx_fresh" else stale.copy()

        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["data"]["market_indices"] = ["idx_fresh", "idx_stale"]
        cfg["data"]["use_realtime_tail"] = False
        cfg["data"]["market_max_date_lag_days"] = 1
        market = market_data.evaluate_market(SplitDateFetcher(), cfg)  # type: ignore[arg-type]
        self.assertEqual(market.regime, "weak")
        self.assertEqual(float(market.target_exposure), 0.0)
        self.assertIn("指数数据日期不一致", market.summary)

    def test_board_symbol_lookup_prefers_fresh_source_over_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["data"]["cache_dir"] = td
            cfg["data"]["cache_hours"] = 1
            stale_path = Path(td) / "board_industry_names.csv"
            pd.DataFrame([{"板块名称": "旧行业", "板块代码": "old"}]).to_csv(stale_path, index=False, encoding="utf-8-sig")
            old_ts = time.time() - 48 * 3600
            os.utime(stale_path, (old_ts, old_ts))
            fetcher = AkshareFetcher(cfg)
            fetcher.board_names = lambda kind: pd.DataFrame([{"板块名称": "新行业", "板块代码": "new"}])  # type: ignore[method-assign]
            valid_names, code_to_name = fetcher._board_symbol_lookup("industry")
            self.assertIn("新行业", valid_names)
            self.assertNotIn("旧行业", valid_names)
            self.assertEqual(code_to_name["new"], "新行业")

    def test_ths_concept_lookup_prefers_fresh_source_over_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["data"]["cache_dir"] = td
            cfg["data"]["cache_hours"] = 1
            stale_path = Path(td) / "board_concept_ths_names.csv"
            pd.DataFrame([{"概念名称": "旧概念", "代码": "old"}]).to_csv(stale_path, index=False, encoding="utf-8-sig")
            old_ts = time.time() - 48 * 3600
            os.utime(stale_path, (old_ts, old_ts))
            fresh = pd.DataFrame([{"概念名称": "新概念", "代码": "new"}])
            old_ak = sys.modules.get("akshare")
            sys.modules["akshare"] = types.SimpleNamespace(stock_board_concept_name_ths=lambda: fresh)
            try:
                fetcher = AkshareFetcher(cfg)
                names, code_to_name = fetcher._ths_concept_hist_lookup()
            finally:
                if old_ak is None:
                    sys.modules.pop("akshare", None)
                else:
                    sys.modules["akshare"] = old_ak
            self.assertIn("新概念", names)
            self.assertNotIn("旧概念", names)
            self.assertEqual(code_to_name["new"], "新概念")

    def test_stock_tail_appends_changed_realtime_snapshot(self) -> None:
        hist = deterministic_hist(10.0, 12.0, periods=220)
        hist["date"] = pd.date_range(end="2026-06-18", periods=len(hist), freq="B")
        last = hist.iloc[-1]
        latest = float(last["close"]) * 1.01
        spot = pd.DataFrame(
            [
                {
                    "代码": "600519",
                    "最新价": latest,
                    "今开": float(last["open"]),
                    "最高": latest,
                    "最低": float(last["low"]),
                    "成交量": float(last["volume"]) + 1000,
                    "成交额": float(last["amount"]) + 1000000,
                    "涨跌幅": 1.0,
                    "换手率": 1.2,
                }
            ]
        )
        old_now = market_data.now_cn
        try:
            market_data.now_cn = lambda: pd.Timestamp("2026-06-19 14:55")  # type: ignore[assignment]
            out = market_data.merge_stock_tail_realtime(hist, spot, "600519")
        finally:
            market_data.now_cn = old_now  # type: ignore[assignment]
        self.assertEqual(len(out), len(hist) + 1)
        self.assertEqual(pd.Timestamp(out.iloc[-1]["date"]).strftime("%Y-%m-%d"), "2026-06-19")
        self.assertAlmostEqual(float(out.iloc[-1]["close"]), latest)

    def test_stale_realtime_spot_is_not_used_for_prefilter_or_snapshot(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["data"]["two_stage_scan"] = True
        cfg["data"]["prefilter_pool_when_gt"] = 3
        cfg["data"]["max_scan_per_run"] = 2
        pool = pd.DataFrame(
            [
                {"code": "600001", "name": "一"},
                {"code": "600002", "name": "二"},
                {"code": "600003", "name": "三"},
                {"code": "600004", "name": "四"},
            ]
        )
        stale_spot = pd.DataFrame(
            [
                {"代码": "600004", "名称": "四", "最新价": 10.0, "涨跌幅": 2.0, "成交额": 9_000_000_000, "换手率": 10.0},
            ]
        )
        stale_spot.attrs["data_provider"] = "stale_cache"
        selected, msg = scanner.prefilter_pool_for_stability(pool, stale_spot, cfg)
        self.assertEqual(selected["code"].tolist(), ["600001", "600002"])
        self.assertIn("优先扫描2只", msg)
        self.assertIn("实时行情为旧缓存", msg)

        with tempfile.TemporaryDirectory() as td:
            cfg["data"]["intraday_snapshot_dir"] = str(Path(td) / "snap")
            out = scanner.update_intraday_snapshot_from_spot("600004", stale_spot, cfg)
            self.assertTrue(out.empty)
            self.assertFalse((Path(td) / "snap" / "600004.csv").exists())

    def test_position_monitor_ignores_stale_minute_window(self) -> None:
        stale_minutes = pd.DataFrame(
            [
                {"datetime": "2026-06-18 14:50:00", "close": 10.0},
                {"datetime": "2026-06-18 14:55:00", "close": 10.1},
            ]
        )
        today_window, note = position_monitor.minute_window_for_today(stale_minutes, pd.Timestamp("2026-06-21 10:00"))
        self.assertTrue(today_window.empty)
        self.assertIn("未使用历史分钟K", note)

        fresh_minutes = pd.concat(
            [
                stale_minutes,
                pd.DataFrame([{"datetime": "2026-06-21 10:00:00", "close": 10.2}]),
            ],
            ignore_index=True,
        )
        today_window, note = position_monitor.minute_window_for_today(fresh_minutes, pd.Timestamp("2026-06-21 10:00"))
        self.assertEqual(len(today_window), 1)
        self.assertEqual(note, "")

    def test_trade_manager_expires_stale_pending_buys(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg.setdefault("trade_lifecycle", {})["pending_buy_expire_days"] = 5
        state = pd.DataFrame(
            [
                {"code": "600001", "name": "过期计划", "status": "PENDING_BUY", "signal_date": "2026-06-15", "shares": 100, "last_price": 10.0},
                {"code": "600002", "name": "未过期计划", "status": "PENDING_BUY", "signal_date": "2026-06-18", "shares": 100, "last_price": 10.0},
                {"code": "600003", "name": "持仓", "status": "ACTIVE", "signal_date": "2026-06-01", "shares": 100, "last_price": 10.0},
            ]
        )
        out, actions = trade_manager.expire_stale_pending_buys(state, cfg, pd.Timestamp("2026-06-21"))
        by_code = out.set_index("code")
        self.assertEqual(by_code.loc["600001", "status"], "EXPIRED")
        self.assertEqual(by_code.loc["600002", "status"], "PENDING_BUY")
        self.assertEqual(by_code.loc["600003", "status"], "ACTIVE")
        self.assertEqual(actions["action"].tolist(), ["EXPIRE_PENDING_BUY"])

    def test_trade_manager_filters_stale_latest_signals(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg.setdefault("trade_lifecycle", {})["latest_signal_max_age_days"] = 3
        signals = pd.DataFrame(
            [
                {"code": "600001", "name": "旧信号", "date": "2026-06-17", "target_shares": 100},
                {"code": "600002", "name": "新信号", "date": "2026-06-20", "target_shares": 100},
            ]
        )
        fresh, note = trade_manager.filter_fresh_signals(signals, cfg, pd.Timestamp("2026-06-21"))
        self.assertEqual(fresh["code"].tolist(), ["600002"])
        self.assertIn("已忽略 1 条过期买入信号", note)

        missing_date, note = trade_manager.filter_fresh_signals(signals.drop(columns=["date"]), cfg, pd.Timestamp("2026-06-21"))
        self.assertTrue(missing_date.empty)
        self.assertIn("缺少信号日期", note)

    def test_trade_manager_run_does_not_create_plan_from_stale_latest_signals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            signals_out = root / "output"
            trade_out = root / "trade_output"
            signals_out.mkdir()
            pd.DataFrame(
                [
                    {
                        "code": "600001",
                        "name": "旧信号",
                        "date": "2026-06-17",
                        "close": 10.0,
                        "target_shares": 100,
                        "stop_loss": 9.2,
                        "take_profit_1": 11.2,
                        "take_profit_2": 12.4,
                    }
                ]
            ).to_csv(signals_out / "latest_signals_raw.csv", index=False, encoding="utf-8-sig")
            args = types.SimpleNamespace(
                action="from_signals",
                portfolio=str(root / "portfolio.csv"),
                state=str(root / "trade_state.csv"),
                signals_out=str(signals_out),
                config=str(ROOT / "config.example.yml"),
                out=str(trade_out),
                account=200000.0,
                mode="intraday",
                sync=False,
                no_add=False,
                refresh=False,
                message_file="",
            )
            old_now = trade_manager.now_cn
            try:
                trade_manager.now_cn = lambda: pd.Timestamp("2026-06-21 10:00").to_pydatetime()  # type: ignore[assignment]
                actions, msg, _ = trade_manager.run(args)
            finally:
                trade_manager.now_cn = old_now  # type: ignore[assignment]
            state = trade_manager.read_state(root / "trade_state.csv")
            self.assertTrue(actions.empty)
            self.assertTrue(state.empty)
            self.assertIn("过期买入信号", msg)

    def test_trading_pool_can_require_local_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "stock_pool.csv"
            pool_path.write_text("code,name,sector\n600519,贵州茅台,白酒\n", encoding="utf-8-sig")
            hot_dir = root / "hot"
            hot_dir.mkdir()
            pd.DataFrame(
                [
                    {"code": "000001", "name": "平安银行", "hot_rank": 1, "hot_score": 100, "sources": "x"},
                    {"code": "000002", "name": "万科A", "hot_rank": 2, "hot_score": 90, "sources": "x"},
                ]
            ).to_csv(hot_dir / "hot_rank_20260619.csv", index=False, encoding="utf-8-sig")
            history_dir = root / "cache"
            history_dir.mkdir()
            deterministic_hist(10.0, 12.0, periods=220).to_csv(
                history_dir / "stock_tencent_000002_qfq_20220101_20260619.csv",
                index=False,
            )
            args = type(
                "Args",
                (),
                {
                    "for_date": "20260622",
                    "cache_dir": str(hot_dir),
                    "allow_same_day_hot": False,
                    "hot_date": "",
                    "mainboard_only": True,
                    "pool": str(pool_path),
                    "max_size": 3,
                    "out": str(root / "out"),
                    "history_cache_dir": str(history_dir),
                    "history_adjust": "qfq",
                    "prefer_local_history": True,
                    "require_local_history": True,
                },
            )()
            trading = hot_pool.build_trading_pool(args)
            self.assertEqual(trading["code"].tolist(), ["600519", "000002"])

    def test_etf_pool_reader_accepts_common_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "etf_pool.csv"
            path.write_text(
                "ETF代码,ETF名称,跟踪指数\n"
                "SH510300,沪深300ETF,沪深300\n"
                "159915,创业板ETF,创业板指\n"
                "bad,无效,忽略\n",
                encoding="utf-8-sig",
            )
            pool = etf_strategy.read_etf_pool(str(path))
            self.assertEqual(pool["code"].tolist(), ["510300", "159915"])
            self.assertEqual(pool["category"].tolist(), ["沪深300", "创业板指"])

    def test_etf_scan_writes_independent_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "etf_pool.csv"
            pool_path.write_text(
                "code,name,category\n"
                "510300,沪深300ETF,宽基\n"
                "159915,创业板ETF,宽基\n",
                encoding="utf-8-sig",
            )
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["output"]["cleanup_on_run"] = False
            cfg["etf"]["cache_dir"] = str(root / "cache" / "etf")
            cfg["etf"]["min_amount_ma20"] = 1
            cfg["etf"]["score_threshold"] = 60.0
            cfg["etf"]["max_positions"] = 2
            out_dir = root / "etf_out"

            old_fetcher = etf_strategy.EtfFetcher
            try:
                etf_strategy.EtfFetcher = DeterministicEtfFetcher
                allocated, candidates, msg_path = etf_strategy.scan_etf(
                    str(pool_path),
                    cfg,
                    str(out_dir),
                    account=100000.0,
                    refresh=False,
                    limit=0,
                )
            finally:
                etf_strategy.EtfFetcher = old_fetcher

            self.assertTrue(msg_path.exists())
            self.assertTrue((out_dir / "latest_etf_signals.csv").exists())
            self.assertTrue((out_dir / "latest_etf_candidates.csv").exists())
            self.assertFalse((out_dir / "latest_signals.csv").exists())
            self.assertIn("510300", candidates["code"].astype(str).tolist())
            self.assertGreaterEqual(len(allocated), 1)
            self.assertTrue(allocated["code"].astype(str).str.contains("510300").any())

    def test_etf_scan_handles_all_data_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "etf_pool.csv"
            pool_path.write_text("code,name,category\n510300,沪深300ETF,宽基\n", encoding="utf-8-sig")
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["output"]["cleanup_on_run"] = False
            cfg["etf"]["cache_dir"] = str(root / "cache" / "etf")
            out_dir = root / "etf_out"

            old_fetcher = etf_strategy.EtfFetcher
            try:
                etf_strategy.EtfFetcher = FailingEtfFetcher
                allocated, candidates, _ = etf_strategy.scan_etf(
                    str(pool_path),
                    cfg,
                    str(out_dir),
                    account=100000.0,
                    refresh=False,
                    limit=0,
                )
            finally:
                etf_strategy.EtfFetcher = old_fetcher

            self.assertTrue(allocated.empty)
            self.assertEqual(float(candidates.iloc[0]["score"]), 0.0)
            self.assertIn("数据错误", str(candidates.iloc[0]["filter_reason"]))
            self.assertTrue((out_dir / "latest_etf_errors.csv").exists())

    def test_etf_scan_suppresses_signals_when_error_rate_too_high(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "etf_pool.csv"
            pool_path.write_text(
                "code,name,category\n"
                "510300,沪深300ETF,宽基\n"
                "159915,创业板ETF,宽基\n"
                "512880,证券ETF,证券\n",
                encoding="utf-8-sig",
            )
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["output"]["cleanup_on_run"] = False
            cfg["etf"]["cache_dir"] = str(root / "cache" / "etf")
            cfg["etf"]["min_amount_ma20"] = 1
            cfg["etf"]["score_threshold"] = 1.0
            cfg["etf"]["max_error_rate_for_valid_run"] = 0.20
            out_dir = root / "etf_out"

            old_fetcher = etf_strategy.EtfFetcher
            try:
                etf_strategy.EtfFetcher = PartiallyFailingEtfFetcher
                allocated, candidates, _ = etf_strategy.scan_etf(
                    str(pool_path),
                    cfg,
                    str(out_dir),
                    account=100000.0,
                    refresh=False,
                    limit=0,
                )
            finally:
                etf_strategy.EtfFetcher = old_fetcher

            self.assertTrue(allocated.empty)
            self.assertFalse(candidates["is_signal"].fillna(False).any())
            self.assertIn("data_quality_warning", candidates.columns)
            self.assertTrue(candidates["data_quality_warning"].astype(str).str.contains("失败率").any())

    def test_etf_scan_filters_stale_history_dates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "etf_pool.csv"
            pool_path.write_text(
                "code,name,category\n"
                "510300,沪深300ETF,宽基\n"
                "159915,创业板ETF,宽基\n",
                encoding="utf-8-sig",
            )
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["output"]["cleanup_on_run"] = False
            cfg["etf"]["cache_dir"] = str(root / "cache" / "etf")
            cfg["etf"]["min_amount_ma20"] = 1
            cfg["etf"]["score_threshold"] = 1.0
            cfg["etf"]["max_data_lag_days"] = 3

            class SplitDateEtfFetcher:
                def __init__(self, cfg, refresh: bool = False):
                    pass

                def etf_hist(self, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
                    df = deterministic_hist(3.0, 5.3, periods=280, breakout=True)
                    end = "2026-06-18" if str(code).zfill(6) == "510300" else "2026-06-10"
                    df["date"] = pd.date_range(end=end, periods=len(df), freq="B")
                    return df

            old_fetcher = etf_strategy.EtfFetcher
            try:
                etf_strategy.EtfFetcher = SplitDateEtfFetcher
                allocated, candidates, _ = etf_strategy.scan_etf(
                    str(pool_path),
                    cfg,
                    str(root / "etf_out"),
                    account=100000.0,
                    refresh=False,
                    limit=0,
                )
            finally:
                etf_strategy.EtfFetcher = old_fetcher

            by_code = candidates.set_index("code")
            self.assertFalse(bool(by_code.loc["159915", "is_signal"]))
            self.assertIn("行情日期滞后", str(by_code.loc["159915", "filter_reason"]))
            self.assertNotIn("159915", allocated["code"].astype(str).tolist())

    def test_etf_pool_fetcher_ignores_stale_latest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["etf"]["pool_builder"]["cache_hours"] = 1
            cache_dir = root / "cache" / "etf_pool"
            cache_dir.mkdir(parents=True)
            latest = cache_dir / "latest_etf_spot_eastmoney.csv"
            pd.DataFrame([{"代码": "510999", "名称": "旧ETF", "最新价": 1.0, "成交额": "1亿"}]).to_csv(latest, index=False, encoding="utf-8-sig")
            old_ts = time.time() - 48 * 3600
            os.utime(latest, (old_ts, old_ts))

            fresh_raw = pd.DataFrame([{"代码": "510300", "名称": "沪深300ETF", "最新价": 4.2, "成交额": "10亿"}])
            old_ak = sys.modules.get("akshare")
            sys.modules["akshare"] = types.SimpleNamespace(fund_etf_spot_em=lambda: fresh_raw)
            try:
                fetcher = etf_pool.EtfPoolFetcher(cfg, cache_dir, refresh=False)
                out = fetcher.fetch_source("eastmoney")
            finally:
                if old_ak is None:
                    sys.modules.pop("akshare", None)
                else:
                    sys.modules["akshare"] = old_ak

            self.assertEqual(out["code"].tolist(), ["510300"])
            self.assertNotIn("510999", out["code"].tolist())

    def test_etf_pool_builder_selects_liquid_diversified_pool(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["pool_builder"]["min_amount"] = 1
        cfg["etf"]["pool_builder"]["min_price"] = 0.0
        cfg["etf"]["pool_builder"]["max_size"] = 4
        cfg["etf"]["pool_builder"]["max_per_theme"] = 1
        cfg["etf"]["pool_builder"]["asset_class_quotas"] = {
            "broad": 2,
            "sector": 2,
            "cross_border": 1,
            "defensive": 1,
            "commodity": 1,
        }
        raw, _ = DeterministicEtfPoolFetcher(cfg).fetch_all(["deterministic"])
        candidates = etf_pool.enrich_etf_pool_candidates(raw, cfg)
        selected = etf_pool.select_etf_pool(candidates, cfg)
        codes = selected["code"].astype(str).tolist()
        self.assertIn("510300", codes)
        self.assertIn("512880", codes)
        self.assertIn("511010", codes)
        self.assertIn("513100", codes)
        self.assertNotIn("510310", codes)
        self.assertFalse(candidates.loc[candidates["code"].eq("511990"), "eligible"].iloc[0])
        self.assertTrue(selected["category"].astype(str).str.contains("/").all())

    def test_etf_asset_class_and_theme_avoid_broad_misclassification(self) -> None:
        self.assertEqual(etf_rotation.classify_asset_class("科创芯片ETF嘉实", ""), "sector")
        self.assertEqual(etf_rotation.classify_asset_class("科创50ETF华夏", ""), "broad")
        self.assertEqual(etf_rotation.classify_asset_class("香港证券ETF易方达", ""), "cross_border")
        self.assertEqual(etf_rotation.classify_asset_class("中概互联网ETF易方达", ""), "cross_border")
        self.assertEqual(etf_rotation.classify_asset_class("自由现金流ETF华夏", ""), "sector")
        self.assertEqual(etf_pool.theme_from_name("科创50ETF华夏", "broad"), "科创50")
        self.assertEqual(etf_pool.theme_from_name("上证50ETF华夏", "broad"), "上证50")

    def test_etf_pool_allows_defensive_missing_spot_amount(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["pool_builder"]["min_amount"] = 30_000_000
        raw = etf_pool.normalize_etf_spot(
            pd.DataFrame(
                [
                    {"代码": "511010", "名称": "国泰上证5年期国债ETF", "最新价": "", "成交额": ""},
                    {"代码": "159201", "名称": "自由现金流ETF华夏", "最新价": 1.126, "成交额": "3亿"},
                    {"代码": "159001", "名称": "货币ETF易方达", "最新价": 100.0, "成交额": "3亿"},
                ]
            ),
            "deterministic",
        )
        candidates = etf_pool.enrich_etf_pool_candidates(raw, cfg)
        by_code = candidates.set_index("code")
        self.assertTrue(bool(by_code.at["511010", "eligible"]))
        self.assertEqual(by_code.at["511010", "asset_class"], "defensive")
        self.assertTrue(bool(by_code.at["159201", "eligible"]))
        self.assertEqual(by_code.at["159201", "asset_class"], "sector")
        self.assertFalse(bool(by_code.at["159001", "eligible"]))

    def test_build_etf_pool_writes_independent_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.yml"
            cfg_path.write_text(
                "etf:\n"
                "  pool_builder:\n"
                "    min_amount: 1\n"
                "    min_price: 0.0\n"
                "    max_size: 3\n"
                "    max_per_theme: 1\n"
                "    asset_class_quotas:\n"
                "      broad: 1\n"
                "      sector: 1\n"
                "      defensive: 1\n"
                "      cross_border: 1\n"
                "      commodity: 1\n",
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {
                    "config": str(cfg_path),
                    "out": str(root / "etf_out"),
                    "pool_out": str(root / "etf_pool.csv"),
                    "cache_dir": str(root / "cache" / "etf_pool"),
                    "sources": "deterministic",
                    "max_size": None,
                    "min_amount": None,
                    "refresh": False,
                },
            )()
            old_fetcher = etf_pool.EtfPoolFetcher
            try:
                etf_pool.EtfPoolFetcher = DeterministicEtfPoolFetcher
                with redirect_stdout(io.StringIO()):
                    pool, candidates, report_path = etf_pool.build_etf_pool(args)
            finally:
                etf_pool.EtfPoolFetcher = old_fetcher
            self.assertFalse(pool.empty)
            self.assertGreater(len(candidates), len(pool))
            self.assertTrue((root / "etf_pool.csv").exists())
            self.assertTrue((root / "etf_out" / "latest_etf_pool_candidates.csv").exists())
            self.assertTrue((root / "etf_out" / "latest_etf_pool_selected.csv").exists())
            self.assertTrue(report_path.exists())
            self.assertFalse((root / "etf_out" / "latest_signals.csv").exists())

    def test_build_etf_pool_does_not_overwrite_on_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "etf_pool.csv"
            original = "code,name,category\n510300,沪深300ETF,宽基\n"
            pool_path.write_text(original, encoding="utf-8-sig")
            args = type(
                "Args",
                (),
                {
                    "config": "",
                    "out": str(root / "etf_out"),
                    "pool_out": str(pool_path),
                    "cache_dir": str(root / "cache" / "etf_pool"),
                    "sources": "eastmoney",
                    "max_size": None,
                    "min_amount": None,
                    "refresh": True,
                },
            )()
            old_fetcher = etf_pool.EtfPoolFetcher
            try:
                etf_pool.EtfPoolFetcher = FailingEtfPoolFetcher
                with self.assertRaisesRegex(RuntimeError, "未获取到ETF列表"):
                    with redirect_stdout(io.StringIO()):
                        etf_pool.build_etf_pool(args)
            finally:
                etf_pool.EtfPoolFetcher = old_fetcher
            self.assertEqual(pool_path.read_text(encoding="utf-8-sig"), original)
            self.assertTrue((root / "etf_out" / "latest_etf_pool_report.md").exists())
            self.assertTrue((root / "etf_out" / "latest_etf_pool_errors.csv").exists())

    def test_etf_rank_pct_direction_matches_score_semantics(self) -> None:
        values = pd.Series([0.10, 0.20, 0.30], index=["weak", "mid", "strong"])
        high_scores = etf_rotation._rank_pct(values, higher_is_better=True)
        self.assertGreater(float(high_scores["strong"]), float(high_scores["mid"]))
        self.assertGreater(float(high_scores["mid"]), float(high_scores["weak"]))

        low_scores = etf_rotation._rank_pct(values, higher_is_better=False)
        self.assertGreater(float(low_scores["weak"]), float(low_scores["mid"]))
        self.assertGreater(float(low_scores["mid"]), float(low_scores["strong"]))

    def test_etf_rotation_selects_with_category_caps(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["min_history_days"] = 120
        cfg["etf"]["rotation"]["min_amount_ma20"] = 1
        cfg["etf"]["rotation"]["score_threshold"] = 20.0
        cfg["etf"]["rotation"]["min_ret60"] = -1.0
        cfg["etf"]["rotation"]["require_ma60"] = False
        cfg["etf"]["rotation"]["max_positions"] = 3
        cfg["etf"]["rotation"]["max_per_category"] = 1
        pool = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基"},
                {"code": "159915", "name": "创业板ETF", "category": "宽基"},
                {"code": "511010", "name": "国债ETF", "category": "债券"},
            ]
        )
        hist_map = {
            "510300": deterministic_hist(3.0, 5.5, periods=360, breakout=True),
            "159915": deterministic_hist(2.5, 5.2, periods=360, breakout=True),
            "511010": deterministic_hist(1.0, 1.12, periods=360, breakout=False),
        }
        candidates = etf_rotation.compute_rotation_candidates(pool, hist_map, cfg)
        regime = etf_rotation.market_regime_from_candidates(candidates, cfg)
        positions = etf_rotation.select_rotation_positions(candidates, hist_map, cfg, account=100000.0, regime=regime)
        self.assertFalse(candidates.empty)
        self.assertGreaterEqual(float(candidates["rotation_score"].max()), 20.0)
        self.assertFalse(positions.empty)
        self.assertLessEqual(int((positions["category"] == "宽基").sum()), 1)
        self.assertIn("target_weight", positions.columns)

    def test_etf_rotation_filters_stale_history_dates(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "relative_momentum"
        cfg["etf"]["rotation"]["min_history_days"] = 120
        cfg["etf"]["rotation"]["min_amount_ma20"] = 1
        cfg["etf"]["rotation"]["relative_momentum"]["min_ret60"] = -1.0
        cfg["etf"]["rotation"]["relative_momentum"]["allow_defensive"] = True
        cfg["etf"]["max_data_lag_days"] = 3
        pool = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基"},
                {"code": "159915", "name": "创业板ETF", "category": "宽基"},
            ]
        )
        fresh = deterministic_hist(3.0, 5.3, periods=280, breakout=True)
        fresh["date"] = pd.date_range(end="2026-06-18", periods=len(fresh), freq="B")
        stale = deterministic_hist(3.0, 7.0, periods=280, breakout=True)
        stale["date"] = pd.date_range(end="2026-06-10", periods=len(stale), freq="B")
        candidates = etf_rotation.compute_rotation_candidates(
            pool,
            {"510300": fresh, "159915": stale},
            cfg,
        )
        by_code = candidates.set_index("code")
        self.assertFalse(bool(by_code.loc["159915", "is_rotation_candidate"]))
        self.assertIn("行情日期滞后", str(by_code.loc["159915", "filter_reason"]))

    def test_etf_rotation_redistributes_after_position_caps(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "balanced"
        cfg["etf"]["rotation"]["max_positions"] = 3
        cfg["etf"]["rotation"]["max_per_category"] = 3
        cfg["etf"]["rotation"]["max_position_pct"] = 0.40
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        candidates = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "rotation_score": 95.0, "selection_score": 95.0, "atr_pct": 0.008, "close": 4.0},
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "rotation_score": 80.0, "selection_score": 80.0, "atr_pct": 0.050, "close": 1.2},
                {"code": "513100", "name": "纳指ETF", "category": "海外", "asset_class": "cross_border", "is_rotation_candidate": True, "rotation_score": 70.0, "selection_score": 70.0, "atr_pct": 0.060, "close": 1.5},
            ]
        )
        hist_map = {
            "510300": deterministic_hist(3.0, 5.5, periods=260, breakout=False),
            "512880": deterministic_hist(1.0, 1.8, periods=260, breakout=False),
            "513100": deterministic_hist(1.0, 1.6, periods=260, breakout=False),
        }
        regime = {"regime": "strong", "target_exposure": 0.90, "summary": "测试强势"}
        positions = etf_rotation.select_rotation_positions(candidates, hist_map, cfg, account=100000.0, regime=regime)
        self.assertAlmostEqual(float(positions["target_weight"].sum()), 0.90, places=6)
        self.assertLessEqual(float(positions["target_weight"].max()), 0.4000001)

    def test_etf_rotation_applies_asset_caps_and_defensive_floor(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "balanced"
        cfg["etf"]["rotation"]["max_positions"] = 3
        cfg["etf"]["rotation"]["max_per_category"] = 3
        cfg["etf"]["rotation"]["max_position_pct"] = 0.50
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["weak_min_defensive_pct"] = 0.20
        candidates = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "rotation_score": 95.0, "selection_score": 95.0, "atr_pct": 0.010, "close": 4.0},
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "rotation_score": 85.0, "selection_score": 85.0, "atr_pct": 0.015, "close": 1.2},
                {"code": "511010", "name": "国债ETF", "category": "债券", "asset_class": "defensive", "is_rotation_candidate": True, "rotation_score": 40.0, "selection_score": 40.0, "atr_pct": 0.006, "close": 1.1},
            ]
        )
        hist_map = {
            "510300": deterministic_hist(3.0, 4.0, periods=260, breakout=False),
            "512880": deterministic_hist(1.0, 1.4, periods=260, breakout=False),
            "511010": deterministic_hist(1.0, 1.1, periods=260, breakout=False),
        }
        regime = {"regime": "weak", "target_exposure": 0.35, "summary": "测试弱势"}
        positions = etf_rotation.select_rotation_positions(candidates, hist_map, cfg, account=100000.0, regime=regime)
        broad_weight = float(positions.loc[positions["asset_class"].eq("broad"), "target_weight"].sum())
        defensive_weight = float(positions.loc[positions["asset_class"].eq("defensive"), "target_weight"].sum())
        self.assertLessEqual(broad_weight, 0.2500001)
        self.assertGreaterEqual(defensive_weight, 0.20)

    def test_etf_rotation_strong_regime_keeps_core_broad_position(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "balanced"
        cfg["etf"]["rotation"]["max_positions"] = 2
        cfg["etf"]["rotation"]["max_per_category"] = 2
        cfg["etf"]["rotation"]["max_position_pct"] = 0.50
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["core_broad_regimes"] = ["strong"]
        cfg["etf"]["rotation"]["core_broad_min_score"] = 55.0
        candidates = pd.DataFrame(
            [
                {"code": "511010", "name": "国债ETF", "category": "债券", "asset_class": "defensive", "is_rotation_candidate": True, "rotation_score": 78.0, "atr_pct": 0.006, "close": 1.1},
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "rotation_score": 60.0, "atr_pct": 0.020, "close": 4.0},
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "rotation_score": 72.0, "atr_pct": 0.030, "close": 1.2},
            ]
        )
        hist_map = {
            "511010": deterministic_hist(1.0, 1.1, periods=260),
            "510300": deterministic_hist(3.0, 4.0, periods=260),
            "512880": deterministic_hist(1.0, 1.8, periods=260),
        }
        positions = etf_rotation.select_rotation_positions(
            candidates,
            hist_map,
            cfg,
            account=100000.0,
            regime={"regime": "strong", "target_exposure": 0.90, "summary": "测试强势"},
        )
        self.assertIn("510300", positions["code"].astype(str).tolist())

    def test_etf_relative_momentum_excludes_defensive_and_equal_weights(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "relative_momentum"
        cfg["etf"]["rotation"]["max_positions"] = 3
        cfg["etf"]["rotation"]["max_position_pct"] = 0.40
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["relative_momentum"]["target_exposure"] = 0.90
        cfg["etf"]["rotation"]["relative_momentum"]["allow_defensive"] = False
        candidates = pd.DataFrame(
            [
                {"code": "511010", "name": "国债ETF", "category": "债券", "asset_class": "defensive", "is_rotation_candidate": True, "momentum_signal": 0.50, "rotation_score": 99.0, "atr_pct": 0.006, "close": 1.1},
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "momentum_signal": 0.30, "rotation_score": 90.0, "atr_pct": 0.030, "close": 1.2},
                {"code": "513100", "name": "纳指ETF", "category": "海外", "asset_class": "cross_border", "is_rotation_candidate": True, "momentum_signal": 0.25, "rotation_score": 85.0, "atr_pct": 0.035, "close": 1.5},
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "momentum_signal": 0.20, "rotation_score": 80.0, "atr_pct": 0.020, "close": 4.0},
            ]
        )
        hist_map = {
            "511010": deterministic_hist(1.0, 1.1, periods=260),
            "512880": deterministic_hist(1.0, 1.8, periods=260),
            "513100": deterministic_hist(1.0, 1.7, periods=260),
            "510300": deterministic_hist(3.0, 4.0, periods=260),
        }
        positions = etf_rotation.select_rotation_positions(
            candidates,
            hist_map,
            cfg,
            account=100000.0,
            regime={"regime": "relative_momentum", "target_exposure": 0.90, "summary": "测试相对动量"},
        )
        self.assertNotIn("511010", positions["code"].astype(str).tolist())
        self.assertAlmostEqual(float(positions["target_weight"].sum()), 0.90, places=6)
        self.assertTrue((positions["target_weight"].round(6) == 0.30).all())

    def test_etf_relative_momentum_keeps_incumbent_with_turnover_penalty(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "relative_momentum"
        cfg["etf"]["rotation"]["max_positions"] = 2
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["relative_momentum"]["max_per_asset_class"] = 3
        cfg["etf"]["rotation"]["relative_momentum"]["turnover_penalty"]["enabled"] = True
        cfg["etf"]["rotation"]["relative_momentum"]["turnover_penalty"]["min_score_advantage"] = 0.03
        candidates = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "momentum_signal": 0.300, "rotation_score": 95.0, "atr_pct": 0.020, "close": 4.0},
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "momentum_signal": 0.280, "rotation_score": 90.0, "atr_pct": 0.030, "close": 1.2},
                {"code": "159915", "name": "创业板ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "momentum_signal": 0.275, "rotation_score": 85.0, "atr_pct": 0.025, "close": 2.0},
            ]
        )
        hist_map = {
            "510300": deterministic_hist(3.0, 4.0, periods=260),
            "512880": deterministic_hist(1.0, 1.8, periods=260),
            "159915": deterministic_hist(1.0, 1.7, periods=260),
        }
        positions = etf_rotation.select_rotation_positions(
            candidates,
            hist_map,
            cfg,
            account=100000.0,
            regime={"regime": "relative_momentum", "target_exposure": 1.0, "summary": "测试相对动量"},
            current_weights={"159915": 0.25},
        )
        codes = positions["code"].astype(str).tolist()
        self.assertIn("159915", codes)
        self.assertNotIn("512880", codes)

    def test_etf_relative_momentum_blocks_cooldown_reentry(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "relative_momentum"
        cfg["etf"]["rotation"]["max_positions"] = 1
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["relative_momentum"]["cooldown_days"] = 10
        candidates = pd.DataFrame(
            [
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "momentum_signal": 0.30, "rotation_score": 95.0, "atr_pct": 0.030, "close": 1.2},
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "momentum_signal": 0.25, "rotation_score": 90.0, "atr_pct": 0.020, "close": 4.0},
            ]
        )
        hist_map = {
            "512880": deterministic_hist(1.0, 1.8, periods=260),
            "510300": deterministic_hist(3.0, 4.0, periods=260),
        }
        positions = etf_rotation.select_rotation_positions(
            candidates,
            hist_map,
            cfg,
            account=100000.0,
            regime={"regime": "relative_momentum", "target_exposure": 1.0, "summary": "测试相对动量"},
            as_of=pd.Timestamp("2025-05-01"),
            cooldown_until={"512880": pd.Timestamp("2025-05-10")},
        )
        self.assertEqual(positions.iloc[0]["code"], "510300")

    def test_etf_relative_momentum_ignores_cooldown_when_disabled(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["model"] = "relative_momentum"
        cfg["etf"]["rotation"]["max_positions"] = 1
        cfg["etf"]["rotation"]["max_correlation"] = 1.0
        cfg["etf"]["rotation"]["relative_momentum"]["cooldown_days"] = 0
        candidates = pd.DataFrame(
            [
                {"code": "512880", "name": "证券ETF", "category": "证券", "asset_class": "sector", "is_rotation_candidate": True, "momentum_signal": 0.30, "rotation_score": 95.0, "atr_pct": 0.030, "close": 1.2},
                {"code": "510300", "name": "沪深300ETF", "category": "宽基", "asset_class": "broad", "is_rotation_candidate": True, "momentum_signal": 0.25, "rotation_score": 90.0, "atr_pct": 0.020, "close": 4.0},
            ]
        )
        hist_map = {
            "512880": deterministic_hist(1.0, 1.8, periods=260),
            "510300": deterministic_hist(3.0, 4.0, periods=260),
        }
        positions = etf_rotation.select_rotation_positions(
            candidates,
            hist_map,
            cfg,
            account=100000.0,
            regime={"regime": "relative_momentum", "target_exposure": 1.0, "summary": "测试相对动量"},
            as_of=pd.Timestamp("2025-05-01"),
            cooldown_until={"512880": pd.Timestamp("2025-05-10")},
        )
        self.assertEqual(positions.iloc[0]["code"], "512880")

    def test_etf_rotation_rejects_low_history_coverage(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["max_error_rate_for_valid_run"] = 0.20
        pool = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基"},
                {"code": "512880", "name": "证券ETF", "category": "行业"},
                {"code": "511010", "name": "国债ETF", "category": "债券"},
            ]
        )
        hist_map = {"510300": deterministic_hist(3.0, 5.0, periods=260)}
        with self.assertRaisesRegex(RuntimeError, "成功 1/3"):
            etf_rotation.validate_history_coverage(pool, hist_map, cfg, "ETF轮动回测")

    def test_etf_rotation_backtest_outputs_summary(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["etf"]["rotation"]["min_history_days"] = 120
        cfg["etf"]["rotation"]["min_amount_ma20"] = 1
        cfg["etf"]["rotation"]["score_threshold"] = 20.0
        cfg["etf"]["rotation"]["min_ret60"] = -1.0
        cfg["etf"]["rotation"]["require_ma60"] = False
        pool = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "category": "宽基"},
                {"code": "512880", "name": "证券ETF", "category": "行业"},
                {"code": "511010", "name": "国债ETF", "category": "债券"},
            ]
        )
        hist_map = {
            "510300": deterministic_hist(3.0, 5.5, periods=420, breakout=True),
            "512880": deterministic_hist(1.0, 1.8, periods=420, breakout=True),
            "511010": deterministic_hist(1.0, 1.10, periods=420, breakout=False),
        }
        equity, rebalances, summary = etf_rotation.backtest_rotation(
            pool,
            hist_map,
            cfg,
            account=100000.0,
            years=2,
            rebalance="W-FRI",
        )
        self.assertFalse(equity.empty)
        self.assertIn("total_return", summary)
        self.assertIn("max_drawdown", summary)
        self.assertIn("benchmark_total_return", summary)
        self.assertEqual(summary.get("benchmark_code"), "510300")
        self.assertIn("benchmark_sharpe", summary)
        self.assertIn("excess_total_return", summary)
        self.assertTrue(np.isfinite(float(summary["benchmark_total_return"])))
        self.assertGreaterEqual(len(rebalances), 1)

    def test_add_indicators_stable_columns(self) -> None:
        dates = pd.date_range("2025-01-01", periods=130, freq="D")
        close = pd.Series(np.linspace(10.0, 20.0, len(dates)))
        df = pd.DataFrame(
            {
                "date": dates,
                "open": close - 0.1,
                "high": close + 0.3,
                "low": close - 0.3,
                "close": close,
                "amount": np.linspace(100_000_000, 200_000_000, len(dates)),
            }
        )
        ind = bot.add_indicators(df)
        for col in ["ma5", "ma20", "ma60", "atr", "ret20", "amount_ratio20", "reg_slope20", "reg_r2_20"]:
            self.assertIn(col, ind.columns)
        self.assertAlmostEqual(float(ind.iloc[-1]["ma5"]), float(close.tail(5).mean()))
        self.assertTrue(np.isfinite(float(ind.iloc[-1]["atr"])))

    def test_risk_gate_runs_without_late_module_globals(self) -> None:
        dates = pd.date_range("2025-01-01", periods=130, freq="D")
        close = pd.Series(np.linspace(20.0, 10.0, len(dates)))
        df = pd.DataFrame(
            {
                "date": dates,
                "open": close + 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "pct_chg": close.pct_change().fillna(0) * 100,
                "amount": 100_000_000,
            }
        )
        ind = bot.add_indicators(df)
        result = bot.compute_risk_gate(ind, "600519", "贵州茅台", copy.deepcopy(bot.DEFAULT_CONFIG))
        self.assertIn("risk_gate_block", result)
        self.assertIn("risk_tags", result)

    def test_prune_stock_pool_writes_formatted_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pool_path = Path(td) / "pool.csv"
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            full_pool = pd.DataFrame(
                [
                    {"code": "600519", "name": "贵州茅台", "sector": "白酒"},
                    {"code": "300750", "name": "宁德时代", "sector": "电池"},
                    {"code": "000001", "name": "平安银行", "sector": "银行"},
                ]
            )
            bot.write_stock_pool(full_pool, pool_path)
            candidates = pd.DataFrame(
                [
                    {"code": "600519", "name": "贵州茅台", "score": 20, "ret60": -0.2, "ret120": -0.3, "drawdown120": -0.4, "filter_reason": "趋势未达标"},
                    {"code": "300750", "name": "宁德时代", "score": 80, "ret60": 0.2, "ret120": 0.3, "drawdown120": -0.1, "filter_reason": ""},
                    {"code": "000001", "name": "平安银行", "score": 70, "ret60": 0.1, "ret120": 0.1, "drawdown120": -0.2, "filter_reason": ""},
                ]
            )
            cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
            cfg["pool"]["max_size"] = 2
            cfg["pool"]["prune_count"] = 1
            cfg["pool"]["backup_dir"] = str(Path(td) / "pool_backups")
            report = bot.prune_stock_pool_by_candidates(
                str(pool_path),
                full_pool,
                candidates,
                pd.DataFrame(),
                cfg,
                out_dir,
                "20260620_000000",
            )
            self.assertTrue(report.triggered)
            self.assertEqual(len(bot.read_stock_pool(str(pool_path))), 2)
            self.assertTrue((out_dir / "latest_pruned.csv").exists())

    def test_format_compact_message_no_signal(self) -> None:
        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["report"]["include_explanations_in_message"] = False
        market = bot.MarketState(
            date="20260620",
            score=55.0,
            regime="neutral",
            target_exposure=0.65,
            details=pd.DataFrame(),
            summary="市场中性",
            market_ret20=0.01,
            market_ret60=0.02,
        )
        candidates = pd.DataFrame(
            [
                {"code": "600519", "name": "贵州茅台", "is_signal": False, "filter_reason": "分数不足"},
                {"code": "300750", "name": "宁德时代", "is_signal": False, "filter_reason": "板块门槛不足"},
            ]
        )
        msg = bot.format_message(pd.DataFrame(), market, 100000, candidates=candidates, cfg=cfg)
        self.assertIn("股票池扫描：2只", msg)
        self.assertIn("今日无最终买入配置", msg)
        self.assertIn("latest_signals.csv", msg)

    def test_parse_chat_action(self) -> None:
        action = bot.parse_chat_action("加入 600519 贵州茅台 到股票池")
        self.assertEqual(action.kind, "add")
        self.assertEqual(action.items[0][0], "600519")

    def test_stockbot_chat_routes_etf_before_stock_backtest(self) -> None:
        script = (ROOT / "stockbot_chat.sh").read_text(encoding="utf-8")
        self.assertIn("etf_pool.py", script)
        self.assertIn("etf_strategy.py", script)
        self.assertIn("etf_rotation.py", script)
        self.assertIn("ETF_POOL", script)
        etf_pool_pos = script.index("last_etf_pool_chat_run.log")
        etf_backtest_pos = script.index("last_etf_rotation_backtest_chat_run.log")
        stock_backtest_pos = script.index("python backtest.py")
        self.assertLess(etf_pool_pos, etf_backtest_pos)
        self.assertLess(etf_backtest_pos, stock_backtest_pos)

    def test_run_etf_daily_runs_signal_and_rotation(self) -> None:
        script = (ROOT / "run_etf_daily.sh").read_text(encoding="utf-8")
        self.assertIn("etf_strategy.py", script)
        self.assertIn("etf_rotation.py", script)
        self.assertIn("latest_etf_rotation_message.txt", script)

    def test_golden_master_full_scan_matches_backup_main(self) -> None:
        self.assertTrue(OLD_MAIN_PATH.exists(), f"缺少旧版源码备份: {OLD_MAIN_PATH}")
        old = load_old_main_module()

        cfg = copy.deepcopy(bot.DEFAULT_CONFIG)
        cfg["data"]["start_date"] = "20240102"
        cfg["data"]["end_date"] = "20250228"
        cfg["data"]["cache_dir"] = ""
        cfg["data"]["use_realtime_tail"] = False
        cfg["data"]["intraday_buy_filter"] = False
        cfg["data"]["two_stage_scan"] = False
        cfg["strategy"]["sector"]["auto_fill"] = False
        cfg["output"]["cleanup_on_run"] = False

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pool_path = root / "pool.csv"
            pool_path.write_text(
                "code,name,sector\n"
                "600519,测试科技,科技\n"
                "300750,测试消费,消费\n"
                "000001,测试银行,银行\n",
                encoding="utf-8-sig",
            )
            new_cfg = copy.deepcopy(cfg)
            old_cfg = copy.deepcopy(cfg)
            new_cfg["data"]["cache_dir"] = str(root / "new_cache")
            old_cfg["data"]["cache_dir"] = str(root / "old_cache")

            old_fetcher = old.AkshareFetcher
            new_fetcher = scanner.AkshareFetcher
            try:
                old.AkshareFetcher = DeterministicFetcher
                scanner.AkshareFetcher = DeterministicFetcher
                with redirect_stdout(io.StringIO()):
                    old_allocated, old_candidates, old_market, _, old_prune = old.scan(
                        str(pool_path),
                        old_cfg,
                        str(root / "old_out"),
                        200000.0,
                        refresh=False,
                        limit=0,
                        auto_prune=False,
                    )
                    new_allocated, new_candidates, new_market, _, new_prune = scanner.scan(
                        str(pool_path),
                        new_cfg,
                        str(root / "new_out"),
                        200000.0,
                        refresh=False,
                        limit=0,
                        auto_prune=False,
                    )
            finally:
                old.AkshareFetcher = old_fetcher
                scanner.AkshareFetcher = new_fetcher

        pd.testing.assert_frame_equal(
            comparable_frame(old_candidates),
            comparable_frame(new_candidates),
            check_dtype=False,
            check_like=True,
            rtol=1e-10,
            atol=1e-10,
        )
        pd.testing.assert_frame_equal(
            comparable_frame(old_allocated),
            comparable_frame(new_allocated),
            check_dtype=False,
            check_like=True,
            rtol=1e-10,
            atol=1e-10,
        )
        pd.testing.assert_frame_equal(
            comparable_frame(old_market.details),
            comparable_frame(new_market.details),
            check_dtype=False,
            check_like=True,
            rtol=1e-10,
            atol=1e-10,
        )
        for attr in ["date", "score", "regime", "target_exposure", "summary", "market_ret20", "market_ret60"]:
            self.assertEqual(getattr(old_market, attr), getattr(new_market, attr))
        self.assertIsNone(old_prune)
        self.assertIsNone(new_prune)


if __name__ == "__main__":
    unittest.main()
