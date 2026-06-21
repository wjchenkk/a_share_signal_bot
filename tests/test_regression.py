from __future__ import annotations

import copy
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

import main as bot
from a_share_signal_bot.market_data import AkshareFetcher
from a_share_signal_bot import hot_pool
from a_share_signal_bot import etf_strategy
from a_share_signal_bot import etf_rotation
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
