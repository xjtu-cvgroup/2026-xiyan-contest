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
重新生成并运行全部探索矩阵：

```bash
python3 client/predicted_map_matrix.py \
  --export-dir client/test_maps/predicted
```

按地图或对手快速复现：

```bash
python3 client/predicted_map_matrix.py \
  --variant 03-split-corridors --opponent tip-rusher
```

当前 `3.98.18` 的关键结果：普通冲锋/官道农夫 24/24 胜；专门执行“先抢
S02 马再冲门”的对手会稳定击穿 `03` 和 `06`，双座位分别为 `426:437`
与 `418:430`。这两张是后续策略修改前必须保持的红灯语料。
