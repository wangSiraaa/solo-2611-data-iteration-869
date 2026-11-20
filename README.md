# 受限 YAML 配置目录服务与配置消费分析仓

Python 3.11 标准库实现（无第三方依赖），严格按题面合同控制类型、锚点/别名与
merge 语义，并把“有效配置 + 每键来源证据 + 关系表血缘”持久化到真实 SQLite。
在此基础上提供两厂离线编辑包的三路语义汇合，以及按移交号核对发出/收到两腿、
原子切代签日结的对账仓。

## 目录结构

```
confcat/
  errors.py         # 合同级错误类型（解析期/消费期分离，整次任务可回滚）
  model.py          # 原始共享图 RawNode 与物化有效树 MatNode、来源证据模型
  yaml_parser.py    # 受限 YAML 解析器（词法 + 递归下降，游标不变量统一）
  materialize.py    # 别名解引用/循环检测 + merge 合同物化 + 来源证据生成
  codec.py          # 物化树 JSON 编解码（保留 int/str 双键区分）与取值辅助
  catalog.py        # 配置产品目录：版本化发布、路径查询、哈希校验（SQLite）
  csv_pipeline.py   # CSV 读取与按配置归一化（列映射/缺失值/分区/列血缘）
  ingest.py         # 消费仓：类型化取参、整次拒绝、任务结果持久化（SQLite）
  semantic.py       # 离线编辑语义模型：声明/使用路径身份、编辑包、受限发射器
  merge3.py         # 三路语义汇合：别名按目标声明比较、保留未决冲突
  recon.py          # 对账仓：双厂双任务、原子切代、日结签单与冻结复核（SQLite）
  cli.py            # 人工检查命令行（含 merge / recon-* 子命令）
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后等 YAML 样例
data/               # 合成 CSV 与运行期 SQLite
demo/run_demo.py           # 原端到端演示（含断言）
demo/run_merge_recon.py    # 两厂离线汇合 -> 对账仓切换与日结演示（含断言）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、
                    #   三路汇合、对账双腿/切换并发/上千任务共用配置
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费；`semantic`/`merge3`
负责离线汇合，`recon` 负责跨厂对账。

## 合同实现要点

### 类型与标量
- 仅双/单引号字符串、十进制整数（`0` 或负号可选的非零首位，正则 `^(0|-?[1-9][0-9]*)$`）、
  `true`/`false`/`null`；其余未加引号标量（`yes`、`001`、日期、`1.5`、`-0`）一律字符串。
- 映射键仅允许 `str`/`int`；`1` 与 `"1"` 是两个键，编解码显式带 `kt` 类型标签持久化。
- 显式拒绝：自定义 tag（`!`）、flow 集合（`[`/`{`）、块标量（`|`/`>`）、
  Tab 缩进、重复显式键、bool/null 键。

### 锚点与别名
- 锚点作用域为**当前文档**：多文档同名锚点互不影响；前向/未知别名、重名锚点拒绝。
- 自引用可通过解析期（pending 锚点），循环在物化期经 active 集合确定性抛
  `CircularReferenceError`，别名链逐跳记录（别名 -> 锚点位置）。
- 原始图保留共享子结构（别名指向同一锚点节点）；物化时每个插入点生成**独立子树**，
  从根上保证消费方修改不可能经别名影响其他任务。

### merge 键 `<<`
- 裸 `<<` 走内部哨兵识别；引号 `"<<"` 是普通字符串键，不触发 merge。
- merge 值为映射或映射序列，否则 `MergeTypeError`。
- 序列从左到右：第一个提供的键胜出，后者只补缺；当前映射**显式键恒覆盖** merge，
  与 `<<` 文本位置无关（前/后均有测试）。
- 只复制“键 -> 值引用”，**不递归合并**同名子映射（整体占位）。
- 每个有效键的 `Provenance` 记录：值/键位置、是否显式、别名链、每个 merge 候选的
  `source_index/won/overridden_by` 覆盖证据。

### 目录与消费仓
- 目录可发布多个命名产品，每次发布产生不可变新版本；任务绑定产品+版本快照，
  其他产品或新版本发布不影响在途/已完成任务。
- 任务按固定路径类型化取得 `source_uri`(str)、`column_mapping`(str->str)、
  `missing_value`(可选)、`partition_by`(可选)。
- 路径缺失、错误类型、原 CSV 列缺失、列映射冲突、分区键缺失 -> **整次任务拒绝**，
  单事务回滚，任务与结果均不落库。
- 归一化表逐行写入 SQLite，每列保留目标列、原 CSV 列、表头行号与配置来源 Provenance。
- 重开目录/任务仓可回查全部结果与配置链；发布快照含源文本 SHA-256，`verify` 校验。

## 两厂离线汇合与对账仓切换

场景：两厂从同一产品离线编辑，各自改出一包配置，需要汇合后一起切换对账批次；
两站从本地 CSV 导入实际重量，按新系数换算，且只在同号两腿重量全部相等时签日结。

### 语义身份：别名按目标声明比较（`semantic.py`）
- 声明 `SDecl` 的身份是**结构路径**，锚点名只是可变属性；
- 使用路径 `SAlias` 只记录目标声明路径，**不记录别名拼写**；
- `EditPack` 以声明或消费路径为对象，支持：改共享定义 `set_shared`、
  给某条使用路径加同值局部覆盖 `add_override`、撤去覆盖 `remove_override`、
  换别名拼写 `alias`、删除 `delete`；
- 同值显式覆盖把节点从 `SAlias` 变为 `SScalar`，是真实修改（保留“以后独立”意图）；
- 别名链会被**规范化到最终真声明路径**，因此“换一种拼写仍指原声明”在汇合中算无变化。

### 三路汇合（`merge3.py`）
以共同版本为基，逐路径比较 `(base, left, right)`：
- 仅一侧改动 -> 采用该侧；两侧改成相同值 -> 采用；
- 两侧改成不同目标值 -> **保留未决冲突**（该处暂用 base，记录 `Conflict`）；
- 一侧删除、另一侧修改 -> 也记为冲突（`delete-modify`）；
- 互不相交的编辑自然合并；
- `catalog.publish_merge` 拒绝带未决冲突的结果；`resolve_conflict` 显式裁定后发布。

对应题面两个真值：

| 左支 | 右支 | 汇合后 A / B |
|------|------|--------------|
| A 显式 1（同值覆盖） | 共享改 2 | A=1、B=2（A 仍显式） |
| 仅把 A 的别名换拼写，仍指原声明 | 共享改 2 | A=2、B=2 |

### 对账仓（`recon.py`）
- **称重任务**：每厂各自 `create_weigh_task` 导入本地 CSV，按所绑目录版本系数把
  实际重量换算（`Decimal`，原重×系数）；任务版本冻结，旧任务永久可读旧版。
- **两腿配齐**：每个移交号需同时有发出腿（A）与收到腿（B），否则日结 `pending`
  原因 `incomplete`。
- **同号重量相等才签**：所有同号换算重量逐一相等才 `signed`，否则 `pending`
  `mismatch`。分裂系数（A=1,B=2）下原始两腿都 10 -> 10 vs 20 不得签；
  统一系数（均为 2）下各 20 可签；统一后若收到腿实为 9 -> 18 vs 20 仍不得签。
- **原子切代**：任务以“代”成对登记；`activate_generation` 单事务切换，
  `active_pair` 一次 JOIN 只取出同代两条任务，读者在切换期只见完整旧对或完整新对。
- **补交与重开**：晚到腿另立新一代并形成新日结；签单冻结当时两腿读数
  （任务、版本、原重、系数、换算），`reopen_slip` 按冻结来源复核，旧签单不被后续
  补交或切版改写。

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 两厂离线汇合 -> 对账仓原子切换 / 双腿日结 / 补交重开（含内置断言）
python3 demo/run_merge_recon.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 三路汇合（打印或写出汇合 YAML；可选直接发布到目录）
python -m confcat.cli merge base.yaml left.yaml right.yaml --out merged.yaml
python -m confcat.cli merge base.yaml left.yaml right.yaml --catalog-db data/catalog.db --product shared

# 对账仓 CLI（recon_db catalog_db ...）
python -m confcat.cli recon-task data/recon.db data/catalog.db A factory_a shared doc-0 a.csv
python -m confcat.cli recon-pair data/recon.db data/catalog.db P --register A B --activate 1
python -m confcat.cli recon-settle data/recon.db data/catalog.db S P
python -m confcat.cli recon-reopen data/recon.db data/catalog.db S-slip
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
