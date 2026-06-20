# 架构说明

本项目保留原有命令入口，例如 `python main.py`、`python backtest.py` 和各个 `run_*.sh` 不变；真实业务实现移动到 `a_share_signal_bot/` 包内。

## 设计边界

- `main.py`、`backtest.py`、`trade_manager.py` 等顶层文件是兼容入口，负责把旧命令转发到包内模块。
- `a_share_signal_bot/base.py` 放配置、模型、股票池 IO、通用工具。
- `a_share_signal_bot/market_data.py` 放行情数据适配器、缓存、指标和大盘状态评估。
- `a_share_signal_bot/sectors.py` 放板块映射、自动补全和板块上下文。
- `a_share_signal_bot/strategy.py` 放买点识别、候选评分和仓位分配。
- `a_share_signal_bot/interaction.py` 放对话命令、解释文本和股票池自动淘汰。
- `a_share_signal_bot/scanner.py` 放扫描流程编排、CLI 参数和 webhook。
- `a_share_signal_bot/formatting.py` 放输出列名映射等纯格式化逻辑。
- 回测、持仓监控、交易计划、热榜和持仓管理分别在包内同名模块中实现。

## 使用的模式

- Facade：顶层脚本和 `a_share_signal_bot/scanner.py` 对外保留原 API，隐藏内部拆分。
- Adapter：`market_data.py` 统一封装腾讯、东方财富、新浪、efinance 等数据源差异。
- Layered Architecture：基础工具、数据、板块、策略、交互、编排单向依赖，避免循环依赖。

## 回归测试

重构或改算法前后执行：

```bash
./run_regression.sh
```

测试只覆盖离线、确定性行为，不请求行情接口。当前覆盖股票代码规范化、配置合并、股票池读写、指标生成、风险闸门、自动淘汰输出、消息格式和对话解析。

其中 `test_golden_master_full_scan_matches_backup_main` 会动态加载重构前备份的 `backups/refactor_20260620_230315/main.py`，再用固定 FakeFetcher 同时运行旧版和新版完整 `scan()` 链路，逐字段比较：

- `candidates` 候选评分表；
- `allocated` 最终仓位表；
- `market.details` 大盘评分明细；
- `MarketState` 的核心标量字段。

这个测试不访问网络，也不依赖真实行情缓存。
