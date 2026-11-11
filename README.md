# 行业订阅源的跨发布者修订归档

面向行业研究员的 Atom 采集归档器。同一条政策消息可能被协会、地方机构和通讯社
各自的订阅源分别发布，本系统在**发布者命名空间**下保留每个 entry 的身份、原始
内容摘要、链接与声明时间，完整记录修订与撤回（tombstone），并基于显式证据建立
转载关系，支持按历史采集时点重建当时已知的内容。

所有输入均为 `fixtures/` 下的虚构离线材料，不访问任何真实机构站点，不含个人
信息或附件。

## 核心语义

### 1. 身份按发布者命名空间隔离

- entry 身份是 `(publisher_id, entry_id)` 的组合，内部 UID 形如
  `publisher!entry-id#generation`。
- 不同 feed 出现相同 `entry_id` 属于不同实体，**绝不会互相覆盖**。
- feed 地址迁移只改变抓取位置，不改变身份：迁移前后的新旧地址登记在同一
  发布者配置里（`urls` + `migrated_from`），同 `entry_id` 仍是同一实体的修订。
  未登记的新地址不会自动继承旧来源的任何记录。

### 2. 修订与 tombstone 都保留

- 归档是只追加的事件日志（`runtime/archive/events.jsonl`），事件类型包括
  `revision / tombstone / revival / split / relation / candidate`。
- 内容指纹（标题+摘要+原文的哈希）变化才产生新修订；相同内容不产生空修订。
- 只有响应中显式的 `deleted-entry` 才产生 tombstone。feed 里“没出现”的旧条目
  **不会被推断为删除**。
- 撤回后同 ID 恢复、再撤回的完整链路均可追溯。

### 3. ID 重用：默认分裂，避免旧记录嫁接

当一个处于 deleted 状态的 entry ID 被重新用于发布内容时：

- 默认 `split` 策略：创建新一代实体（generation +1），通过 `predecessor_uid`
  记录血缘和分裂原因，旧实体的修订与撤回记录原样保留，不嫁接到新实体。
- 可选 `continue` 策略：只有配置的连续性证据全部满足（如
  `title-persists`、`link-href-persists`、`declared-updated-monotonic`）
  才在同一实体上恢复；任一证据不足仍分裂。

### 4. 转载关系：强证据确认，弱匹配只留候选

- `rel="canonical"` 链接能解析到某个实体的非 canonical 链接时，建立
  **确认关系**（`canonical-link`）。canonical 链接本身不作为实体身份链接，
  避免转载方抢占原作者的 URL。
- 也支持在配置中显式登记 `configured-evidence` 对应关系；证据强度未被配置
  接受时只作为候选。
- 仅标题相同等弱匹配保留为 `candidate`，绝不合并实体。
- canonical 指向的目标在当前归档中不存在时，记录为“待解析候选”。

### 5. 单源故障隔离

一次采集包含多个 feed。任一 feed 传输失败（HTTP 0）、返回非 200 或 XML 解析
失败时：

- 仅该 feed 标记为 `failed` 并跳过；
- 它的旧条目原样保留，不产生任何删除推断；
- 同一轮内其他已完整取得的 feed 结果正常写入，不回滚。

### 6. 304 必须区分两种情况

- 响应 304 且校验器（ETag / Last-Modified）与本地缓存**一致**：
  `not_modified`，确认内容未变，不重新摄入。
- 响应 304 但本地无缓存、无校验器或校验器对不上：
  `not_modified_no_cache`，表示无法确认内容对应任何本地版本，按不可用处理，
  不复用未知内容、不改动归档。
- 条件请求缓存（`runtime/cache/`）与归档分离，可单独丢失；缓存丢失后归档中的
  历史不受影响。

### 7. 时点重建

`Archive.view_at_run(n)` 重放到第 n 轮采集结束、`view_at_time(iso)` 重放到
指定观测时间，返回只读 `ArchiveView`，包含当时的实体状态、修订、关系与候选，
不会混入之后轮次才出现的世代或事件。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `app/atom.py` | Atom/tombstone 解析，保留身份、摘要、原文、链接、声明时间 |
| `app/model.py` | 实体/修订/撤回/关系数据模型、追加式事件归档与重放视图 |
| `app/config.py` | 发布者命名空间、地址迁移、ID 重用策略、转载证据配置 |
| `app/collector.py` | 离线抓取抽象、条件请求缓存、摄入、关系引擎、采集编排 |
| `app/inspect.py` | 跑通五轮离线场景的演示入口 |

## 离线场景（fixtures/offline）

五轮合成响应覆盖全部关键路径，组织、feed 与政策摘要均为虚构：

1. **round1**：协会发摘要、通讯社发原文、地方机构以自有 ID 转载并带 canonical。
2. **round2**：协会补发原文（产生修订）并 tombstone 一条旧通知；地方机构 304
   内容未变；通讯社传输故障。
3. **round3**：协会同 ID 重新出现（默认策略下分裂为新一代）；地方机构修订转载；
   通讯社 304。
4. **round4**：本地缓存整体丢失后收到 304，区分“未变”与“无可对应缓存版本”。
5. **round5**：协会先 tombstone 再重用该 ID 发布完全不同的内容，再次分裂，
   旧记录保持独立。

`script.json` 声明每轮各 URL 的离线响应（200/304/传输故障），响应体引用同目录
XML。另有多份 `sources*.json` 用于地址迁移、continue 策略与配置证据的测试。

## 本地运行

需要 Python 3.12+，仅用标准库。从项目根目录：

```sh
# 离线演示（写入并清理 runtime/）
python3 -m app.inspect

# 测试
python3 -m unittest discover -s tests -v

# 语法检查
python3 -m compileall -q app tests
```

处理结果写入 `runtime/`（已被 `.gitignore` 忽略），源 fixture 保持只读。
若将来连接真实来源，需先按来源方授权范围配置读取权限，本仓库不包含任何
真实站点调用。
