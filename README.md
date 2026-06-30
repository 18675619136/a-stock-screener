# A股选股策略框架 📊

模块化、可插拔的A股全市场选股引擎。

## 快速开始

```bash
# 列出所有策略
python3 -m strategies.run --list

# 运行默认策略（动量+均线+量能）
python3 -m strategies.run momentum_ma

# 测试模式（只查前10只）
python3 -m strategies.run momentum_ma --limit 10

# 运行金叉策略
python3 -m strategies.run golden_cross

# 自定义参数
python3 -m strategies.run momentum_ma -c '{"max_mv": 500, "top_n": 20}'

# 保存结果
python3 -m strategies.run momentum_ma --save
```

## 架构

```
screening/strategies/
├── base.py               # StrategyBase + StrategyContext + ScreeningResult
├── registry.py           # @register_strategy 装饰器 + 策略注册表
├── engine.py             # 核心引擎（数据获取 + 策略调度）
├── config.py             # 全局配置 + 策略默认参数
├── run.py                # CLI 入口
├── data/fetcher.py       # 数据获取（新浪 + 腾讯API）
└── builtins/
    ├── momentum_ma.py    # [默认] 动量+均线+量能+中小市值多因子策略
    └── golden_cross.py   # MA5上穿MA18金叉 + 热门赛道策略
```

## 添加新策略

1. 在 `builtins/` 下新建文件
2. 继承 `StrategyBase`，实现 `run()` 方法
3. 用 `@register_strategy` 装饰器注册

```python
from strategies.base import StrategyBase, StrategyContext
from strategies.registry import register_strategy

@register_strategy
class MyStrategy(StrategyBase):
    name = "my_strategy"
    description = "我的自定义策略"

    def run(self, context: StrategyContext) -> list[dict]:
        # context.all_stocks  - 全A股列表
        # context.market_data - 市值/股本数据
        # context.klines      - K线数据（懒加载）
        # context.config      - 策略配置参数
        return [...]  # 每项含 code, name, price, mv, score
```

## 默认策略：momentum_ma

五因子加权评分：

| 因子 | 权重 | 逻辑 |
|------|------|------|
| 动量 | 25% | 当日涨幅正分 |
| 均线排列 | 25% | MA5 > MA20 + 偏离强度 |
| 量能放大 | 15% | 当日量 > 20日均量×1.5 |
| 小市值 | 20% | 市值越小分越高 |
| 价格位置 | 15% | 在20日区间20%-60%位置最佳 |
