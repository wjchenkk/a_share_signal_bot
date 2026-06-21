## A 股信号与资产配置助手

本文件是给外部 Agent/小龙虾使用的操作说明模板。Agent 只能调用脚本输出信号和计划，不能自动下单，不能手工编造股票、ETF、基金或持仓建议。

### 通用入口

绝大多数用户请求都通过统一对话脚本处理：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ./stockbot_chat.sh
```

如果脚本失败，回复错误说明和日志最后 120 行。不要在脚本失败时自行补充交易信号。

### 通用规则

1. 不自动下单，不替用户确认交易。
2. 不手工编造信号、价格、仓位、止损、止盈或基金名单。
3. 必须调用仓库脚本，优先回复脚本生成的摘要或报告。
4. 涉及金额时优先使用环境变量传入，例如 `ACCOUNT=200000`、`FUND_DCA_BUDGET=5000`。
5. 默认项目路径为 `~/project/a_share_signal_bot`。
6. 如果用户要求“预览”或“不实际删除”，必须使用 dry-run 类命令。

### 个股信号和股票池

适用请求：

- 生成买入信号、扫描股票池、今日信号；
- 加入股票、删除股票、查看股票池；
- 解释为什么没买、解释某只股票；
- 个股策略回测。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ./stockbot_chat.sh
```

常用示例：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '生成买入信号' | ./stockbot_chat.sh
cd ~/project/a_share_signal_bot && printf '%s\n' '为什么没买 600519' | ./stockbot_chat.sh
cd ~/project/a_share_signal_bot && printf '%s\n' '用20万模拟账户回测近两年' | ACCOUNT=200000 ./stockbot_chat.sh
```

### ETF 策略

适用请求：

- ETF 建池、更新 ETF 池；
- 生成 ETF 信号；
- ETF 轮动配置；
- ETF 轮动回测。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ./stockbot_chat.sh
```

可用环境变量：

```text
ETF_POOL=etf_pool.csv
ACCOUNT=200000
ETF_BACKTEST_YEARS=3
ETF_REBALANCE=W-FRI
REFRESH=1
```

### 基金定投

适用请求：

- 基金定投计划；
- 筛选定投基金；
- 调整月度定投预算。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '基金定投计划' | FUND_DCA_BUDGET=5000 ./stockbot_chat.sh
```

只输出基金筛选和金额计划，不自动申购。用户执行前需要自行确认费率、限购、风险等级和现金流。

### 持仓管理

适用请求：

- 查看持仓、持仓列表、当前持仓；
- 添加持仓、修改持仓、删除持仓、清空持仓；
- 导入持仓、覆盖持仓、追加持仓；
- 设置总资金、可用现金。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ./stockbot_chat.sh
```

持仓管理只修改本地 `portfolio.csv`，不下单。

### 交易计划和持仓后续

适用请求：

- 生成交易计划；
- 买入后怎么办；
- 后续操作；
- 止损止盈；
- 卖出计划；
- 同步交易状态；
- 我的持仓后续怎么办。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ACCOUNT=200000 ./stockbot_chat.sh
```

脚本会使用 `trade_state.csv` 管理状态，输出待买、止损、止盈、趋势退出和加仓建议。仍然只能回复建议，不能替用户执行交易。

### 盘中持仓监控

适用请求：

- 盘中检查持仓；
- 持仓交易建议；
- 止损、止盈、减仓、加仓；
- position / Position。

调用：

```bash
cd ~/project/a_share_signal_bot && ACCOUNT=200000 PORTFOLIO=portfolio.csv ./run_position_monitor.sh
```

也可以直接走统一入口：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '检查我的持仓是否需要交易' | ./stockbot_chat.sh
```

### 空间清理

适用请求：

- 清理空间、释放空间、磁盘清理；
- 清理缓存、删除冗余、删除过期文件；
- 预览清理空间；
- 深度清理空间。

调用：

```bash
cd ~/project/a_share_signal_bot && printf '%s\n' '<用户原文>' | ./stockbot_chat.sh
```

安全规则：

- 用户说“预览”“看看”“dry-run”时，只能预览，不能删除。
- 清理器只处理生成文件、缓存、备份、临时对比目录和 Python 缓存。
- 不删除源码、配置、`stock_pool.csv`、`etf_pool.csv`、`portfolio.csv`、`.git`、`.venv`。

### 定时脚本

尾盘个股信号和交易计划：

```bash
cd ~/project/a_share_signal_bot && ACCOUNT=200000 ./run_tail_prune.sh
```

ETF 日常信号和轮动：

```bash
cd ~/project/a_share_signal_bot && ACCOUNT=200000 ETF_POOL=etf_pool.csv ./run_etf_daily.sh
```

### 回复规范

- 优先返回脚本输出的摘要。
- 用户问“详细原因”时，引用对应 `latest_*_report.md` 或 `latest_*_candidates.csv` 中的信息。
- 如果有数据源失败、旧缓存、失败率过高等提示，必须原样提醒用户。
- 不把回测结果描述成未来收益承诺。
