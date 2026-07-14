# 八强隐藏地图预测集

母版来自 `map_config_variant_a.json`，SHA-256：
`771aac158ab6fbaa8bfc76b95331a80467674b0d7c18c5a1562869658863d7dd`。

预测依据是组委会三条提示：首窗站存在资源、S14 仍必经、S14 前单点强堵
收益不稳定。六张图都保留 S14 为终点唯一入口，分别隔离一个高风险变量。

| 文件 | 变化 | 验证目标 |
|---|---|---|
| `01-s02-fast-horse.start.json` | S02 投放快马 | 先处理还是先抢资源 |
| `02-single-s10-bypass.start.json` | S09-S11 旁路 | S10 失效后动态墙/S14 接管 |
| `03-split-corridors.start.json` | 官水、山线各有旁路 | 只有 S14 汇合时的资源竞速 |
| `04-short-gate-edges.start.json` | S14 入边缩短至 4 帧内 | 必经但无法反应设卡时回落得分 |
| `05-optional-s02.start.json` | S02 有冰，山线明显更快 | 首窗可争但不一定值得争 |
| `06-resource-shuffle.start.json` | 马匹重排，S02 为短马 | 不依赖旧资源点硬编码 |

每个文件都是本地 `Arena(start_data=...)` 可直接读取的完整 start 消息。

`raw/` 目录另有一套可直接交给地图工具的原始 `map_config` JSON。它们保留
母版的 Unity 地形、图层、节点坐标和渲染字段，仅替换对应测试场景的道路、
资源与处理配置：

| 文件 | 实战测试重点 |
|---|---|
| `raw/01-s02-fast-horse.map_config.json` | 首窗站快马资源争夺 |
| `raw/02-single-s10-bypass.map_config.json` | 单旁路绕过 S10 |
| `raw/03-split-corridors.map_config.json` | 两条走廊均绕过 S10 |
| `raw/04-short-gate-edges.map_config.json` | S14 短入边，无法临边反应设卡 |
| `raw/05-optional-s02.map_config.json` | S02 可选，抢资源与绕行竞速 |
| `raw/06-resource-shuffle.map_config.json` | 资源点重排与双旁路组合 |

重新生成并运行全部探索矩阵：

```bash
python3 client/predicted_map_matrix.py \
  --export-dir client/test_maps/predicted
```

重新生成原始地图格式：

```bash
python3 client/predicted_map_matrix.py \
  --raw-map-source /path/to/map_config_variant_a.json \
  --raw-export-dir client/test_maps/predicted/raw \
  --topology-only
```

按地图或对手快速复现：

```bash
python3 client/predicted_map_matrix.py \
  --variant 03-split-corridors --opponent tip-rusher
```

`3.98.18` 的关键红灯：专门执行“先抢 S02 马再冲门”的对手会稳定击穿
`03` 和 `06`，双座位分别为 `426:437` 与 `418:430`。`3.98.19` 改为按
S02 马对 S14 竞速的真实收益决定是否争夺，并在宫门墙失去复卡能力后及时
交付；资源型冲锋矩阵现为 12/12 胜，其中原四个红灯均为 `399:0`。
