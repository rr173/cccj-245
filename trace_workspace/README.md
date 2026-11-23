# 离线展品追溯（纯标准库 · 磁盘后备 · 支持超内存输入）

撤展时巡场人员看得到展品装进了哪只周转箱，却常常只扫码搬走最外层箱子。
本工具为展会项目提供一个**离线追溯入口**：输入一届已验收的全目录报告、
盘点时刻的物资清册、各扫码点汇出的交接记录，以及待查展品编号和查询时段，
导出这些展品在该时段内的**去向与凭据**，供遗失排查使用。

纯 Python 标准库实现（SQLite 做磁盘台账 + 自带外排工具），无网络、无
第三方依赖；所有 Python 调用均以 `python3 -I` 隔离模式运行。

## 1. 输入契约

| 输入 | 形态 | 说明 |
| --- | --- | --- |
| 目录报告 | JSON | 已验收全目录：`{"accepted": true, "booth_ids": [...]}`，或 booth_collector 形态的完整报告（`complete=true` + `booths[].id`）。只取合法展位编号，不重新采集 |
| 物资清册 | JSONL，每行一物 | `obj_id` 唯一、`kind ∈ {exhibit, box}`、直接去向 `parent_id` + `parent_kind ∈ {location, box}`；箱可装箱 |
| 交接记录 | JSONL，行可乱序 | `record_id`、整数秒 `ts`、被搬 `obj_id`、`old_parent`、不同的 `new_parent`、`new_kind` |
| 查询参数 | JSON | `{"start", "end", "targets":[展品编号...]}`，非空左闭右开整数秒区间 |

另有一个固定的**公共收货区**地点 `RECEIVING`（可用 `--receiving-id` 改名），
与目录展位编号共同构成全部合法地点。

## 2. 规则实现要点

- **只纳入截止秒之前的记录**：`ts < end` 才进台账，`ts >= end` 一律忽略；
  交接必须晚于盘点（`ts > inventory_ts`）。
- **同秒 = 同一次交接**：同一秒的全部记录按整组终态判定合法性，
  不按输入行序逐条搬动。组内要求：每件物品至多移动一次、原去向都与
  交接前一致、新旧去向不同；交接后每条装箱链落到合法地点且不成环。
  典型例子「同秒把内箱放地面、把原外箱装进它」即使按某行序逐条搬会
  临时成环，整组终态无环即判合法（见 `tests/test_core.py::test_swap_same_second_is_legal`）。
- **外箱随动**：搬动外箱只改外箱自己的父指针，箱内全部物品的路径在
  重放时自然跟随；同一展位的两只箱仍是各自独立的去向。
- **段拆分规则**：对每件待查展品输出覆盖整个 `[start, end)`、互不重叠的
  连续段。路径（从展品经各层箱到最终地点）或凭据不变不拆段；**地点没变
  但装箱路径变化必须拆段**。起点之前的交接会决定第一段的路径与凭据。
- **凭据**：每一级关系标注来源——初始清册关系的凭据是该物件自己的
  物件号，交接产生的关系标注交接 `record_id`。

### 成环判定（磁盘并查集）

把「交接前森林删掉本组被搬边」后的每个连通分量收缩成一个并查集节点，
再逐条加入新边：新边两端已连通即交接后成环。并查集存放在 SQLite 临时表
`tmp_uf` 中，组成员判定走 `tmp_group` 索引，**同秒大组不占用与组规模成
正比的内存**。

## 3. 超内存设计

清册、交接日志、追溯结果都可能大于内存：

- 物件表、交接表、当前父指针、同秒组、并查集全部落在 SQLite（临时库，
  连接关闭即删）；Python 侧任何时刻只持有单条/单块记录；
- 交接重放的每条 SQL 都走索引，工作量只与「本组被搬物件及其装箱链」
  相关，**不为每次交接扫描整场库存，也不复制全量快照**；
- 历史路径重建用「逐节点边事件回放游标」（初始清册边 + 该节点有序交接
  边），服务端游标迭代，不物化单节点全部历史；
- 最终段结果先写临时 JSONL，再经 `expotrace/extmem.py` 的分块外排
  （多路归并、句柄数有界）产出按 `(展品编号, 起始秒)` 有序的交付文件；
- 同秒大组的边按块 `fetchmany` 处理。

## 4. 拒绝策略（整次查询失败，不留半份导出）

以下任一情况都以退出码 2、中文错误码拒绝，**包括问题发生在未查询的
箱子里**；失败时暂存目录与工作目录整体清理，上次成功结果与 `CURRENT`
原样保留：

不认识的待查编号、待查编号是箱子、清册成环/悬挂/编号重复/父箱不存在/
非法地点、截止前交接记录号重复、搬动不存在的物件、新去向不存在或指向
展品、原去向不符、新旧去向相同、同秒重复移动、交接后成环（含自包含）、
交接不晚于盘点、查询起点早于盘点、空查询区间、目录报告未验收。

## 5. 本地用法

```bash
cd trace_workspace
bash run_all.sh                  # 一键：样例→端到端→28 项单测→三档压力对账

# 或分步：
python3 -I scripts/build_examples.py
python3 -I scripts/run_trace.py run \
    --catalog examples/catalog.json \
    --manifest examples/manifest.jsonl \
    --handovers examples/handovers.jsonl \
    --query examples/query.json \
    --inventory-ts 1000 --out ./out
python3 -I scripts/run_trace.py show   --out ./out   # 查看 CURRENT 的 manifest
python3 -I scripts/run_trace.py verify ./out         # 独立核验
```

也可用 `PYTHONPATH=. python3 -I -m expotrace.cli ...`（`-I` 不把当前目录
加入 `sys.path`，所以仓库提供 `scripts/run_trace.py` 包装器）。

### 输出（版本化、原子交付）

```
out/
  CURRENT                 # 指向最新成功的 trace-NNNNNN
  trace-000001/
    manifest.json         # 参数、合法展位、输入计数、输出计数
    segments.jsonl        # 每行一段：展品编号、段首尾、有序路径与每级凭据
```

段文件每行示例：

```json
{"end":1300,"exhibit_id":"E1","start":1050,
 "path":[{"evidence":"R1","from":"E1","parent_kind":"box","to":"C3"},
         {"evidence":"C3","from":"C3","parent_kind":"location","to":"B2"}]}
```

## 6. 独立核验（`expotrace/verifier.py`）

核验不相信生成过程，只从产物自身检查：段按 `(展品编号, 起始秒)` 严格
有序；每展品的段连续、互不重叠且恰好覆盖查询区间；每条路径从展品自身
出发、逐级经箱到合法地点（展位或收货区）收尾、节点不重复（无环）、
每级凭据非空；段数与 manifest 计数一致。篡改段文件或让路径落到非法
地点都会被拒绝（见 `tests/test_core.py::TestVerifier`）。

## 7. 可复跑小样例与压力测试

- `examples/` 是可人工审计的固定小样例（6 物件、6 条乱序交接，含同秒
  内外箱互换、外箱整体搬动、收货区落地、截止秒外记录被忽略）；
- `scripts/stress_test.py` **现场生成**大输入并与一个独立的纯 Python
  参考实现（dict 森林 + 逐节点事件回放）逐展品逐段对账，比对段边界、
  路径与凭据完全一致；数据默认写临时目录、跑完即删，**仓库不携带大文件**：
  - `--small`：500 物件快速自检；
  - `--objects 20000 --groups 60`：大库存多交接组；
  - `--big-group --objects 3000`：单同秒组移动全部箱子 + 3001 层深链，
    专门压磁盘并查集与深链重建。

## 8. 目录结构

```
expotrace/
  errors.py     错误类型体系（CLI 统一退出码 2）
  storage.py    SQLite 磁盘台账、清册森林校验、同秒组校验与磁盘并查集
  engine.py     装载校验、交接重放、边事件回放、路径与段构建、顶层入口
  extmem.py     分块外排与 JSONL 流工具（最终段排序用）
  verifier.py   独立核验
  cli.py        命令行入口（run/show/verify）
scripts/        样例生成、CLI 包装、压力对账
tests/          28 项机制级单元测试
examples/       固定小样例
out/            版本化交付目录（运行产物，已 gitignore）
```

## 9. 已知边界

- 目录报告只用来确认合法展位编号，不做跨届身份处理，也不重新采集；
- 路径深度受 SQLite 查询次数与单条结果大小约束，深链场景已在
  3000 层下验证；业务上装箱层级通常远浅于此；
- 时间为整数秒、区间左闭右开，不支持亚秒精度。
