# -*- coding: utf-8 -*-
from __future__ import annotations

from .market_data import *

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


