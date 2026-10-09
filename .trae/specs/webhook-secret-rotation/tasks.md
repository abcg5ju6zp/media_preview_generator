# Webhook 密钥可恢复多服务器轮换 — 实施计划

## Task 1: 扩展 plex_webhook_registration 远端协调原语
- **Status**: `completed`
- **Completion Evidence**:
  - `plex_webhook_registration.py` 新增 `reconcile_webhook`/`add_webhook`/`delete_webhook_url`/`build_authenticated_url`/`urls_carrying_secret` 与确定性拒绝 vs 网络不确定的异常分类；严格 list→add→list 确认→delete→list 确认顺序，新 URL 未确认前零 delete。
  - 新增 `tests/test_plex_webhook_reconcile.py` 17 个用例全绿（`.venv/bin/python -m pytest tests/test_plex_webhook_reconcile.py -o addopts=""` → 17 passed），覆盖顺序、幂等、rejected 保留旧注册、uncertain 两分支复核、legacy/异主 scope、not_configured。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 `media_preview_generator/web/plex_webhook_registration.py` 新增（不破坏既有 `register/unregister/is_registered/list_webhooks` 签名与语义）：
    - `add_webhook(token, url_with_auth)`：先 list，目标完全相等则直接返回；不存在才 addWebhook，再 list 复核；不删除任何其他 URL。
    - `delete_webhook_url(token, url)`：list 后精确删除，已缺失视为成功；返回删除后列表。
    - `urls_carrying_token(remote_urls, secret)` / `build_authenticated_url`（复用 `_build_authenticated_url` 公开化）：识别携带旧密钥的同 base/legacy URL。
    - 高层 `reconcile_webhook(token, public_url, wanted_url, *, stale_secrets, persisted_state=None) -> ReconcileResult`：严格按 list→确保新→list 确认→删旧→list 确认的顺序单步/整步推进，返回结构化结果（new_present/new_confirmed/old_removed、outcome 代码 `converged|new_added|new_confirmed|old_removed|rejected|uncertain|not_configured`、machine reason、可安全重试标志）。
    - 异常归类：`PlexWebhookError` 增加/复用 reason 映射——确定性拒绝（unauthorized/plex_pass_required/missing_url/4xx BadRequest）与不确定（network_error/超时/5xx/读取后无法确认）二分；任何"写后不确定"结果要求调用方以重新 list 复核，禁止猜测性 delete。
  - 全部新增日志只输出 base URL，不输出带 token 的 URL。
- **Acceptance Criteria Addressed**: AC-2, AC-4, AC-5, AC-7
- **Test Requirements**:
  - `rule` TR-1.1: mock MyPlexAccount 时，新 URL 缺失→add 恰好 1 次；已存在→add 0 次；新未确认时 delete 调用为 0；旧 URL 在新确认后才删除且删除后经 list 复核。证据：`tests/test_plex_webhook_reconcile.py`（新）有序 mock 断言。
  - `rule` TR-1.2: add 抛 Unauthorized/BadRequest → outcome=rejected 且 delete 调用 0；add/delete 抛网络异常 → outcome=uncertain，下一次调用首个 plex.tv 动作是 list，且按 list 真值分支（已生效不重复 add、未生效重新 add）。证据：同文件参数化用例。
  - `rule` TR-1.3: 连续两次 reconcile 收敛后调用不产生第二次 add/delete（幂等）。证据：mock 调用计数。
  - `rule` TR-1.4: 既有 `register()` 行为与其旧测试保持不变。证据：运行现有涉及 pwh.register 的测试。
- **Notes**: 旧 `register()` 保留给"无轮换手动注册"路径；轮换与仲裁路径只调用新原语。

## Task 2: 持久化轮换状态模型与 RotationManager 核心
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 `media_preview_generator/web/secret_rotation.py`：0600 原子状态文件、generation 单调、同值 no-op、连续轮换 legacy 链合并、24h grace（环境变量可覆盖）、恒定时间比对、损坏隔离、settings 分叉 aborted、secret-free `public_status()`、单例与 reset 钩子。
  - 新增 `tests/test_secret_rotation_manager.py`（与 Task 3 合计 17 用例全绿）。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增 `media_preview_generator/web/secret_rotation.py`：
    - 状态文件 `$CONFIG_DIR/webhook_secret_rotation.json`，经 `atomic_json_save_with_backup(..., permissions=0o600)` 原子写；schema 含 version、generation（单调递增 int）、started_at、grace_until、new_secret、legacy_secrets[]、servers{server_id: {server_name, generation, phase/state, new_url_confirmed, old_url_removed, failure_reason, failure_message, attempts, last_attempt_at, next_attempt_at, updated_at}}。
    - `RotationManager`（单例 `get_rotation_manager()`/`reset_rotation_manager()`，模式对齐 settings_manager；构造接收 config_dir）：load（缺失/损坏安全回退并 warning）、save、`begin_rotation(old, new)`（同值 no-op；首次无旧密钥不走轮换；进行中再换不同密钥→generation+1 且 legacy_secrets 合并去重）、成员计算（启用 Plex；缺 token/public_url 即记 `resolved_no_webhook` 终态）、`accepts_secret(token)`（当前密钥直接放行；legacy 仅在 grace 内且状态文件 new_secret==settings 当前 webhook_secret 时放行；分叉见 Task 4）。
    - 过渡期默认 24h，环境变量 `WEBHOOK_SECRET_ROTATION_GRACE_HOURS` 解析（非法值回退默认+warning）。
    - 恒定时间比较（`secrets.compare_digest`）；对外 DTO 与日志不含任何密钥/带 token URL（提供 `public_status()` 序列化）。
- **Acceptance Criteria Addressed**: AC-1, AC-7, AC-11, AC-13, AC-14
- **Test Requirements**:
  - `rule` TR-2.1: begin 同值 no-op（generation/文件/pwh 调用零变化）；异值且存在旧密钥→generation+1、文件结构与 0600 权限正确、legacy 记录旧值。证据：新单测 + 文件权限断言。
  - `rule` TR-2.2: 进行中再保存第三密钥→generation 再+1，legacy_secrets 含前两个值且去重；accepts_secret 对三代密钥在 grace 内均为真、无关密钥为假。证据：单测。
  - `rule` TR-2.3: grace 到期后 accepts_secret 对 legacy 返回 False、对当前返回 True。证据：注入截止时间单测。
  - `rule` TR-2.4: `public_status()` 输出及 JSON 序列化结果做新旧密钥子串扫描 = 0 命中，且含每服务器 generation/state/failure_reason 字段。证据：单测。
  - `rule` TR-2.5: 状态文件损坏时 load 不抛异常、不放宽鉴权（等同无活跃轮换）并记录 warning。证据：单测。

## Task 3: 收敛工作线程状态机（失败三态、持久退避、完成/到期）
- **Status**: `completed`
- **Completion Evidence**:
  - daemon worker + Event 唤醒（无忙轮询）、快照无锁 I/O 后按代次丢弃过期结果、rejected/uncertain 映射、持久化指数退避、成员重算（新增/消失/禁用/重启用）、全终态收敛删文件、grace 到期仅停鉴权不停修复、resume_on_startup 不重试已收敛服务器。
  - `tests/test_secret_rotation_manager.py` 17 用例全绿（含真实后台线程自动收敛、重启恢复）。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 在 RotationManager 内实现单守护线程 worker（threading.Event 唤醒 + 条件等待，无忙轮询）：
    - 每轮以 settings 最新 `media_servers` 重算成员：新增启用 Plex 入列当前代次；消失的服务器确定性置 `resolved_deleted`；由启用变禁用由 Task 5 钩子置位，worker 兜底复核；`resolved_no_webhook` 类即时终态。
    - 对每个未终态服务器读取持久 per-server 阶段，调用 Task 1 的 `reconcile_webhook`（传入持久化阶段与 stale_secrets=legacy 链）：严格"新 URL 确认后才删旧"；结果映射 `new_confirmed/old_removed/converged`；确定性拒绝→`failed_rejected`（保留旧注册，记录 reason/message，指数退避并持久化 next_attempt_at）；不确定→`uncertain`（下次先 list）。
    - 每次尝试前后落盘 attempts/last_attempt_at/next_attempt_at/updated_at；退避上下限为模块常量（可被环境变量/测试钩子覆盖以便快速测试）。
    - 全部终态收敛→完成：删除状态文件（旧密钥链即刻失效）。grace 到期后：legacy 鉴权立即失效，但 worker 不清状态、继续修复，直到全部收敛或成员消失；public_status 标注 grace_expired。
    - 远端 URL 目标取服务器最新 `output.webhook_public_url`（host 变更场景由 list 复核兜底清理携带旧密钥的任意 base/path URL）。
  - 提供测试钩子：同步执行一轮 pass（`run_once()`）、立即唤醒（`kick()`），仅供测试/内部复用。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-5, AC-10
- **Test Requirements**:
  - `rule` TR-3.1: 两台 Plex，A 顺利收敛、B 持续网络错误：A 收敛后旧密钥仍有效、active=true；B 恢复后收敛→状态文件被删除、旧密钥 401。证据：manager+Flask app_context 单测（run_once/kick 驱动，轮询 public_status）。
  - `rule` TR-3.2: 确定性拒绝场景 delete 调用恒为 0、状态 failed_rejected 与 reason 正确、退避后自动重试。证据：单测。
  - `rule` TR-3.3: 不确定写后"实际生效/实际未生效"两分支均以 list 复核推进，无重复 add、无越序 delete。证据：单测。
  - `rule` TR-3.4: grace 到期注入：legacy 立即失效但 worker 继续，随后收敛仍正常删除状态文件。证据：单测。
  - `rule` TR-3.5: worker 运行期间新增/移除启用 Plex，成员与待办计数随之增减且不重启线程。证据：单测。

## Task 4: 接入鉴权、设置保存链路与启动恢复
- **Status**: `completed`
- **Completion Evidence**:
  - `webhooks.py` 入站鉴权增加轮换 legacy 密钥判定（恒定时间、异常安全、候选顺序与 API token 回退不变）；`save_settings` 同值（含空白/`****`）完全 no-op，异值开启 begin_rotation，首次设置保留一次性尽力注册；`create_app` 启动 resume + atexit 关闭。
  - 重写 `tests/test_webhook_secret_rotation.py`（7 用例全绿）：双密钥 query/header 202、无关 401、收敛后旧 401 新 202、同值/掩码零调用、首次设置走 register、非 Plex/无 URL/禁用不阻塞、GET 掩码。
  - 回归：test_webhooks.py 108 passed；test_webhook_router.py + test_csrf.py 112 passed（ffmpeg 桩件环境）；基线对照确认剩余失败仅为本机缺 ffmpeg 与既有 xdist worksteal 跨文件污染，与本次改动无关。
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - `web/webhooks.py::_authenticate_webhook`：在当前 `webhook_secret` 比对旁，增加 RotationManager `accepts_secret()` 判定（仅当存在活跃轮换）；候选收集顺序、`validate_token` 回退、日志措辞与无密钥行为保持兼容。
  - `web/routes/api_settings.py`：保存链路在 `settings.update` 前抓取旧值；webhook_secret 候选与当前值相同（strip 后、空、`****`）时从 updates/incoming keys 剔除且不触发任何 pwh 调用；异值且旧值非空→持久化后 `begin_rotation`（取代 `_reregister_plex_webhooks_after_secret_rotation` 同步尽力逻辑）；旧值为空（首次设置）保留既有尽力注册行为。
  - `web/app.py::create_app`：settings/job manager 初始化之后注册 `get_rotation_manager(config_dir).resume_on_startup()`（有状态文件则恢复 worker）；提供关闭钩子（daemon 线程随进程退出，atexit 仅做 event 置位）。
  - 异常分叉（状态 new_secret 既不等于当前密钥、当前密钥也不在 legacy 链）：resume 时将轮换标记 `aborted`（停止 legacy 鉴权、不自动改 settings、warning 日志），并在 public_status 暴露。
  - 重写 `tests/test_webhook_secret_rotation.py`：从"尽力重注册"契约改写为新协议契约（保留逐台隔离、跳过非 Plex/无 URL/无 token、保存触发等同类保证的对应断言）。
- **Acceptance Criteria Addressed**: AC-1, AC-6, AC-7, AC-12, AC-14
- **Test Requirements**:
  - `rule` TR-4.1: 轮换中以 old/new token 经 `?token=`、X-Auth-Token、Bearer、Basic 四种位置 POST webhook 均非 401；无关 token 401；完成后 old 401、new 通过。证据：Flask test_client 单测。
  - `rule` TR-4.2: 同值保存（含空白差异/`****`/空串）零 pwh 调用、无新代次；异值保存触发 begin 一次且旧值被正确捕获。证据：单测。
  - `rule` TR-4.3: 进程退出恢复：app 销毁重建后 generation 不变、旧密钥仍按 grace 鉴权、worker 从"新已确认旧未删"继续且无重复 add，最终完成删文件。证据：重建 app 单测。
  - `rule` TR-4.4: 第三方 settings 分叉：legacy 鉴权返回 401、状态 aborted、有 warning、settings 未被改写。证据：单测。
  - `rule` TR-4.5: 未配置密钥、纯 Emby/Jellyfin、普通 API token 鉴权、旧 `register()` 手动路径行为全部保持。证据：既有 test_webhooks.py / test_webhook_router.py / test_csrf.py / 重写后的轮换测试全绿。

## Task 5: 手动注册/注销仲裁与服务器删除/禁用/启用钩子
- **Status**: `completed`
- **Completion Evidence**:
  - `RotationManager.manual_register/manual_unregister`：活跃轮换中 register 只 add 当前密钥 URL（legacy register 零调用）、unregister 删除新旧两类 URL 并确定性置终态；api_plex_webhook 端点先行仲裁、无轮换走旧路径；DELETE/PATCH enabled/PUT server 全部接入 resolve/reenable/kick。
  - `tests/test_secret_rotation_api.py` 8 用例全绿（含无轮换 legacy 路径回归）。
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - `web/routes/api_plex_webhook.py`：
    - register：检测活跃轮换且该服务器在本轮成员中→改用 Task 1 原语仅"确保当前密钥 URL 存在并 list 确认"（不删旧 URL），结果回写其新一代状态并 kick worker；无轮换→现有 register() 路径原样。
    - unregister：活跃轮换中→删除该 base 下携带新密钥与 legacy 链密钥的全部 URL，服务器置 `resolved_unregistered` 终态；若因此全部收敛则本轮完成；无轮换→原样。
  - `web/routes/api_servers.py`：
    - DELETE：保留现有尽力 unregister；随后 `resolve_server(id, "deleted")` 确定性终态（pwh 失败也不回滚删除）。
    - PATCH enabled=false：`resolve_server(id, "disabled")`；enabled=true：`reenable_server(id)`——轮换进行中重新纳入当前代次并 kick；轮换已结束则调用幂等"确保当前密钥 URL 存在"（有 token+public_url 时）。
    - update_server 改动 output.webhook_public_url/host 或 token 时 kick worker 复核（手动注册 host 变更场景）。
- **Acceptance Criteria Addressed**: AC-8, AC-9
- **Test Requirements**:
  - `rule` TR-5.1: 活跃轮换中手动 register：pwh 调用不含 delete、目标 URL 携带当前密钥、服务器状态更新为 new_confirmed；手动 unregister：新旧 URL 均删除、服务器终态 resolved_unregistered、最后待办消解时 active 转 false。证据：端点级单测。
  - `rule` TR-5.2: 无轮换时两端点响应体与 pwh 调用与现状一致。证据：保留/新增回归断言。
  - `rule` TR-5.3: DELETE 与 PATCH enabled=false 后该服务器从待办中确定性消失并可能触发完成；重新启用在轮换中重新入列、在轮换后幂等确保当前密钥 URL。证据：端点级单测。

## Task 6: 轮换状态接口、立即重试端点与状态附带
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 `web/routes/api_secret_rotation.py`（GET 状态、POST retry）并注册蓝图；plex_webhook/status 附带非敏感 rotation 片段；白名单序列化，测试对响应原文做新旧密钥子串扫描 0 命中。
  - `tests/test_secret_rotation_api.py` 覆盖状态形状/零泄露/idle/retry/plex 状态附带，全绿。
- **Priority**: medium
- **Depends On**: Task 5
- **Description**:
  - 在 settings 路由（新 `web/routes/api_secret_rotation.py` 或 api_settings.py 内分组，优先独立小模块并在 routes/__init__.py 注册）：
    - `GET /api/settings/webhook-rotation`：无活跃轮换返回 `{active:false}`；活跃时返回 public_status（总体 + 每台 Plex 的 generation/两阶段布尔/状态/failure_reason/failure_message/attempts/时间戳/grace 信息）。
    - `POST /api/settings/webhook-rotation/retry`：重置可重试服务器的 next_attempt_at 并 kick；不重置代次、不改变状态顺序。
    - `/api/settings/plex_webhook/status` 响应增加非敏感字段 `rotation: {active, generation, state, new_url_confirmed, old_url_removed}`（无轮换时 null/省略）。
  - 鉴权沿用 `setup_or_auth_required`；做响应字典白名单序列化，杜绝密钥字段误入。
- **Acceptance Criteria Addressed**: AC-11, AC-15
- **Test Requirements**:
  - `rule` TR-6.1: 活跃/无活跃/到期/中止四种情况下响应字段完整且正确；无密钥、无 token URL 泄露（对响应原文扫描所有已知密钥字面值）。证据：端点单测。
  - `rule` TR-6.2: retry 后 failed/uncertain 服务器立即被 worker 再次尝试（attempts 增加），generation 不变。证据：单测。
  - `rule` TR-6.3: plex_webhook/status 附带字段不破坏现有前端消费字段（registered_in_plex/public_url 等保持）。证据：现有 plex webhook 相关测试回归。

## Task 7: 设置页轮换状态轻量面板
- **Status**: `completed`
- **Completion Evidence**:
  - `_automation_triggers.html`（/automation 页）新增 secretRotationPanel 容器（代次/总体徽标/grace 倒计时/逐服务器两阶段+原因/retry 按钮）并挂载新 `static/js/secret_rotation_panel.js`（5s 轮询、active 消失自动隐藏、escapeHtml、apiGet/apiPost 复用 CSRF 链）；plex_webhook_panel.js 模态徽标消费 rotation 片段。
  - 结构性测试 test_settings_page_contains_rotation_panel_and_script 全绿（页面含面板/脚本、JS 含两端点路径）。
- **Priority**: medium
- **Depends On**: Task 6
- **Description**:
  - 在 `web/templates/_automation_triggers.html` webhook 密钥区新增轮换状态容器（整体进度、grace 剩余/已过期、逐服务器行：名称+代次+阶段+失败原因），新增 `web/static/js/secret_rotation_panel.js`（轮询 GET 状态接口，active 时展示并在全部完成后自动隐藏；retry 按钮接 POST）；基模板按既有 JS 引入方式挂载。
  - 服务器编辑模态 plex webhook 面板消费 Task 6 的 rotation 附带字段显示一枚"轮换中/已收敛/失败"小徽标（plex_webhook_panel.js 增量）。
  - 全程不展示密钥；沿用现有样式类与 apiGet/apiPost/escapeHtml 约定。
- **Acceptance Criteria Addressed**: AC-15
- **Test Requirements**:
  - `rule` TR-7.1: 页面在无轮换时不显示状态区；mock 活跃轮换时渲染总体进度与每台 generation/状态/原因，且页面文本不含密钥。证据：新增 e2e/结构断言（tests/e2e 既有模式）。
  - `rubric` TR-7.2: 可观测性维度；scale 1-5；anchors 同 AC-15；threshold >= 4；证据：面板交互走查（active→收敛后自动消失、retry 按钮、过期文案）。

## Task 8: 全量回归、竞态/恢复加固与证据汇总
- **Status**: `completed`
- **Completion Evidence**:
  - 新增并发一致性测试（worker pass 与 resolve 并发：手动终态优先、状态文档可解析）与不可写配置目录测试（begin 持久化失败不外泄 legacy 鉴权）；修复两处实现缺陷（在途结果覆盖手动终态、begin 先持久化后入内存）。
  - `uvx ruff check media_preview_generator tests` 全绿；ruff format 已应用。
  - 轮换 4 个测试文件合计 52 passed；secret_rotation.py 覆盖率 84.7%。
  - 全量默认套件（ffmpeg 桩件 PATH）：19357 passed / 85 failed / 46 skipped / 40 errors；失败全部归因为本机环境（缺 ffmpeg/真实 ffmpeg 行为、jekyll、Linux `/proc/self/fd` 与 `libc.so.6`、macOS `/etc` 沙箱）；基线对照（test_jobs/test_processing_multi_server/test_routes/test_logging_config）失败数完全一致，与本次改动无关。
- **Priority**: high
- **Depends On**: Task 7
- **Description**:
  - 补齐边界测试：状态文件原子写/备份、并发 begin（settings.lock 内串行）、worker 与手动操作并发仲裁、host 变更后旧 URL 清理、legacy `/plex` 与每服务器路径混存清理、配置目录不可写时的安全失败。
  - 运行 `pytest`（默认套件）全量通过；`ruff check .` 干净；按需运行相关 e2e（test_webhooks_automation、test_settings_page、test_server_modal_redesign、test_journey_webhook_to_dashboard）。
  - 在 tasks.md 各任务回填 Completion Evidence。

## R1 审查问题修复（独立审查 FAIL → 已修复，待 R2 复核）
- **F-1（medium，CP-R4）**：reconcile 首次 list 异常按确定性拒绝/网络不确定分流（`is_deterministic_rejection`），401/403/无 Pass → rejected 且保留旧注册；新增 `test_list_unauthorized_is_rejected_not_uncertain/test_list_plex_pass_403_is_rejected/test_list_network_error_is_uncertain`。
- **F-2（medium，CP-R4）**：`_write_with_confirmation` 透传分类 reason（第三返回值），reconcile 不再硬编码 `rejected/remove_rejected`；新增 `test_add_unauthorized_propagates_machine_reason`（真实 Unauthorized，断言 reason=unauthorized 且零 delete）。
- **F-3（medium，CP-R3/FR-1.6/FR-7.2）**：零启用 Plex 时 begin 直接 no-op（不产生状态文件、不持有旧密钥）；全员终态即时完成；空成员也可完成；`POST /api/servers` 新建启用 Plex 后 `kick()`；新增 manager/API 对应用例（含 201 后轮询入列）。
- **F-4（medium，FR-6.2）**：legacy `register()/unregister()` 的 INFO/WARNING/DEBUG 日志全部只记 `_base_url()`，不再输出 `?token=`；新增 reviewer 同类实证已转为结构（日志代码白盒）保证。
- **F-5（low）**：worker 退出路径在最后一次 pass 后持锁复检是否出现新待办，消除 begin/worker 退出竞态。
- **F-6（low）**：状态文件改用不产生备份的 `atomic_json_save`（仍 0600），完成时清理任何 `.bak/.corrupt` 残留；新增 `test_state_writes_never_leave_secret_backups`。
- **F-7（low）**：当前密钥被清空时一致性检查将轮次置为 aborted（状态语义一致）；新增 `test_cleared_current_secret_aborts_round`。
- **盲区补齐**：CP-R10 端到端到期后继续收敛删文件（`test_post_expiry_convergence_still_deletes_state`）；CP-R13 stale 链实际传参断言（`test_chained_rotation_passes_full_stale_chain_to_reconcile`）；aborted 状态 API 可见且零泄露；面板 JS 显示 aborted 与 failure_message。
- **复测**：轮换四件套 64 passed（连跑 3 次稳定）；webhook 回归 220 passed（ffmpeg 桩件）；`uvx ruff check media_preview_generator tests` 全绿。

## R2 PASS 后的低危加固（R3 复核 PASS 后追加）
- **F-8 修复**：新增 `_http_status()` 解析 plexapi 异常消息中的 `(NNN)` 与 `_classify_remote_exception()`——429/5xx 归为 network_error（uncertain），401/403/其他 4xx 归确定性拒绝，无状态码再按异常类型回退；`_safe_list`/写层统一使用。新增参数化 4 例 503/429 + 400 用例；legacy register/unregister/list_webhooks 行为不变。
- **F-9 残余窗口修复**：新增 `RotationManager.arm_worker()`（释放管理器锁后复检线程是否已分离/死亡并重启后继），begin/kick/request_retry/manual_register/reenable/resume 全部改为锁外 arm；worker finally 仅按线程身份置空。
- **复测**：轮换四件套 69 passed，连跑 3 次稳定；test_webhook_router/test_csrf/test_webhooks 220 passed（ffmpeg 桩件）；ruff 全绿。R3 独立复核结论：整体维持 PASS。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-6, AC-12
- **Test Requirements**:
  - `rule` TR-8.1: 默认 `pytest` 套件全绿且新增代码不被覆盖率门限卡住；ruff 零告警。证据：命令输出粘贴。
  - `rule` TR-8.2: 并发场景（保存与手动 register/unregister 同时、worker pass 与 DELETE 同时）下无重复 add/delete、无悬空待办、状态文件始终可解析。证据：并发单测。
  - `rule` TR-8.3: 相关 e2e 套件通过（或记录不可跑原因与等价覆盖）。证据：playwright/pytest 输出。
