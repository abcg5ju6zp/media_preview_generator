# Plex 刷新待办持久化与后台恢复 实施计划

## Repository Research（现状调研）

### 现有刷新链路

- [plex_refresh.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/processing/plex_refresh.py)
  - `PlexRefreshQueue`：**纯内存**、容量 128 的 `OrderedDict`，键为 `(server_id, canonical_path)`；单守护线程"plex-preview-notifications"，空闲 30s 自动退出。
  - `enqueue` 容量满直接返回 `False`（仅打一条 warning）；`close()`（atexit）清空 `_pending`，进程内未完成通知全部丢失。
  - worker 调用 `PlexServer.refresh_preview_metadata(path, item_id)`：返回 `True`（Plex 接受 Analyze）后才以精确 token 调 `clear_plex_refresh_pending`；异常/返回 False 仅置 `_completed=False` 供章节屏障判断，不重试。
  - `wait()` 是章节注册前的 Analyze 屏障：只看内存 `_pending/_inflight/_completed`。
- [multi_server.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/processing/multi_server.py#L1225-L1254)
  - `_refresh_after_publish`：新输出走 `mark_plex_refresh_pending`（写 journal、发新 token）后 enqueue；旧输出走 `get_plex_refresh_pending`，**只有将来某个作业碰巧又处理同一媒体时**才会把 journal 里遗留的失败标记重新 enqueue。非 Plex 服务器走 `server.trigger_refresh(...)`（Emby/Jellyfin 即时刷新）。
- [journal.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/output/journal.py)
  - 每个输出文件旁的 `.meta` sidecar，`plex_refresh_pending` 保存 marker：`{path, server_id, source_fingerprint, token}`。同一 `(path, server_id)` 新发布会用新 token 取代旧 marker（指纹也更新）；共享 bundle 上不同源副本、不同服务器的 marker 互相保留；`clear_meta(..., preserve_plex_refresh_pending=True)` 也保留他人 marker。
  - 旧版无 journal 输出：`mark_plex_refresh_pending` 因源未盖章返回 `None`，即"无需 Analyze 确认"，跳过检查。
- [plex.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/servers/plex.py#L2126-L2150)
  - `refresh_preview_metadata`：无 item_id 时先触发分区扫描再反查 ratingKey；查不到返回 `False`（未决，不是失败）；非数字 id 抛 `ValueError`；最终 PUT `/library/metadata/<id>/analyze`，成功返回 `True`。Analyze 是幂等 PUT。
- [registry.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/servers/registry.py)：`ServerRegistry.from_settings(media_servers)` 每次从 settings 重建 live client；`ServerConfig.enabled` 表示服务器是否禁用；删除服务器即从 settings 移除（[api_servers.py DELETE](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/web/routes/api_servers.py#L879-L923)，无软删除）。
- [app.py](file:///Users/ding/Documents/swe/0910091/project-06/media_preview_generator/web/app.py#L680-L1010)：`create_app(config_dir)` 是唯一启动装配点；settings、作业恢复、调度器都在此初始化；config_dir 默认 `/config`（`CONFIG_DIR`）。
- 测试约束：[tests/conftest.py](file:///Users/ding/Documents/swe/0910091/project-06/tests/conftest.py#L1290-L1303) 有 **autouse** fixture 把 `multi_server.enqueue_plex_refresh` mock 掉，故管线测试不会触达模块级队列；[test_plex_refresh.py](file:///Users/ding/Documents/swe/0910091/project-06/tests/test_plex_refresh.py) 与 [test_chapter_refresh_barrier.py](file:///Users/ding/Documents/swe/0910091/project-06/tests/test_chapter_refresh_barrier.py) 直接构造 `PlexRefreshQueue(capacity=..., idle_seconds=...)` 并断言现有内存语义，必须保持兼容。

### 问题归结

预览已发布但 Analyze 失败/未决时，唯一的重试依据是输出旁 journal；它只能被动等待"同一媒体的下一个作业"。队列满、Plex 离线、进程重启后若再无新作业，该 Analyze 永久丢失。需要一个**独立于媒体作业、跨重启、按服务器限速推进的持久待办**，且不能破坏即时刷新、共享 bundle、章节屏障、旧版无 journal 输出和非 Plex 发布的既有行为。

## Files and Modules（改动范围）

- `media_preview_generator/processing/plex_refresh_backlog.py`（**新增**）：持久待办存储 + 后台恢复 worker。
- `media_preview_generator/output/journal.py`：新增 marker 现状分类只读助手（不改写 journal 既有函数行为）。
- `media_preview_generator/processing/plex_refresh.py`：即时队列与持久 backlog 的衔接（类与模块函数签名向后兼容）。
- `media_preview_generator/web/app.py`：启动时配置/装载/启动 backlog，退出时关闭。
- `media_preview_generator/web/routes/api_plex.py`：新增只读状态端点，提供可观察结果。
- `tests/test_plex_refresh_backlog.py`（**新增**）：持久化与恢复语义测试。
- 回归（只运行、预期不改）：`tests/test_plex_refresh.py`、`tests/test_chapter_refresh_barrier.py`、`tests/test_output_journal.py`、`tests/test_processing_multi_server.py`。
- `multi_server.py` 不需要语义改动：仍调用 `enqueue_plex_refresh`；token 为 `None` 的旧版路径天然不进入持久 backlog。

## 详细设计

### 1. journal marker 分类（output/journal.py）

新增：

```python
def classify_plex_refresh_marker(output_paths, canonical_path, server_id, token,
                                *, source_fingerprint=None) -> str
```

返回常量：

- `"current"`：至少一个**存在且非空**的输出，其 `.meta` 中存在精确匹配 `(path, server_id, fingerprint, token)` 的 marker；
- `"superseded"`：无精确匹配，但现存输出的 `.meta` 上同一 `(path, server_id)` 存在**其他 token/指纹**的 marker（新发布已取代本记录）——调用方只能淘汰本记录，**不得**清除新 marker；
- `"cleared"`：输出都在、`.meta` 可读，但已无任何相关 marker（已被接受方确认，或 journal 被重写）；
- `"output_missing"`：输出集合中没有任何现存输出。

实现复用现有 `_read_meta/_pending_refreshes/_has_content/_WRITE_LOCK`；纯只读，不改变任何文件。

### 2. 持久待办（新模块 processing/plex_refresh_backlog.py）

**记录模型** `RefreshTodo`（dataclass）：

- 身份与发布信息：`server_id`、`canonical_path`、`item_id`、`output_paths: tuple[str, ...]`、`token: str`、`source_fingerprint: tuple[int,...]`；
- 调度字段：`created_at`、`attempts`、`not_before`（monotonic 由 worker 时钟换算，持久化为 epoch 秒）、`last_attempt_at`、`last_error`；
- 终态记录：`terminal_reason`（`superseded` / `server_deleted` / `server_no_longer_plex` / `output_gone` / `source_replaced`）、`terminated_at`，仅保留最近 N=200 条且不超过 7 天，供观察。

**存储** `RefreshBacklogStore`：

- 文件 `<config_dir>/plex_refresh_pending.json`，`{"schema": 1, "todos": [...], "terminated": [...]}`；`tmp + os.replace` 原子写（仿 journal），进程内锁串行化读改写；
- 记录以 **token 为主键** upsert（同一发布被后续作业重复 enqueue 时合并、刷新 `item_id/output_paths`，重置 `not_before` 以重新给即时队列机会，不增加 attempts）；
- upsert 时若存在同 `(server_id, canonical_path)` 但 token 不同的活动记录，将旧记录置终态 `superseded`（journal 中的新 marker 由既有 `mark_plex_refresh_pending` 写入，backlog **不调用任何 clear**，保证不能确认新发布）；
- 文件损坏/不可写：读按空处理（warning 一次），写失败降级为仅内存并周期重试落盘，不阻塞 enqueue 与派发。

**后台 worker** `PlexRefreshBacklog`（单守护线程，条件变量唤醒，可注入时钟/退避/轮询参数以便快速测试）：

- 构造参数：`store`、`server_provider: Callable[[], ServerRegistry | None]`、即时队列引用（可为 None）；`min_server_interval`（默认 3s/服务器）、`poll_interval`（默认 2s）、`immediate_grace`（默认 60s：新 upsert 的记录先留给即时队列）、退避表默认复用 `retry_queue.BACKOFF_SCHEDULE`（经 `scaled_backoff_delay` 思路，attempts 超表取末档）。
- **启动装载校验**：逐条用 `classify_plex_refresh_marker` + 输出存在性校验，只保留 `"current"`；`superseded`/`output_missing` 直接落终态并各打一条 WARNING（可观察、不可重试）；`cleared` 视为已确认静默移除；装载后活动记录的 `not_before` 重置为"启动后 1–2s"，使关闭前在飞的请求由下次启动接管。
- 每个轮询周期：
  1. 经 provider 取 registry（默认 provider 读 `SettingsManager.get("media_servers")` 并 `ServerRegistry.from_settings`；按 media_servers 原始 JSON 签名缓存，避免每轮重建客户端）；
  2. 服务器级裁决，**不影响其他服务器**：
     - registry 中无该 `server_id` → 该服务器全部活动记录置 `server_deleted` 终态（WARNING，含服务器/文件信息）；
     - 配置存在但 `enabled=False` → **暂停**其记录（不计失败、不推进退避，状态快照中可见 paused），重新启用后下一轮自动恢复；
     - 类型已不是 Plex → `server_no_longer_plex` 终态；
  3. 选记录：`not_before <= now`、所属服务器闸门已开放（距该服务器上次尝试 ≥ min_server_interval）、且即时队列中没有同键 pending/in-flight（`immediate.is_active(server_id, path)`）；跨服务器挑选最早到期候选，A 服务器退避/暂停不阻塞 B；
  4. 派发前重新校验：源文件 stat 指纹与记录不一致 → `source_replaced` 终态（stat 失败属不确定，按失败重试）；journal 分类非 `"current"` → 按第 1 节映射终态（superseded/cleared/output_gone）；
  5. 调 `server.refresh_preview_metadata(path, item_id)`：
     - `True`（明确接受）→ `clear_plex_refresh_pending(精确 token/指纹)` + store 移除 + 通知即时队列 `report_external(server, path, True)`（唤醒章节屏障）；
     - `False`（未索引，未决）→ 允许幂等重发：attempts+1、按退避置 `not_before`、记录原因；
     - 异常 → 同上保留待办并记 `last_error`；HTTP 429 若带 `Retry-After` 则优先采用；远端 4xx 拒绝也只记原因+退避（可调度，不终态）；
  6. 每次尝试都设置该服务器的限速闸门；无候选时在条件变量上等待到最近 `not_before`/闸门时间（上限 poll_interval）。
- 快照 API：`snapshot()` 返回 `active`（含 next_retry_in、attempts、last_error、paused 标记）、`paused_server_ids`、`terminated` 汇总与明细；`has_active(server_id, path)` 供即时队列 wait() 回查。
- `close()`：停止线程（守护 + 有界 join），数据已逐条原子持久化，无需额外刷盘。

### 3. 即时队列衔接（processing/plex_refresh.py）

保持 `PlexRefreshQueue(capacity, idle_seconds)` 现有构造与内存语义不变（现有测试零改动通过），新增可选 `backlog` 协作：

- `enqueue`：当 `notification_token` 非空且 backlog 已配置时，**先** `backlog.upsert(...)`（持久成功不依赖内存容量；内存已满返回 `False` 或队列已关闭都不影响持久待办的可调度性），再走现有内存即时路径；token 为 None 的旧版无 journal 路径只做即时通知、不入持久 backlog。
- worker 尝试结束：成功 → 现有 `clear_plex_refresh_pending` 之外 `backlog.ack(token)`；失败/False → `backlog.note_result(token, ...)`（记录尝试与退避；backlog 的 grace/在飞互斥保证不会与后台重复发送）。
- `wait()`：内存三集合无证据时，若 backlog 中该键仍有活动记录，则按"未决"参与等待（章节屏障继续等到后台确认或超时）；backlog 为 None 时与现行为完全一致。
- 新增内部 `is_active(server_id, path)` 与 `report_external(server_id, path, success)`（后者把后台结果写入 `_completed` 并 notify，供屏障/`wait` 观察）。
- 模块级新增 `configure_plex_refresh_backlog(config_dir)`：以默认 settings provider 创建并启动单例 backlog、挂 atexit close、绑定到模块 `_queue`；未配置时所有模块函数行为同今天（测试直接 new 的队列 backlog=None）。

### 4. 启动装配（web/app.py）

在 `create_app` 中 settings manager 初始化/迁移完成之后（作业恢复之前即可），调用 `configure_plex_refresh_backlog(config_dir)`：构造即完成启动装载校验并启动守护线程；关闭由 atexit 处理（与 scheduler 同样模式）。后台线程不阻塞 `create_app` 返回。

### 5. 可观察端点（web/routes/api_plex.py）

`GET /api/plex/refresh-backlog`（`@setup_or_auth_required`）：返回

```json
{
  "active": [{"server_id","server_name","canonical_path","attempts","next_retry_in","last_error","paused"}],
  "counts": {"active": n, "paused": n},
  "paused_server_ids": [],
  "terminated": {"superseded": n, "server_deleted": n, "output_gone": n, "source_replaced": n, "...": n},
  "terminated_recent": [{"server_id","canonical_path","reason","at"}]
}
```

未配置/文件不存在时返回空骨架。终态同时以 WARNING 写入应用日志（Logs 页可观察）。

## 需求逐条对照

1. **脱离后续媒体作业持续推进**：持久 backlog 守护线程独立派发，不依赖任何作业触发。
2. **保留服务器/源身份/输出集合/token**：`RefreshTodo` 字段齐备，token 为主键。
3. **启动及后台恢复只装载对应当前输出的记录**：启动与每次派发前用输出存在性 + journal 精确 marker 分类校验。
4. **按服务器限速重试**：每服务器尝试间隔闸门 + 每记录退避，跨服务器选 due 候选。
5. **Plex 明确接受后才清除同一 token**：仅 `refresh_preview_metadata is True` 后精确 token clear + ack。
6. **结果不确定允许幂等重发**：False/异常/超时/429 均保留待办退避重发；Analyze 为幂等 PUT。
7. **源被替换/同输出更新发布只淘汰被取代记录、不能确认新发布**：upsert 取代与 journal `superseded` 分类只终止旧 token，绝不调用 clear 触碰新 marker。
8. **容量不足/远端拒绝/进程关闭**：容量满仍持久留可调度待办；拒绝留原因+退避；关闭不丢已持久记录、下次启动 1–2s 内接管；单服务器故障不阻塞其他服务器。
9. **服务器禁用暂停、启用恢复**：enabled 裁决每轮进行，暂停不推进退避。
10. **服务器删除/输出消失的可观察终止**：终态记录（原因+时间）+ WARNING 日志 + 只读 API。
11. **既有行为不变**：即时内存队列语义/签名不变；共享 bundle 的多 marker 并存沿用 journal 逻辑；章节 wait 屏障保留（并可观察后台结果）；旧版无 journal（token=None）只即时通知；非 Plex 仍走 `trigger_refresh`。

## Implementation Steps（依赖顺序）

1. `output/journal.py`：新增 marker 分类常量与 `classify_plex_refresh_marker`（纯只读，附单测）。
2. 新增 `processing/plex_refresh_backlog.py`：`RefreshTodo`、JSON 原子存储、终态保留、启动校验、带服务器闸门/退避/暂停/终态裁决的守护 worker、快照与查询接口（时钟与间隔全部可注入）。
3. `processing/plex_refresh.py`：队列与 backlog 衔接（upsert/ack/note_result/is_active/report_external/wait 回查）、模块单例 `configure_plex_refresh_backlog` 与 atexit。
4. `web/app.py`：启动装配一行 + 必要导入。
5. `web/routes/api_plex.py`：只读状态端点。
6. 新增 `tests/test_plex_refresh_backlog.py`：覆盖持久化/重启接管、幂等重发、取代不确认新发布、输出消失/服务器删除/禁用-启用/源替换终态、容量满仍可调度、故障服务器不阻塞其他服务器、启动只装载 current、等待屏障观察后台结果、token=None 不入持久层。
7. 运行回归：`tests/test_plex_refresh.py`、`test_chapter_refresh_barrier.py`、`test_output_journal.py`、`test_processing_multi_server.py`，以及 `make test` 全量单元套件。

## Dependencies and Considerations（依赖与注意）

- 持久文件放在 config_dir（与 `settings.json`、`jobs.db` 同级），容器卷天然持久；文件名 `plex_refresh_pending.json`。
- autouse fixture mock 了管线侧入口，e2e 启动 app 时 backlog 使用临时 config_dir，不会污染开发机 `/config`；未显式 configure 时模块函数维持无 backlog 行为。
- `ServerRegistry.from_settings` 仅构造客户端、不发起网络连接；仍以 media_servers 签名缓存并在变化时重建。
- 即时队列与 backlog 双写窗口：以 token 主键 upsert + immediate grace + `is_active` 在飞互斥避免重复发送；即便重复，PUT Analyze 幂等且 clear 仅精确 token。
- 不新增设置项、不改 UI、不动数据库 schema；退避复用 `BACKOFF_SCHEDULE` 既有节奏。
- 共享 bundle：记录携带自己的输出集合；接受只 clear 自己的 marker，他人 marker 由 journal 现有保留逻辑保护。

## Validation（验证）

- 新增与相关单元测试全部通过（pytest，直接运行新文件 + 四个既有回归文件）。
- `make test` 全量单元套件通过；lint/format 与仓库预提交一致（ruff/mypy 风格遵循现有代码）。
- 手工代码走查：进程 kill -9 后重启，临时 config_dir 中待办文件被重新装载并在 Plex mock 接受后移除、journal marker 同步清除。

## Risks（风险与对策）

- **重复 Analyze**：grace 期 + 在飞互斥 + token 精确确认；幂等 PUT 兜底。
- **持久文件损坏/磁盘只读**：读损坏按空集合并 warning；写失败降级内存、周期重试落盘，不阻塞通知链路。
- **后台线程在测试中泄漏**：线程守护化、close 有界 join、轮询/退避参数可注入小值；新测试统一 try/finally close。
- **服务器配置频繁变化导致误终态**：删除是 settings 中真实移除才终态；禁用只暂停；终态保留 7 天/200 条可观察可核对，重建同 id 服务器不影响已终态记录（新发布生成新记录）。
- **等待屏障语义变化**：backlog 缺席时 `wait()` 逐行保持旧逻辑；仅在内存无证据且确有持久活动记录时按未决处理，BIF-only（required=False）调用不被后台记录阻塞。
