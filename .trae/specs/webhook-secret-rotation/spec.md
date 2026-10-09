# Webhook 密钥可恢复多服务器轮换 — 产品需求文档

## Overview
- **Summary**: 将"管理员轮换 webhook 密钥"从"立即写新值 + 尽力逐台重注册 Plex"改造为**可恢复的多服务器轮换协议**：轮换开始后新旧密钥在受限过渡期内均可鉴权；每台启用的 Plex 服务器分别持久化"新 URL 已确认 / 旧 URL 已移除"状态；全部收敛（或过渡期到期）后才撤销旧密钥；过程可跨进程重启恢复，并与手动注册/注销、服务器删除/禁用仲裁。
- **Purpose**: 消除当前轮换窗口内离线 Plex 继续向携带旧 token 的 URL 投递、媒体事件被全部 401 拒绝的事故面；让轮换结果对管理员可见、可恢复、可审计，且不泄露密钥。
- **Target Users**: 在设置页轮换 webhook 密钥的管理员；拥有 1..N 台 Plex Media Server（可能有 Emby/Jellyfin 并存）的部署者。

## Goals
- 轮换期间投递不中断：旧 URL（携带旧 token）与新 URL（携带新 token）在过渡期内均通过鉴权。
- 每台启用的 Plex 独立跟踪远端收敛状态：先确认新 URL 已在 plex.tv 远端列表中，再删除旧 URL，再确认旧 URL 消失；顺序不可逾越。
- 失败三态分明：Plex 确定性拒绝 → 保留旧注册并记录原因；网络响应不确定 → 只通过复核远端列表推进、不做盲写；进程退出 → 重启后从持久化状态继续。
- 幂等：重复保存同一密钥不开启新一轮、不产生任何重复注册；远端写操作一律先 list 后写，不重复添加 URL。
- 仲裁：手动注册/注销与进行中的轮换协调；删除或禁用服务器确定地解除其待办，不阻塞全局收敛。
- 可观测：状态接口按服务器返回当前代次（generation）与失败原因，任何响应/日志均不泄露密钥或带 token 的 URL。
- 兼容：未配置密钥、非 Plex 服务器、普通 webhook 鉴权链路、既有 URL 规范化行为保持不变。

## Non-Goals
- 不改动 Radarr/Sonarr/Emby/Jellyfin 等非 Plex 发送方的注册或鉴权模型（Emby/Jellyfin 走 header，天然按当前值鉴权）。
- 不引入每服务器独立 webhook 密钥（v10 迁移已移除该特性，继续沿用全局单一密钥）。
- 不改变 plex.tv/Pass 的账号侧语义，不做 Plex API 之外的通道。
- 不提供强制撤销/取消轮换的管理员 UI（保留"立即重试验收"动作；未来可扩展）。
- 不重写既有的 URL 路径迁移（`/plex` → `/incoming` → 每服务器 `/server/<id>`）逻辑。

## Background & Context
- 现状链路：
  - `POST /api/settings`（`web/routes/api_settings.py`）在校验后立即 `settings.update()` 写入新 `webhook_secret`，随后 `_apply_post_save_hooks` 同步调用 `_reregister_plex_webhooks_after_secret_rotation`，对每台带 token + `output.webhook_public_url` 的 Plex **尽力**调用 `plex_webhook_registration.register(token, url, auth_token=新密钥)`；异常仅 warning。
  - `register()`（`web/plex_webhook_registration.py`）会先 list 远端、删除同 base URL 的全部旧变体、再 addWebhook 新 URL；任一台服务器离线时其旧 URL 留在 plex.tv，而本地只认新密钥 → 事件全部 401。
  - 入站鉴权在 `web/webhooks.py::_authenticate_webhook`：仅比对当前 `webhook_secret` 与 app API token。
  - 手动端点：`/api/settings/plex_webhook/register|unregister|status`（`web/routes/api_plex_webhook.py`）。
  - 服务器删除：`DELETE /api/servers/<id>`（尽力 unregister 后删条目）；禁用：`PATCH /api/servers/<id>/enabled`（仅翻标志）。
- 持久化约定：`/config`（`CONFIG_DIR`）下 JSON 文件经 `utils.atomic_json_save_with_backup` 原子写、敏感文件 0600（settings.json 即如此）；后台守护线程模式见 `app.py::_prewarm_caches`；启动恢复钩子集中在 `create_app()`。
- 现有测试 `tests/test_webhook_secret_rotation.py` 固定的是旧的"尽力重注册"契约，需按新协议重写（其隔离性/跳过非 Plex 等保证在新模型下仍保留对应验收）。

## Functional Requirements

### FR-1 轮换轮次与持久化状态
- **FR-1.1**: 当管理员保存的 `webhook_secret` 与当前值**不同且非空**、且当前已存在非空旧密钥时，系统开启一轮轮换：代次（generation，单调递增整数）+1，持久化轮次状态（启动时间、过渡期截止时间、旧密钥链、每台启用 Plex 的服务器级状态），新值照常作为当前 `webhook_secret` 立即生效。
- **FR-1.2**: 轮次状态独立存于 `$CONFIG_DIR/webhook_secret_rotation.json`（原子写、权限 0600）；进程重启后 `create_app()` 启动恢复，未完成的轮换从该文件继续，鉴权与远端收敛均不依赖进程内存。
- **FR-1.3**: 首次设置密钥（原先无密钥）不开启轮换，沿用既有"为配置了 public_url 的 Plex 尽力注册新 URL"行为。
- **FR-1.4**: 保存的值与当前密钥相同（含去除空白后相同、掩码 `****`、空串）时：不开启新一轮、代次不递增、不触发任何 plex.tv list/add/delete 调用。
- **FR-1.5**: 轮换进行中保存另一个不同的新密钥时，合并为**新一代**：代次+1；上一轮的"上一密钥"与"当前密钥"作为旧密钥链携带；每个服务器的状态按新一代重置目标（以最新密钥 URL 为新 URL），但已确认的远端事实需经重新 list 复核。
- **FR-1.6**: 轮次的参与集合 = 当前设置中所有**启用的 Plex**服务器；缺少 token 或 `webhook_public_url` 的服务器在开启时即以"无 webhook 配置"终态记账，不阻塞收敛；轮换期间新增的启用 Plex 自动加入当前代次。

### FR-2 过渡期双密钥鉴权
- **FR-2.1**: 轮换进行且未过截止时间时，入站 webhook 携带当前密钥**或**本轮旧密钥链中任一密钥均通过鉴权；旧密钥只在"本轮状态中的新密钥 == settings 中当前密钥"时有效（防止陈旧状态文件放宽鉴权）。
- **FR-2.2**: 过渡期默认 24 小时，可由环境变量 `WEBHOOK_SECRET_ROTATION_GRACE_HOURS` 覆盖（正数；非法值回退默认并告警）。
- **FR-2.3**: 过渡期到期后，旧密钥链立即不再通过鉴权，即使仍有服务器未收敛；当前密钥始终有效。
- **FR-2.4**: 鉴权比对继续使用 `secrets.compare_digest`；X-Auth-Token / Bearer / Basic / `?token=` 候选收集顺序与 app API token 回退保持不变。

### FR-3 每服务器两阶段远端收敛
- **FR-3.1**: 对每台参与服务器，工作线程严格按顺序推进：① list 远端 → ② 新 URL（base 不变、token=新密钥）不存在则添加 → ③ 再次 list **确认新 URL 存在** → ④ 删除携带旧密钥的同 base（含每服务器路径与既有 legacy 路径）URL → ⑤ 再次 list **确认旧 URL 已不存在**。新 URL 未确认前绝不删除旧 URL。
- **FR-3.2**: 所有 plex.tv 写操作前必须先 list 判定，目标 URL 已存在/已不存在时直接记账，不重复 add/delete。
- **FR-3.3**: plex.tv 返回确定性拒绝（4xx、Unauthorized、BadRequest、无 Plex Pass 等）时：**保留旧注册**（不删除任何旧 URL），服务器状态记为 `failed_rejected` 并保存机器可读原因与不含密钥的人类说明；按退避继续重试（管理员更新 token 后可自动恢复），也可由状态接口的"立即重试"动作唤醒。
- **FR-3.4**: 网络异常/超时/5xx/响应不确定时：服务器状态记为 `uncertain`，**不根据猜测改写状态、不盲写**；下一轮动作先重新 list 远端列表，以远端事实决定 add/delete/记账。
- **FR-3.5**: 每次尝试记录 `attempts`、`last_attempt_at`、`next_attempt_at`；退避间隔持久化，跨重启生效；工作线程在有服务器待处理时持续运行，无待办时休眠并可被事件唤醒（新保存、手动操作、服务器增删改、立即重试）。

### FR-4 完成与撤销
- **FR-4.1**: 当所有参与服务器均到达终态收敛（新 URL 确认且旧 URL 移除，或"无 webhook 配置/已注销/已禁用/已删除"类确定性终态）时，轮换完成：删除持久化状态文件，旧密钥链随即失效。
- **FR-4.2**: 过渡期到期不阻止后台收敛：到期后旧密钥停止鉴权，但工作线程继续为未收敛服务器重试远端修复直至收敛或服务器被移除；状态中明确区分"旧密钥仍有效/已过期"。
- **FR-4.3**: 完成/到期路径均不得修改用户当前 `webhook_secret`；除非检测到"当前密钥 == 本轮旧密钥、settings 中当前密钥 != 本轮新密钥且也不等于任何旧链密钥"的异常分叉（如 settings 被备份还原为第三个值），此时停止轮换、记 warning、保持以当前 settings 为准，不自动改密钥。

### FR-5 手动操作与服务器生命周期仲裁
- **FR-5.1**: 轮换进行中手动 **register** 某台 Plex：注册动作使用**当前（新）密钥**构造 URL，只执行"确保新 URL 存在并确认"，**不删除旧 URL**（旧 URL 删除权归轮换工作线程）；动作结果回写该服务器的新一代状态并唤醒工作线程。无轮换时手动 register 行为与现状完全一致。
- **FR-5.2**: 轮换进行中手动 **unregister** 某台 Plex：删除该服务器 base 下携带新密钥与旧密钥链的全部 URL；该服务器轮次待办被确定性地置为"已手动注销"终态；若它是最后一个待办，本轮立即收敛完成。
- **FR-5.3**: **删除**服务器时：既有的尽力 plex.tv unregister 保留；随后将其轮次待办确定性置为"已删除"终态（远端清理失败也不阻塞），可能触发全局完成。
- **FR-5.4**: **禁用**服务器时：其轮次待办被确定性置为"已禁用"终态，不阻塞收敛；不主动删除 plex.tv 注册。**重新启用**时：若轮换仍在进行则重新纳入当前代次并唤醒收敛；若轮换已结束则确保以当前密钥复核/注册（走手动 register 同款幂等确保逻辑）。
- **FR-5.5**: 工作线程每轮以 settings 中的服务器清单重算成员，外部直接编辑 settings.json 导致的服务器消失/禁用也被确定性消解，不遗留永久待办。

### FR-6 状态接口与 UI
- **FR-6.1**: 新增 `GET /api/settings/webhook-rotation`（setup/auth 保护）：返回是否有活跃轮换、代次、开始/截止时间、旧密钥是否仍有效、是否已过期，以及每台 Plex 服务器的 `server_id`、`server_name`、所属 `generation`、状态机状态、`new_url_confirmed`、`old_url_removed`、`failure_reason`、`failure_message`、尝试次数与时间戳。
- **FR-6.2**: 响应体、任何日志消息中均不得出现密钥本身或 `?token=` 后的密钥片段（URL 日志只记 base）；GET 设置接口对 `webhook_secret` 的 `****` 掩码行为不变。
- **FR-6.3**: 新增 `POST /api/settings/webhook-rotation/retry`：对失败/不确定的服务器立即唤醒一轮收敛尝试（不重置代次、不改变状态机安全顺序）。
- **FR-6.4**: 设置 → Webhooks 面板在轮换活跃时展示轻量状态区：整体进度（X/N 已收敛、过渡期剩余/已过期）与每台 Plex 的代次、状态、失败原因；不展示密钥；轮换位息后该区域自动消失。
- **FR-6.5**: 现有 `/api/settings/plex_webhook/status` 行为保持兼容；可在其响应中附带该服务器所处的轮换代次/状态（不含密钥），供服务器编辑模态展示。

### FR-7 兼容性
- **FR-7.1**: 未配置 `webhook_secret` 时，注册 URL 继续回退嵌入 app API token；入站鉴权回退 `validate_token` 不变。
- **FR-7.2**: 非 Plex 服务器不参与轮换、不出现在轮换服务器状态中，其 webhook 收发不受影响。
- **FR-7.3**: URL 规范化（`_normalize_url`/`_base_url`/`_rebuild_path_preserving_host`、去尾斜杠、base 比对）保持既有语义；每服务器路径 `/api/webhooks/server/<id>` 继续不加冗余 `server_id=` 查询参数。
- **FR-7.4**: 无轮换时，`register/unregister/is_registered/list_webhooks` 的现有函数签名与行为保持可调用（含旧测试与脚本）。

## Non-Functional Requirements
- **NFR-1 可靠性**: 任何单一服务器、单次 plex.tv 调用异常都不得使设置保存失败或使其他服务器收敛停滞；状态写入先于/伴随每次远端副作用，崩溃在任意点后重启都能安全续跑（不重复 add、不漏删）。
- **NFR-2 安全性**: 轮换状态文件 0600；旧密钥接受窗口有硬性时限；状态接口与日志零密钥泄露；密钥比较恒定时间。
- **NFR-3 可观测性**: 关键迁移（开启、每服务器新 URL 确认、旧 URL 移除、完成、到期、拒绝、不确定）均有 loguru 结构化日志，日志仅含服务器名/ID 与 URL base。
- **NFR-4 性能**: 轮换为低频管理操作；plex.tv 调用按服务器串行/小并发并带退避，不进行忙轮询（最小间隔与上限退避），不影响作业调度与 webhook 入站热路径。
- **NFR-5 可测试性**: 核心编排不直接依赖 Flask；pwh 远端操作可 mock；提供测试用重置/立即执行钩子（类似现有 `reset_settings_manager`/MPG_TEST_RESET 模式）。

## Constraints
- **Technical**: Python 3.11、Flask 蓝图、loguru；持久化仅用现有 `atomic_json_save_with_backup`；后台线程为 daemon；不引入新第三方依赖；遵循 ruff 规范与项目注释/测试约定（见 `.claude/rules/`）。
- **Business**: 不破坏现存安装的升级路径；v10 之后全局单密钥模型不变。
- **Dependencies**: plexapi（MyPlexAccount.webhooks/addWebhook/deleteWebhook）行为现状；仅在 plex.tv 可达时推进远端状态。

## Assumptions
- plex.tv `addWebhook` 对完全相同 URL 幂等；删除一个不存在的 URL 等效成功（现有代码已按此处理）。
- 同一次轮换窗口内，`output.webhook_public_url` 的 host 不被管理员改动；若被改动（手动 register 允许改 host），以最新 stored URL 为新 URL 目标，旧 base 下的旧 URL 仍在 list 复核时按"携带旧密钥的 URL"规则清理。
- 多 worker/gunicorn 部署中同一 CONFIG_DIR 只有一个本应用进程持有轮换工作线程；状态文件以原子写 + 文件 mtime 复核避免线程/进程陈旧覆盖（不实现跨进程分布式锁）。
- 首次设置密钥场景不要求双窗口（没有旧密钥需要保护）。

## Acceptance Criteria

### AC-1: 轮换开启与立即生效（rule）
- **Given**: 已配置非空 webhook_secret `old` 且至少一台启用 Plex 具备 token + public_url；
- **When**: `POST /api/settings` 保存不同的非空密钥 `new`；
- **Then**: 设置立即持久化为 `new`；`$CONFIG_DIR/webhook_secret_rotation.json` 出现、generation 递增、含每台启用 Plex 的服务器状态；旧密钥在过渡期内对入站 webhook 仍鉴权通过，新密钥也通过。
- **Pass Condition**: 单测断言 settings 值、状态文件存在与结构、用 old/new 两个 token POST webhook 均非 401。
- **Evidence**: `tests/test_webhook_secret_rotation.py`（重写版）+ 手动 curl 级断言。

### AC-2: 先确认新 URL 再删旧 URL 的收敛顺序（rule）
- **Given**: 轮换已开启，plex.tv 远端列表 mock 可观测；
- **When**: 工作线程推进一台服务器；
- **Then**: 调用序列满足：list → add(新 token URL，仅在缺失时) → list 确认新存在 → delete(旧 token URL) → list 确认旧消失；新 URL 未确认时不存在任何 delete 调用；目标已存在/已消失时不重复 add/delete。
- **Pass Condition**: 用有序 mock 记录断言调用顺序与去重；重复收敛两轮不产生第二次 add/delete。
- **Evidence**: pwh 协调器单测 + manager 单测。

### AC-3: 全部服务器收敛后才撤销旧密钥（rule）
- **Given**: 两台启用 Plex，A 快速收敛、B 离线/失败；
- **When**: A 已收敛而 B 未收敛；
- **Then**: 旧密钥仍鉴权通过、状态文件仍存在；B 收敛后状态文件删除、旧密钥变为 401。
- **Pass Condition**: 两个时间点分别断言鉴权结果、状态接口 active 与服务器终态、状态文件存否。
- **Evidence**: manager 集成式单测（Flask app + mock pwh，轮询状态接口）。

### AC-4: Plex 确定性拒绝时保留旧注册（rule）
- **Given**: 某服务器 add/新 URL 调用返回 Unauthorized/无 Plex Pass/4xx 类确定性错误；
- **When**: 工作线程处理该服务器；
- **Then**: 不发生该服务器的任何 deleteWebhook；状态为 `failed_rejected`，failure_reason 可区分（如 `unauthorized`/`plex_pass_required`/`rejected`）；旧 URL 远端保留、旧密钥继续可鉴权（过渡期内）；退避后重试。
- **Pass Condition**: mock 抛出对应异常后断言 delete 调用为 0、状态字段正确、重试后再次尝试。
- **Evidence**: 单测。

### AC-5: 网络不确定时以远端列表复核为准（rule）
- **Given**: add 或 delete 调用超时/连接错误；
- **When**: 下一次工作线程尝试发生；
- **Then**: 服务器状态为 `uncertain`；下一动作首先 list；list 显示新 URL 已在 → 直接进入删旧阶段而不重新 add；list 显示新 URL 不在 → 重新 add；绝不基于不确定的响应删除旧 URL。
- **Pass Condition**: 两组场景（超时后实际已生效/实际未生效）各断言零冗余 add 与零越序 delete。
- **Evidence**: 单测。

### AC-6: 进程退出后从持久状态恢复（rule）
- **Given**: 轮换进行到某服务器"新 URL 已确认、旧 URL 未删"时进程退出（状态文件已落盘）；
- **When**: 重新 `create_app()`（同 CONFIG_DIR）；
- **Then**: 恢复的轮换沿用同一 generation；旧密钥仍按过渡期鉴权；工作线程从持久状态继续（先 list 复核，再删旧 URL），不重复 add 新 URL；收敛后正常完成。
- **Pass Condition**: 销毁并重建 app（不删 CONFIG_DIR）后断言 generation 不变、调用序列无重复 add、最终完成。
- **Evidence**: 单测。

### AC-7: 重复保存同一密钥不开启新一轮（rule）
- **Given**: 当前密钥为 `same`（含轮换进行中与无轮换两种情形）；
- **When**: 再次 POST `webhook_secret="same"`（或仅空白差异、`****`、空串）；
- **Then**: 不新增 generation、不创建/推进轮换、不产生 plex.tv list/add/delete 调用。
- **Pass Condition**: 保存前后状态文件与 pwh mock 调用次数均为零变化。
- **Evidence**: 单测。

### AC-8: 手动注册/注销与轮换仲裁（rule）
- **Given**: 轮换进行中；
- **When**: 分别对手动 register 与手动 unregister 端点发请求；
- **Then**: register 只确保新密钥 URL 存在并确认、不删旧 URL 并回写服务器状态；unregister 删除新旧两类 URL、该服务器转"已手动注销"终态并可触发全局完成。无轮换时两端点行为与现状一致（旧测试通过）。
- **Pass Condition**: mock pwh 调用参数与状态接口终态断言；无轮换回归断言。
- **Evidence**: 单测。

### AC-9: 删除/禁用服务器确定性解除待办（rule）
- **Given**: 轮换进行中某服务器为未收敛待办；
- **When**: 调用 DELETE 该服务器、或 PATCH enabled=false（再测重新启用）；
- **Then**: 该服务器立即转为"已删除"/"已禁用"终态并从待办计数剔除（删除时保留尽力 unregister）；若因此全部收敛则轮换完成；重新启用在轮换进行中时重新纳入并唤醒，轮换结束后确保按当前密钥幂等注册。
- **Pass Condition**: 操作后状态接口中该服务器终态确定、active 随最后待办消解而变 false；重启用路径断言。
- **Evidence**: 单测。

### AC-10: 过渡期到期撤销旧密钥但继续远端修复（rule）
- **Given**: 轮换中某服务器持续失败，`WEBHOOK_SECRET_ROTATION_GRACE_HOURS` 设为极小值或通过时间推进越过截止时间；
- **When**: 截止时间过后；
- **Then**: 旧密钥立即返回 401，当前密钥通过；状态接口 `old_secret_valid=false`、`grace_expired=true` 且仍能看到失败服务器与原因；工作线程继续重试，服务器恢复后收敛并删除状态文件。
- **Pass Condition**: 鉴权断言 + 到期后收敛断言。
- **Evidence**: 单测。

### AC-11: 状态接口零密钥泄露（rule）
- **Given**: 轮换处于任意阶段；
- **When**: 调用 `GET /api/settings/webhook-rotation`（及 plex_webhook/status）；
- **Then**: 响应 JSON 序列化文本中不包含旧/新密钥任何一个字面值，也不含带 token 的 URL；含每服务器 generation、状态、failure_reason；GET `/api/settings` 仍为 `****` 掩码。
- **Pass Condition**: 对响应原文做密钥子串扫描 = 0 命中；字段存在性断言。
- **Evidence**: 单测。

### AC-12: 兼容性保持（rule）
- **Given**: 四类既有场景；
- **When**: 分别验证 ① 从未配置 webhook_secret；② 仅 Emby/Jellyfin；③ 普通 webhook 鉴权（四种凭证位置 + API token）；④ 既有 URL 规范化/legacy 清理；
- **Then**: 行为与现状一致：无密钥时回退 API token、非 Plex 不参与轮换、鉴权候选链路不变、无轮换时 `register()` 旧签名/旧语义可用。
- **Pass Condition**: 现存相关测试套件（test_webhooks、test_webhook_router、test_csrf、test_routes 中相关项、既有 plex webhook 测试）全部通过。
- **Evidence**: pytest 既有套件运行结果。

### AC-13: 连续轮换合并为新一代并携带旧密钥链（rule）
- **Given**: 轮换 `old→mid` 进行中（部分服务器未收敛）；
- **When**: 保存第三个密钥 `new`；
- **Then**: generation 在原代次上 +1；过渡期内 `old`、`mid`、`new` 三者均可鉴权；服务器目标变为 `new` URL；远端携带 old/mid 的 URL 仅在新 URL 确认后删除；最终只接受 `new`。
- **Pass Condition**: 三代密钥鉴权断言 + 远端 URL 收敛到仅 new token。
- **Evidence**: 单测。

### AC-14: 异常状态分叉的安全失败（rule）
- **Given**: 轮换状态文件声称的 new_secret 与 settings 当前密钥不一致，且当前密钥不等于旧密钥链任一值（如手工还原了第三方 settings）；
- **When**: 入站请求携带状态文件中的旧密钥、且 app 启动恢复；
- **Then**: 该旧密钥不得通过轮换路径鉴权；恢复逻辑记录 warning 并将轮换标记为中止/等待人工处理，不自动改写 settings 密钥。
- **Pass Condition**: 401 断言 + 状态/日志断言。
- **Evidence**: 单测。

### AC-15: 状态面板与接口信息质量（rubric）
- **Dimension**: 轮换可观测性与操作可信度
- **Scale**: 1-5
- **Anchors**: 1 = 管理员无法判断轮换是否在进行/哪台卡住/为何失败；3 = 接口给出总体状态与每台结果但原因含糊或缺少代次/重试入口；5 = 设置页与 API 清晰展示代次、逐台两阶段状态、失败原因与过渡期截止，可一键立即重试，全程不泄露密钥，轮换位息后界面自动恢复常态。
- **Pass Threshold**: >= 4
- **Evidence**: 接口字段审查 + 设置页面板渲染检查（可由 e2e/结构断言覆盖）。

## Open Questions
- 无（四个关键决策已确认：24h 可环境变量覆盖；到期撤销旧密钥但继续后台修复；API + 设置页轻量面板；连续轮换合并携带旧密钥链）。
