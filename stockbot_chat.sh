#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p output backtest_output position_output etf_output

ACCOUNT="${ACCOUNT:-200000}"
ETF_POOL="${ETF_POOL:-etf_pool.csv}"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

cat > "$tmp"

source .venv/bin/activate
msg="$(cat "$tmp")"

ensure_etf_pool() {
  if [ ! -f "$ETF_POOL" ]; then
    echo "ETF池文件不存在：$ETF_POOL"
    echo "可先执行：python etf_pool.py --config config.example.yml --pool-out \"$ETF_POOL\" --out etf_output"
    echo "也可以复制样例：cp etf_pool_sample.csv etf_pool.csv，或设置 ETF_POOL=/path/to/etf_pool.csv"
    exit 1
  fi
}

# ETF 建池：只生成 etf_pool.csv 和 etf_output/latest_etf_pool_*，不读写个股池。
if printf '%s' "$msg" | grep -Eq 'ETF.*(建池|构建.*池|生成.*池|更新.*池)|etf.*(建池|构建.*池|生成.*池|更新.*池)'; then
  cmd=(python etf_pool.py --config config.example.yml --pool-out "$ETF_POOL" --out etf_output)
  if [ -n "${ETF_POOL_MAX_SIZE:-}" ]; then
    cmd+=(--max-size "$ETF_POOL_MAX_SIZE")
  fi
  if [ -n "${ETF_POOL_MIN_AMOUNT:-}" ]; then
    cmd+=(--min-amount "$ETF_POOL_MIN_AMOUNT")
  fi
  if [ "${REFRESH:-0}" = "1" ]; then
    cmd+=(--refresh)
  fi
  if ! "${cmd[@]}" > etf_output/last_etf_pool_chat_run.log 2>&1; then
    echo "ETF建池执行失败："
    tail -120 etf_output/last_etf_pool_chat_run.log || true
    exit 1
  fi
  cat etf_output/latest_etf_pool_message.txt
  exit 0
fi

# ETF 轮动回测必须优先于普通个股回测，否则“ETF回测”会被普通回测规则截获。
if printf '%s' "$msg" | grep -Eq 'ETF.*(回测|模拟|backtest|Backtest)|etf.*(回测|模拟|backtest)'; then
  ensure_etf_pool
  if ! python etf_rotation.py \
    --mode backtest \
    --pool "$ETF_POOL" \
    --config config.example.yml \
    --out etf_output \
    --account "$ACCOUNT" \
    --years "${ETF_BACKTEST_YEARS:-3}" \
    --rebalance "${ETF_REBALANCE:-W-FRI}" \
    > etf_output/last_etf_rotation_backtest_chat_run.log 2>&1; then
    echo "ETF轮动回测执行失败："
    tail -120 etf_output/last_etf_rotation_backtest_chat_run.log || true
    exit 1
  fi
  cat etf_output/latest_etf_rotation_backtest_report.md
  exit 0
fi

# ETF 轮动配置：按全ETF池排序，输出当期组合。
if printf '%s' "$msg" | grep -Eq 'ETF.*(轮动|配置|组合|资产配置|调仓)|etf.*(轮动|配置|组合|资产配置|调仓)'; then
  ensure_etf_pool
  if ! python etf_rotation.py \
    --mode rotate \
    --pool "$ETF_POOL" \
    --config config.example.yml \
    --out etf_output \
    --account "$ACCOUNT" \
    > etf_output/last_etf_rotation_chat_run.log 2>&1; then
    echo "ETF轮动配置执行失败："
    tail -120 etf_output/last_etf_rotation_chat_run.log || true
    exit 1
  fi
  cat etf_output/latest_etf_rotation_message.txt
  exit 0
fi

# ETF 买点/信号扫描：单ETF趋势买点，不走个股股票池。
if printf '%s' "$msg" | grep -Eq 'ETF.*(信号|买点|扫描|策略)|etf.*(信号|买点|扫描|策略)'; then
  ensure_etf_pool
  if ! python etf_strategy.py \
    --pool "$ETF_POOL" \
    --config config.example.yml \
    --out etf_output \
    --account "$ACCOUNT" \
    > etf_output/last_etf_strategy_chat_run.log 2>&1; then
    echo "ETF信号扫描执行失败："
    tail -120 etf_output/last_etf_strategy_chat_run.log || true
    exit 1
  fi
  cat etf_output/latest_etf_message.txt
  exit 0
fi

if printf '%s' "$msg" | grep -Eq '回测|模拟交易|模拟账户|近两年|backtest|Backtest'; then
  if ! python backtest.py \
    --pool stock_pool.csv \
    --config config.example.yml \
    --out backtest_output \
    --account "$ACCOUNT" \
    --years 2 \
    > backtest_output/last_backtest_run.log 2>&1; then
    echo "回测执行失败："
    tail -120 backtest_output/last_backtest_run.log || true
    exit 1
  fi
  cat backtest_output/latest_backtest_message.txt
  exit 0
fi


# 持仓管理：查看/添加/修改/删除/清空/导入/设置资金，优先于盘中监控。
if printf '%s' "$msg" | grep -Eq '查看持仓|持仓列表|当前持仓|我的持仓|添加持仓|加入持仓|新增持仓|买入持仓|修改持仓|更新持仓|设置持仓|删除持仓|移除持仓|删掉持仓|清空持仓|重置持仓|清除持仓|导入持仓|覆盖持仓|追加持仓|设置.*(总资金|可用现金|可用资金|现金)|修改.*(总资金|可用现金|可用资金|现金)'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python portfolio_manager.py --portfolio "$PORTFOLIO" --message-file "$tmp" > position_output/last_portfolio_manage_run.log 2>&1; then
    echo "持仓管理执行失败："
    tail -120 position_output/last_portfolio_manage_run.log || true
    exit 1
  fi
  cat position_output/last_portfolio_manage_run.log
  exit 0
fi


# 交易计划生命周期：从买入信号生成待买、同步持仓、输出后续止损/止盈/趋势退出/加仓建议。
if printf '%s' "$msg" | grep -Eq '交易计划|操作计划|后续怎么办|后续怎么|买入后|止损止盈|止损/止盈|卖出计划|明天.*(买|卖)|次日.*(买|卖)|同步交易|更新交易状态|从信号生成|持仓后续|后续操作'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python trade_manager.py \
    --action auto \
    --message-file "$tmp" \
    --portfolio "$PORTFOLIO" \
    --state trade_state.csv \
    --signals-out output \
    --config config.example.yml \
    --out trade_output \
    --account "$ACCOUNT" \
    --sync \
    > trade_output/last_trade_manager_chat_run.log 2>&1; then
    echo "交易计划执行失败："
    tail -120 trade_output/last_trade_manager_chat_run.log || true
    exit 1
  fi
  cat trade_output/latest_trade_plan.txt
  exit 0
fi

# 持仓监控/交易建议：只读持仓，输出是否需要交易，不自动下单。
if printf '%s' "$msg" | grep -Eq '检查.*持仓|持仓.*检查|持仓.*交易建议|盘中|交易建议|止损|止盈|减仓|加仓|position|Position'; then
  PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
  if ! python position_monitor.py     --portfolio "$PORTFOLIO"     --config config.example.yml     --out position_output     --account "$ACCOUNT"     > position_output/last_position_chat_run.log 2>&1; then
    echo "持仓监控执行失败："
    tail -120 position_output/last_position_chat_run.log || true
    exit 1
  fi
  cat position_output/latest_position_message.txt
  exit 0
fi

if ! python main.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out output \
  --account "$ACCOUNT" \
  --tail \
  --chat-file "$tmp" \
  > output/last_chat_run.log 2>&1; then
  echo "股票池/信号命令执行失败："
  tail -80 output/last_chat_run.log || true
  exit 1
fi

cat output/latest_chat_reply.txt
