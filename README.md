# A 股信号与资产配置系统

本项目用于生成 A 股尾盘/收盘买入信号、ETF 独立策略信号、ETF 轮动组合、基金定投计划，以及买入后的持仓和交易生命周期建议。所有脚本只输出信号、计划和风控建议，不自动下单。

策略结果不是投资建议。实盘前请先小仓或模拟验证，并自行确认数据质量、交易成本、流动性和个人风险承受能力。

## 功能总览

- 个股信号：扫描 `stock_pool.csv`，输出买入候选、目标仓位、止损止盈和解释。
- 交易池：核心股票池叠加热榜补充，支持只纳入已有本地历史 K 线缓存的股票。
- 个股回测：按日线信号回测收益、回撤、交易明细和期末持仓。
- ETF 策略：独立 ETF 池、独立输出目录，支持 ETF 建池、买点信号、轮动配置和轮动回测。
- 基金定投：筛选开放式基金，输出月度预算、定投周期、执行日和每期金额。
- 持仓管理：通过对话维护 `portfolio.csv`，支持查看、添加、修改、删除、清空和导入。
- 交易生命周期：从买入信号生成 T+1 买入计划，并给出止损、止盈、趋势退出和加仓建议。
- 空间清理：清理过期输出、缓存、备份、临时对比目录和 Python 缓存。
- 对话入口：`stockbot_chat.sh` 支持个股、ETF、基金定投、持仓和交易计划相关指令。

顶层脚本保留为兼容入口；业务实现已经拆到 `a_share_signal_bot/` 包内。模块职责和回归测试入口见 [docs/architecture.md](docs/architecture.md)。

## 安装

```bash
mkdir -p ~/project
cd ~/project
git clone git@github.com:wjchenkk/a_share_signal_bot.git
cd ~/project/a_share_signal_bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n stock_pool_sample.csv stock_pool.csv
cp -n portfolio_sample.csv portfolio.csv
cp -n etf_pool_sample.csv etf_pool.csv
cp -n etf_portfolio_sample.csv etf_portfolio.csv
```

国内网络可用：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 数据和配置

主配置文件是 `config.example.yml`。个人实盘参数可以复制到本地配置文件再改，避免把私有配置提交到仓库：

```bash
cp config.example.yml config.yml
```

常用本地文件：

```text
stock_pool.csv        个股核心池
sector_map.csv        可选，手工板块映射
portfolio.csv         当前持仓
trade_state.csv       买入后交易计划状态
etf_pool.csv          ETF池
etf_portfolio.csv     ETF当前持仓
config.yml            可选，本地私有配置
```

`stock_pool.csv` 可以只维护代码和名称：

```csv
code,name
600519,贵州茅台
000858,五粮液
300750,宁德时代
```

扫描时会按配置自动识别或补全板块，并缓存到 `cache/auto_sector_map.csv`。优先级为：股票池已有板块列、`sector_map.csv`、自动缓存、在线反查、未分组。

`portfolio.csv` 示例：

```csv
总资金,可用现金,股票代码,股票名称,股票股数,买入价格
200000,50000,600519,贵州茅台,100,1500.00
200000,50000,300750,宁德时代,200,210.00
```

## 个股信号

个股策略按数据质量、大盘状态、板块主线、个股板块内强度、买点形态和 ATR 风控生成信号。买点形态包括平台突破、主线回踩、天量锚点后缩量再异动。

生成信号：

```bash
python main.py --pool stock_pool.csv --config config.example.yml --out output --account 100000 --tail
```

日常尾盘运行，同时生成交易生命周期计划：

```bash
ACCOUNT=200000 ./run_tail_prune.sh
```

通过对话入口：

```bash
printf '%s\n' '生成买入信号' | ./stockbot_chat.sh
printf '%s\n' '解释今天为什么没有信号' | ./stockbot_chat.sh
printf '%s\n' '为什么没买 600519' | ./stockbot_chat.sh
```

主要输出：

```text
output/latest_message.txt           精简摘要
output/latest_signals.csv           买入信号
output/latest_candidates.csv        候选评分
output/latest_explanations.csv      中文字段解释
output/latest_explanations_raw.csv  原始解释字段
output/latest_report.md             完整报告
```

## 交易池

`stock_pool.csv` 是核心池，不直接被热榜覆盖。热榜只作为补充来源，输出次日扫描用交易池。

```bash
# 缓存当天热榜
python hot_pool.py --mode cache --pool stock_pool.csv --top 100

# 生成次日交易池
python hot_pool.py --mode build --pool stock_pool.csv --max-size 150

# 网络不可用或想减少失败时，只补入已有历史K缓存的热榜股
python hot_pool.py --mode build --pool stock_pool.csv --max-size 150 --require-local-history

# 缓存热榜并生成下一工作日交易池
python hot_pool.py --mode all --pool stock_pool.csv --max-size 150
```

主要输出：

```text
cache/hot_pool/hot_rank_YYYYMMDD.csv  当天热榜缓存
output/latest_trading_pool.csv        次日扫描用交易池
output/latest_hot_pool_top100.csv     最新热榜Top100
```

## 个股回测

```bash
python backtest.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out backtest_output \
  --account 200000 \
  --years 2
```

指定区间：

```bash
python backtest.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out backtest_output \
  --account 200000 \
  --start 20240318 \
  --end 20260618
```

主要输出：

```text
backtest_output/latest_backtest_report.md          回测报告
backtest_output/latest_backtest_message.txt        精简摘要
backtest_output/latest_backtest_equity.csv         每日权益曲线
backtest_output/latest_backtest_trades.csv         交易明细
backtest_output/latest_backtest_signals.csv        历史信号记录
backtest_output/latest_backtest_open_positions.csv 期末持仓
backtest_output/latest_backtest_summary.csv        核心指标
```

通过对话入口：

```bash
printf '%s\n' '用20万模拟账户回测近两年' | ./stockbot_chat.sh
```

## ETF 策略

ETF 策略和个股策略是独立链路：使用 `etf_pool.csv`、`cache/etf`、`cache/etf_pool` 和 `etf_output`，不会读写个股池或个股信号文件。

构建 ETF 池：

```bash
python etf_pool.py --config config.example.yml --pool-out etf_pool.csv --out etf_output
```

生成 ETF 买点信号：

```bash
python etf_strategy.py --pool etf_pool.csv --config config.example.yml --out etf_output --account 100000
```

生成 ETF 轮动组合：

```bash
python etf_rotation.py --mode rotate --pool etf_pool.csv --config config.example.yml --out etf_output --account 100000
```

生成 ETF 持仓/调仓计划：

```bash
python etf_trade_manager.py \
  --portfolio etf_portfolio.csv \
  --targets etf_output/latest_etf_rotation_positions_raw.csv \
  --candidates etf_output/latest_etf_rotation_candidates_raw.csv \
  --config config.example.yml \
  --out etf_output \
  --account 200000
```

ETF 调仓管理默认沿用 `etf.backtest.rebalance`，也就是和回测一致的再平衡频率。默认 `W-FRI` 时，每天运行也只会在周五输出买卖差额，其他日期只推送目标组合和持仓偏离。

回测 ETF 轮动：

```bash
python etf_rotation.py --mode backtest --pool etf_pool.csv --config config.example.yml --out etf_output --account 200000 --years 3 --rebalance W-FRI
```

日常 ETF 运行：

```bash
ACCOUNT=200000 ETF_POOL=etf_pool.csv ETF_PORTFOLIO=etf_portfolio.csv ./run_etf_daily.sh
REFRESH=1 ACCOUNT=200000 ETF_POOL=etf_pool.csv ETF_PORTFOLIO=etf_portfolio.csv ./run_etf_daily.sh
```

通过对话入口：

```bash
printf '%s\n' 'ETF建池' | ./stockbot_chat.sh
printf '%s\n' '生成ETF信号' | ./stockbot_chat.sh
printf '%s\n' 'ETF轮动配置' | ./stockbot_chat.sh
printf '%s\n' 'ETF持仓调仓计划' | ETF_PORTFOLIO=etf_portfolio.csv ./stockbot_chat.sh
printf '%s\n' 'ETF轮动回测' | ./stockbot_chat.sh
```

主要输出：

```text
etf_output/latest_etf_pool_report.md              ETF池构建报告
etf_output/latest_etf_pool_candidates.csv         ETF池候选
etf_output/latest_etf_pool_selected.csv           入池ETF
etf_output/latest_etf_message.txt                 ETF买点摘要
etf_output/latest_etf_signals.csv                 ETF买点配置
etf_output/latest_etf_candidates.csv              ETF买点候选评分
etf_output/latest_etf_rotation_message.txt        ETF轮动摘要
etf_output/latest_etf_rotation_positions.csv      ETF轮动组合
etf_output/latest_etf_rotation_candidates.csv     ETF轮动候选
etf_output/latest_etf_trade_plan.txt              ETF持仓/调仓摘要
etf_output/latest_etf_trade_actions.csv           ETF调仓动作清单
etf_output/latest_etf_rotation_backtest_report.md ETF轮动回测报告
```

## 基金定投

基金定投策略只读取开放式基金排行数据，写入 `fund_output` 和 `cache/fund_dca`，不读写个股池、ETF 池或交易信号文件。

```bash
python fund_dca.py --config config.example.yml --out fund_output --budget 5000
```

通过对话入口：

```bash
printf '%s\n' '基金定投计划' | FUND_DCA_BUDGET=5000 ./stockbot_chat.sh
```

定投模型会从股票型、混合型、指数型、债券型和 QDII 排行中清洗候选，排除货币、现金、短债、同业存单和指定份额类别，再按多周期收益和持续性打分。入选组合按宽基/指数、主动权益、均衡混合、债券、海外 QDII 做类别配额和预算权重。

主要输出：

```text
fund_output/latest_fund_dca_message.txt     精简摘要
fund_output/latest_fund_dca_plan.csv        中文定投计划
fund_output/latest_fund_dca_plan_raw.csv    原始字段计划
fund_output/latest_fund_dca_candidates.csv  全部候选及过滤原因
fund_output/latest_fund_dca_report.md       完整报告
cache/fund_dca/fund_rank_TYPE_YYYYMMDD.csv  基金排行缓存
```

## 持仓和交易计划

通过对话维护持仓：

```text
查看持仓
添加持仓 600519 贵州茅台 100股 成本1500 总资金20万 可用现金5万
修改持仓 600519 200股 成本1450
删除持仓 600519
清空持仓
设置持仓总资金20万 可用现金3万
```

批量导入：

```text
导入持仓 覆盖
股票代码,股票名称,股票股数,买入价格
600519,贵州茅台,100,1500
300750,宁德时代,200,210
```

盘中持仓监控：

```bash
ACCOUNT=200000 PORTFOLIO=portfolio.csv ./run_position_monitor.sh
```

交易生命周期：

```bash
# 从最新买入信号生成 T+1 买入计划
python trade_manager.py --action from_signals --signals-out output --state trade_state.csv --out trade_output

# 同步实际持仓
python trade_manager.py --action sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000

# 盘中止损、止盈、加仓建议
python trade_manager.py --action advise --mode intraday --sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000

# 收盘后生成下一交易日操作计划
python trade_manager.py --action advise --mode close --sync --portfolio portfolio.csv --state trade_state.csv --out trade_output --account 200000
```

通过对话入口：

```text
生成交易计划
同步持仓交易状态
我的持仓后续怎么办
今天止损止盈怎么操作
收盘后给我明天操作计划
```

主要输出：

```text
position_output/latest_position_message.txt  持仓监控摘要
position_output/latest_position_actions.csv  持仓监控动作
trade_output/latest_trade_plan.txt           交易计划摘要
trade_output/latest_trade_actions.csv        结构化操作清单
trade_state.csv                              交易计划状态
```

## 空间清理

清理器只处理已知生成文件：`output`、`backtest_output`、`etf_output`、`fund_output`、`position_output`、`trade_output`、`cache` 下的策略缓存、备份目录、临时对比目录和 Python 缓存。不会删除源码、配置、股票池、ETF 池、持仓文件、`.git` 或 `.venv`。

标准清理：

```bash
python space_cleanup.py --config config.example.yml --out cleanup_output
```

预览清理，不实际删除：

```bash
python space_cleanup.py --config config.example.yml --out cleanup_output --dry-run
```

深度清理会删除更多历史输出和缓存，仍会保留 `latest_*` / `last_*` 最新结果：

```bash
python space_cleanup.py --config config.example.yml --out cleanup_output --aggressive
```

通过对话入口：

```bash
printf '%s\n' '清理空间' | ./stockbot_chat.sh
printf '%s\n' '预览清理空间' | ./stockbot_chat.sh
printf '%s\n' '深度清理空间' | ./stockbot_chat.sh
```

主要输出：

```text
cleanup_output/latest_space_cleanup_message.txt  清理摘要
cleanup_output/latest_space_cleanup_report.md    清理明细
```

## 对话入口

`stockbot_chat.sh` 会按消息内容路由到对应脚本。常用示例：

```bash
printf '%s\n' '生成买入信号' | ./stockbot_chat.sh
printf '%s\n' '用20万模拟账户回测近两年' | ACCOUNT=200000 ./stockbot_chat.sh
printf '%s\n' 'ETF建池' | ./stockbot_chat.sh
printf '%s\n' 'ETF轮动配置' | ETF_POOL=etf_pool.csv ACCOUNT=200000 ./stockbot_chat.sh
printf '%s\n' '基金定投计划' | FUND_DCA_BUDGET=5000 ./stockbot_chat.sh
printf '%s\n' '查看持仓' | ./stockbot_chat.sh
printf '%s\n' '我的持仓后续怎么办' | ./stockbot_chat.sh
printf '%s\n' '清理空间' | ./stockbot_chat.sh
```

## 数据可靠性

- 行情数据优先使用配置里的多数据源顺序，单源失败时自动切换或使用合格缓存。
- 实时行情旧缓存不会用于生成新的买入或交易动作，只会在报告中提示。
- ETF、基金定投、个股扫描使用独立缓存和输出目录，互不覆盖。
- 数据源失败率过高时，系统会抑制新交易信号，避免把坏数据当作可执行信号。

## 回归验证

提交前建议运行：

```bash
./run_regression.sh
```

该脚本会运行离线回归测试、检查关键 shell 脚本语法，并编译主要 Python 模块。
