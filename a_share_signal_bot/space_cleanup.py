# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import *


SAFE_OUTPUT_DIRS = [
    "output",
    "backtest_output",
    "backtest_output_strict_t1",
    "backtest_2022_now",
    "bt_strict_2y_slip20",
    "position_strategy_backtest_output",
    "etf_output",
    "fund_output",
    "position_output",
    "trade_output",
]
SAFE_CACHE_DIRS = [
    "cache/hot_pool",
    "cache/etf",
    "cache/etf_pool",
    "cache/fund_dca",
    "cache/intraday_snapshots",
]
SAFE_BACKUP_DIRS = [
    "pool_backups",
    "portfolio_backups",
    "trade_state_backups",
]
SAFE_COMPARISON_GLOBS = [
    "backtest_compare_*",
    "etf_output_compare_*",
]
SKIP_WALK_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
}


@dataclass
class CleanupCandidate:
    path: Path
    size_bytes: int
    kind: str
    reason: str


@dataclass
class CleanupResult:
    candidates: List[CleanupCandidate]
    deleted: List[CleanupCandidate]
    failed: List[Tuple[CleanupCandidate, str]]
    dry_run: bool
    report_path: Path

    @property
    def candidate_bytes(self) -> int:
        return sum(x.size_bytes for x in self.candidates)

    @property
    def deleted_bytes(self) -> int:
        return sum(x.size_bytes for x in self.deleted)


def _cleanup_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    scfg = dict(cfg.get("space_cleanup", {}) or {})
    output_cfg = cfg.get("output", {}) or {}
    scfg.setdefault("output_dirs", SAFE_OUTPUT_DIRS)
    scfg.setdefault("cache_dirs", SAFE_CACHE_DIRS)
    scfg.setdefault("backup_dirs", SAFE_BACKUP_DIRS)
    scfg.setdefault("comparison_dir_globs", SAFE_COMPARISON_GLOBS)
    scfg.setdefault("output_retention_days", output_cfg.get("retention_days", 14))
    scfg.setdefault("cache_retention_days", 60)
    scfg.setdefault("backup_retention_days", 90)
    scfg.setdefault("comparison_retention_days", 0)
    scfg.setdefault("max_history_files_per_dir", output_cfg.get("max_history_files", 300))
    scfg.setdefault("backup_keep_files", 30)
    scfg.setdefault("keep_latest_files", output_cfg.get("keep_latest_files", True))
    scfg.setdefault("delete_python_cache", True)
    scfg.setdefault("delete_comparison_dirs", True)
    return scfg


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def _inside_root(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                try:
                    if child.is_file() and not child.is_symlink():
                        total += int(child.stat().st_size)
                except Exception:
                    continue
            return total
    except Exception:
        return 0
    return 0


def _is_latest_name(path: Path) -> bool:
    name = path.name
    return name.startswith(("latest_", "last_"))


def _expired(path: Path, days: float, now_ts: float) -> bool:
    if days <= 0:
        return True
    try:
        return now_ts - path.stat().st_mtime > days * 86400
    except Exception:
        return False


def _add_candidate(
    candidates: List[CleanupCandidate],
    seen: set[str],
    root: Path,
    path: Path,
    kind: str,
    reason: str,
) -> None:
    path = path.resolve()
    if not _inside_root(root, path) or path == root:
        return
    key = str(path)
    if key in seen or not path.exists():
        return
    seen.add(key)
    candidates.append(CleanupCandidate(path=path, size_bytes=_path_size(path), kind=kind, reason=reason))


def _history_files(base: Path) -> List[Path]:
    if not base.exists() or not base.is_dir():
        return []
    out: List[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_latest_name(path):
            continue
        out.append(path)
    return out


def _collect_output_candidates(root: Path, scfg: Dict[str, Any], aggressive: bool, now_ts: float) -> List[CleanupCandidate]:
    candidates: List[CleanupCandidate] = []
    seen: set[str] = set()
    retention_days = float(scfg.get("output_retention_days", 14))
    max_history = int(scfg.get("max_history_files_per_dir", 300))
    keep_latest = bool(scfg.get("keep_latest_files", True))
    for rel in _as_list(scfg.get("output_dirs")):
        base = (root / rel).resolve()
        if not _inside_root(root, base) or not base.exists() or not base.is_dir():
            continue
        files = []
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if keep_latest and _is_latest_name(path):
                continue
            files.append(path)
            if aggressive or _expired(path, retention_days, now_ts):
                _add_candidate(candidates, seen, root, path, "output", f"{rel} 历史输出")
        remaining = [x for x in files if str(x.resolve()) not in seen]
        remaining = sorted(remaining, key=lambda x: x.stat().st_mtime, reverse=True)
        if max_history > 0 and len(remaining) > max_history:
            for path in remaining[max_history:]:
                _add_candidate(candidates, seen, root, path, "output", f"{rel} 超过保留数量")
    return candidates


def _collect_cache_candidates(root: Path, scfg: Dict[str, Any], aggressive: bool, now_ts: float) -> List[CleanupCandidate]:
    candidates: List[CleanupCandidate] = []
    seen: set[str] = set()
    retention_days = float(scfg.get("cache_retention_days", 60))
    keep_latest = bool(scfg.get("keep_latest_files", True))
    for rel in _as_list(scfg.get("cache_dirs")):
        base = (root / rel).resolve()
        if not _inside_root(root, base) or not base.exists() or not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if keep_latest and _is_latest_name(path):
                continue
            if aggressive or _expired(path, retention_days, now_ts):
                _add_candidate(candidates, seen, root, path, "cache", f"{rel} 过期缓存")
    return candidates


def _collect_backup_candidates(root: Path, scfg: Dict[str, Any], aggressive: bool, now_ts: float) -> List[CleanupCandidate]:
    candidates: List[CleanupCandidate] = []
    seen: set[str] = set()
    retention_days = float(scfg.get("backup_retention_days", 90))
    keep_files = int(scfg.get("backup_keep_files", 30))
    for rel in _as_list(scfg.get("backup_dirs")):
        base = (root / rel).resolve()
        if not _inside_root(root, base) or not base.exists() or not base.is_dir():
            continue
        files = sorted([x for x in base.rglob("*") if x.is_file() and not x.is_symlink()], key=lambda x: x.stat().st_mtime, reverse=True)
        for idx, path in enumerate(files):
            if aggressive or _expired(path, retention_days, now_ts) or (keep_files > 0 and idx >= keep_files):
                _add_candidate(candidates, seen, root, path, "backup", f"{rel} 旧备份")
    return candidates


def _collect_comparison_candidates(root: Path, scfg: Dict[str, Any], aggressive: bool, now_ts: float) -> List[CleanupCandidate]:
    candidates: List[CleanupCandidate] = []
    seen: set[str] = set()
    if not bool(scfg.get("delete_comparison_dirs", True)):
        return candidates
    retention_days = float(scfg.get("comparison_retention_days", 0))
    for pattern in _as_list(scfg.get("comparison_dir_globs")):
        if "/" in pattern or "\\" in pattern:
            continue
        for path in root.glob(pattern):
            if not path.is_dir() or path.is_symlink():
                continue
            if aggressive or _expired(path, retention_days, now_ts):
                _add_candidate(candidates, seen, root, path, "comparison", "临时对比输出目录")
    return candidates


def _collect_python_cache_candidates(root: Path, scfg: Dict[str, Any]) -> List[CleanupCandidate]:
    candidates: List[CleanupCandidate] = []
    seen: set[str] = set()
    if not bool(scfg.get("delete_python_cache", True)):
        return candidates
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_WALK_DIRS]
        for name in list(dirnames):
            if name in {"__pycache__", ".pytest_cache"}:
                path = current / name
                _add_candidate(candidates, seen, root, path, "python_cache", "Python 测试/编译缓存")
                dirnames.remove(name)
    return candidates


def _prune_nested_candidates(candidates: List[CleanupCandidate]) -> List[CleanupCandidate]:
    sorted_candidates = sorted(candidates, key=lambda x: (len(x.path.parts), str(x.path)))
    kept: List[CleanupCandidate] = []
    kept_dirs: List[Path] = []
    for item in sorted_candidates:
        nested = False
        for parent in kept_dirs:
            try:
                item.path.relative_to(parent)
                nested = True
                break
            except Exception:
                pass
        if nested:
            continue
        kept.append(item)
        if item.path.is_dir():
            kept_dirs.append(item.path)
    return kept


def collect_space_cleanup_candidates(
    root: str | Path,
    cfg: Dict[str, Any],
    aggressive: bool = False,
    now_ts: Optional[float] = None,
) -> List[CleanupCandidate]:
    root_path = Path(root).resolve()
    scfg = _cleanup_cfg(cfg)
    ts = time.time() if now_ts is None else float(now_ts)
    candidates: List[CleanupCandidate] = []
    candidates.extend(_collect_output_candidates(root_path, scfg, aggressive, ts))
    candidates.extend(_collect_cache_candidates(root_path, scfg, aggressive, ts))
    candidates.extend(_collect_backup_candidates(root_path, scfg, aggressive, ts))
    candidates.extend(_collect_comparison_candidates(root_path, scfg, aggressive, ts))
    candidates.extend(_collect_python_cache_candidates(root_path, scfg))
    return _prune_nested_candidates(candidates)


def apply_space_cleanup(candidates: List[CleanupCandidate], dry_run: bool = False) -> Tuple[List[CleanupCandidate], List[Tuple[CleanupCandidate, str]]]:
    deleted: List[CleanupCandidate] = []
    failed: List[Tuple[CleanupCandidate, str]] = []
    if dry_run:
        return [], []
    for item in sorted(candidates, key=lambda x: len(x.path.parts), reverse=True):
        try:
            if item.path.is_dir():
                shutil.rmtree(item.path)
            elif item.path.exists():
                item.path.unlink()
            deleted.append(item)
        except Exception as exc:
            failed.append((item, str(exc)))
    return deleted, failed


def human_bytes(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{value:.0f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TB"


def format_space_cleanup_report(result: CleanupResult, root: str | Path) -> str:
    root_path = Path(root).resolve()
    action = "预览" if result.dry_run else "已清理"
    lines = [f"# 空间清理{action} {now_cn().strftime('%Y-%m-%d %H:%M')}", ""]
    lines.append(f"- 候选项：{len(result.candidates)}")
    lines.append(f"- 候选空间：{human_bytes(result.candidate_bytes)}")
    if result.dry_run:
        lines.append("- 当前为预览模式，未删除文件。")
    else:
        lines.append(f"- 已删除：{len(result.deleted)}")
        lines.append(f"- 释放空间：{human_bytes(result.deleted_bytes)}")
    if result.failed:
        lines.append(f"- 删除失败：{len(result.failed)}")
    by_kind: Dict[str, Tuple[int, int]] = {}
    for item in result.candidates:
        count, size = by_kind.get(item.kind, (0, 0))
        by_kind[item.kind] = (count + 1, size + item.size_bytes)
    if by_kind:
        lines.append("")
        lines.append("## 分类")
        for kind, (count, size) in sorted(by_kind.items()):
            lines.append(f"- {kind}: {count} 项，{human_bytes(size)}")
    if result.candidates:
        lines.append("")
        lines.append("## 明细")
        for item in sorted(result.candidates, key=lambda x: x.size_bytes, reverse=True)[:40]:
            try:
                rel = item.path.relative_to(root_path)
            except Exception:
                rel = item.path
            lines.append(f"- {rel}：{human_bytes(item.size_bytes)}，{item.reason}")
        if len(result.candidates) > 40:
            lines.append(f"- ... 其余 {len(result.candidates) - 40} 项省略")
    if result.failed:
        lines.append("")
        lines.append("## 失败")
        for item, err in result.failed[:20]:
            try:
                rel = item.path.relative_to(root_path)
            except Exception:
                rel = item.path
            lines.append(f"- {rel}: {err}")
    return "\n".join(lines)


def run_space_cleanup(
    cfg: Dict[str, Any],
    root: str | Path = ".",
    out_dir: str | Path = "cleanup_output",
    dry_run: bool = False,
    aggressive: bool = False,
) -> CleanupResult:
    root_path = Path(root).resolve()
    out_path = ensure_dir(root_path / out_dir)
    candidates = collect_space_cleanup_candidates(root_path, cfg, aggressive=aggressive)
    deleted, failed = apply_space_cleanup(candidates, dry_run=dry_run)
    result = CleanupResult(candidates=candidates, deleted=deleted, failed=failed, dry_run=dry_run, report_path=out_path / "latest_space_cleanup_report.md")
    report = format_space_cleanup_report(result, root_path)
    result.report_path.write_text(report, encoding="utf-8")
    (out_path / "latest_space_cleanup_message.txt").write_text("\n".join(report.splitlines()[:50]), encoding="utf-8")
    return result


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="清理量化系统生成的冗余/过期文件")
    p.add_argument("--config", default="", help="配置文件 YAML/JSON，可选")
    p.add_argument("--root", default=".", help="项目根目录")
    p.add_argument("--out", default="cleanup_output", help="清理报告输出目录")
    p.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    p.add_argument("--aggressive", action="store_true", help="深度清理：删除更多历史输出和缓存")
    p.add_argument("--output-retention-days", type=float, default=None, help="输出文件保留天数")
    p.add_argument("--cache-retention-days", type=float, default=None, help="缓存文件保留天数")
    p.add_argument("--backup-retention-days", type=float, default=None, help="备份文件保留天数")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    scfg = cfg.setdefault("space_cleanup", {})
    if args.output_retention_days is not None:
        scfg["output_retention_days"] = args.output_retention_days
    if args.cache_retention_days is not None:
        scfg["cache_retention_days"] = args.cache_retention_days
    if args.backup_retention_days is not None:
        scfg["backup_retention_days"] = args.backup_retention_days
    result = run_space_cleanup(
        cfg,
        root=args.root,
        out_dir=args.out,
        dry_run=bool(args.dry_run),
        aggressive=bool(args.aggressive),
    )
    print(result.report_path.read_text(encoding="utf-8"))
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
