# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *

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
        self.use_stale_cache_on_network_error = bool(cfg["data"].get("use_stale_cache_on_network_error", True))
        self.fail_fast_network_errors = bool(cfg["data"].get("fail_fast_network_errors", True))
        self.stock_providers = provider_list(cfg, "hist_providers", ["tencent", "sina", "eastmoney"])
        self.index_providers = provider_list(cfg, "index_providers", ["tencent", "eastmoney", "legacy"])
        self._spot_cache: Optional[pd.DataFrame] = None
        self._index_spot_cache: Optional[pd.DataFrame] = None
        self._board_names_cache: Dict[str, pd.DataFrame] = {}
        self._board_symbol_cache: Dict[str, Tuple[set, Dict[str, str]]] = {}
        self._board_hist_cache: Dict[Tuple[str, str, str, str, str], pd.DataFrame] = {}
        self._active_pool_codes_cache: Optional[List[str]] = None
        self._ths_concept_hist_symbol_cache: Optional[Tuple[set, Dict[str, str]]] = None
        self._network_error_seen = False

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

    def _is_network_error(self, exc: Exception) -> bool:
        text = repr(exc)
        markers = [
            "NameResolutionError",
            "Failed to resolve",
            "Name or service not known",
            "Temporary failure in name resolution",
            "getaddrinfo failed",
            "nodename nor servname",
            "Network is unreachable",
            "Connection refused",
            "ConnectionError",
            "ConnectTimeout",
            "ReadTimeout",
            "Read timed out",
            "timed out",
            "Max retries exceeded",
            "ProxyError",
        ]
        return any(m in text for m in markers)

    def _mark_network_error(self, exc: Exception) -> bool:
        is_network_error = self._is_network_error(exc)
        if is_network_error:
            self._network_error_seen = True
        return is_network_error

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
                if self.fail_fast_network_errors and self._mark_network_error(exc):
                    raise exc
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
        stale = self._fallback_stale_cache(f"stock_*_{code}_{adjust or 'none'}_*.csv", normalize_stock_hist) if self.use_stale_cache_on_network_error else None
        if self.fail_fast_network_errors and self._network_error_seen:
            if stale is not None:
                return stale
            raise RuntimeError("已检测到网络不可用，且无本地历史K缓存")
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
                if self._mark_network_error(exc):
                    if stale is not None:
                        return stale
                    if self.fail_fast_network_errors:
                        break
                continue

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
        stale = self._fallback_stale_cache(f"index_*_{symbol}_*.csv", normalize_index_hist) if self.use_stale_cache_on_network_error else None
        if self.fail_fast_network_errors and self._network_error_seen:
            if stale is not None:
                return stale
            raise RuntimeError("已检测到网络不可用，且无本地指数K缓存")
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
                if self._mark_network_error(exc):
                    if stale is not None:
                        return stale
                    if self.fail_fast_network_errors:
                        break
                continue
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
        stale = None
        if self.use_stale_cache_on_network_error:
            if pool_spot_mode and pool_cache_path is not None:
                stale = self._fallback_stale_cache(pool_cache_path.name, lambda x: x)
            if stale is None:
                stale = self._fallback_stale_cache("spot_all.csv", lambda x: x)
        if self.fail_fast_network_errors and self._network_error_seen:
            if stale is not None:
                self._spot_cache = stale
                return stale.copy()
            raise RuntimeError("已检测到网络不可用，且无本地实时行情缓存")
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
                if self._mark_network_error(exc):
                    if stale is not None:
                        self._spot_cache = stale
                        return stale.copy()
                    if self.fail_fast_network_errors:
                        break
                continue
        if stale is None:
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
        cached = self._read_spot_cache(cache_path)
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
        stale = self._fallback_stale_cache(f"board_{kind}_hist_{safe_symbol}_{adjust or 'none'}_{start_date}_*.csv", normalize_index_hist)
        if self.fail_fast_network_errors and self._network_error_seen:
            if stale is not None:
                self._board_hist_cache[mem_key] = stale.copy()
                return stale.copy()
            raise RuntimeError("已检测到网络不可用，跳过板块日K请求")
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
            if stale is not None:
                self._board_hist_cache[mem_key] = stale.copy()
                return stale.copy()
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
    if _should_skip_realtime_tail(hist, today, latest, f("今开"), f("最高"), f("最低"), f("成交量"), f("成交额")):
        return hist
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
    if _should_skip_realtime_tail(hist, today, latest, f("今开"), f("最高"), f("最低"), f("成交量"), f("成交额")):
        return hist
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


def _close_enough(a: float, b: float, rel_tol: float = 1e-5, abs_tol: float = 1e-3) -> bool:
    if not (np.isfinite(a) and np.isfinite(b)):
        return False
    return abs(float(a) - float(b)) <= max(abs_tol, rel_tol * max(abs(float(a)), abs(float(b)), 1.0))


def _should_skip_realtime_tail(
    hist: pd.DataFrame,
    today: pd.Timestamp,
    close: float,
    open_px: float,
    high: float,
    low: float,
    volume: float,
    amount: float,
) -> bool:
    if hist is None or hist.empty:
        return False
    today = pd.Timestamp(today).normalize()
    dates = pd.to_datetime(hist.get("date"), errors="coerce").dropna()
    if dates.empty:
        return False
    last_date = pd.Timestamp(dates.iloc[-1]).normalize()
    if today < last_date:
        return True
    if today == last_date:
        return False
    if today.weekday() >= 5:
        return True

    last = hist.iloc[-1]
    checks = [
        _close_enough(close, safe_float(last.get("close"))),
        _close_enough(open_px, safe_float(last.get("open"))),
        _close_enough(high, safe_float(last.get("high"))),
        _close_enough(low, safe_float(last.get("low"))),
    ]
    if np.isfinite(volume) and np.isfinite(safe_float(last.get("volume"))):
        checks.append(_close_enough(volume, safe_float(last.get("volume")), rel_tol=1e-4, abs_tol=1.0))
    if np.isfinite(amount) and np.isfinite(safe_float(last.get("amount"))):
        checks.append(_close_enough(amount, safe_float(last.get("amount")), rel_tol=1e-4, abs_tol=1.0))
    return bool(checks) and all(checks)


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


def enforce_market_date_consistency(details: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, str, bool]:
    if details is None or details.empty or "date" not in details.columns:
        return details, "", False
    max_lag_days = int(cfg.get("data", {}).get("market_max_date_lag_days", 1) or 0)
    if max_lag_days < 0:
        return details, "", False
    out = details.copy()
    dates = pd.to_datetime(out["date"], errors="coerce")
    valid_dates = dates.dropna()
    if valid_dates.empty:
        return out, "", False
    latest = pd.Timestamp(valid_dates.max()).normalize()
    earliest = pd.Timestamp(valid_dates.min()).normalize()
    spread_days = int((latest - earliest).days)
    if spread_days <= max_lag_days:
        return out, "", False
    stale_mask = dates.notna() & (dates.dt.normalize() < latest - pd.Timedelta(days=max_lag_days))
    warning = f"指数数据日期不一致：最新{latest.strftime('%Y-%m-%d')}，最旧{earliest.strftime('%Y-%m-%d')}，超过{max_lag_days}天，已降为弱势防守"
    if "data_warning" not in out.columns:
        out["data_warning"] = ""
    out.loc[stale_mask, "data_warning"] = (
        out.loc[stale_mask, "data_warning"].astype(str).replace("nan", "").str.strip()
        + "；"
        + warning
    ).str.strip("；")
    return out, warning, True


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

    details, date_warning, force_defensive = enforce_market_date_consistency(details, cfg)
    valid = details[pd.to_numeric(details.get("close"), errors="coerce").notna()].copy()
    if valid.empty:
        valid = details.copy()
    avg_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).mean())
    min_score = float(pd.to_numeric(valid["score"], errors="coerce").fillna(0).min())
    if force_defensive:
        avg_score = min(avg_score, 44.0)
        min_score = min(min_score, 0.0)
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
    if date_warning:
        summary += f"，数据提示：{date_warning}"
    return MarketState(date, avg_score, regime, exposure, details, summary, market_ret20, market_ret60)
