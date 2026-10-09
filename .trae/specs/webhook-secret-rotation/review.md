# Webhook 密钥可恢复多服务器轮换 — 独立审查

## 审查范围

- 用户目标：见 spec.md（轮换不中断投递、失败三态、可恢复、幂等、仲裁、可观测且不泄露、兼容）。
- 实现产物：
  - `media_preview_generator/web/secret_rotation.py`（新增，轮换状态机+持久化+工作线程）
  - `media_preview_generator/web/plex_webhook_registration.py`（新增 rotation-safe 协调原语）
  - `media_preview_generator/web/routes/api_secret_rotation.py`（新增，状态/重试端点）
  - `media_preview_generator/web/webhooks.py`（鉴权接入）
  - `media_preview_generator/web/routes/api_settings.py`（保存链路）
  - `media_preview_generator/web/routes/api_plex_webhook.py`（手动注册/注销仲裁、status 附带）
  - `media_preview_generator/web/routes/api_servers.py`（删除/禁用/启用/更新钩子）
  - `media_preview_generator/web/app.py`（启动恢复）
  - `media_preview_generator/web/templates/_automation_triggers.html`、`media_preview_generator/web/static/js/secret_rotation_panel.js`、`plex_webhook_panel.js`
  - 测试：tests/test_plex_webhook_reconcile.py、test_secret_rotation_manager.py、test_webhook_secret_rotation.py（重写）、test_secret_rotation_api.py
- 运行环境提示：本机无真实 ffmpeg/jekyll，部分既有测试因此失败（与本变更无关，需基线对照）；运行 load_config 相关测试可在 PATH 前置 `/tmp/mpg-fakebin`（ffmpeg/ffprobe 桩件）。默认套件：`.venv/bin/python -m pytest`（xdist）。

## Checkpoints

- [ ] **CP-R1（rule，覆盖 AC-1）**: 保存不同密钥后新值立即生效、`webhook_secret_rotation.json`（0600）含 generation 与每台启用 Plex 行；旧/新密钥在过渡期内均可通过 webhook 鉴权。证据：tests/test_webhook_secret_rotation.py + 状态文件权限断言（test_secret_rotation_manager.py）。
- [ ] **CP-R2（rule，覆盖 AC-2）**: 远端收敛严格 list→add（仅缺失时）→list 确认→delete→list 确认；新未确认零 delete；重复收敛无二次写。证据：tests/test_plex_webhook_reconcile.py 有序调用断言。
- [ ] **CP-R3（rule，覆盖 AC-3）**: 一台未收敛时旧密钥仍有效且状态文件存在；全部终态后文件删除、旧密钥 401。证据：test_secret_rotation_manager.py::test_all_servers_converge_revokes_old_secret。
- [ ] **CP-R4（rule，覆盖 AC-4）**: Unauthorized/BadRequest/Pass 类确定性拒绝时 delete 为 0、状态 failed_rejected+reason、退避后重试。证据：reconcile 与 manager 用例。
- [ ] **CP-R5（rule，覆盖 AC-5）**: add/delete 超时后下一轮先 list，按远端真值分支（已生效不重复 add，未生效重 add），绝不基于猜测删旧。证据：test_plex_webhook_reconcile.py uncertain 两用例。
- [ ] **CP-R6（rule，覆盖 AC-6）**: 进程重建后同 generation 恢复，已收敛服务器不重复尝试，从"新已确认旧未删"继续直至删文件。证据：test_restart_resumes_same_generation_and_converges。
- [ ] **CP-R7（rule，覆盖 AC-7）**: 同值（含空白/`****`/空）保存不产生新代次、无 plex.tv 调用；远端写幂等。证据：test_webhook_secret_rotation.py 同值/掩码用例 + reconcile 幂等用例。
- [ ] **CP-R8（rule，覆盖 AC-8）**: 活跃轮换中手动 register 只确保新密钥 URL（legacy register 零调用）、unregister 删除新旧两类 URL 并置终态可触发完成；无轮换时旧路径不变。证据：tests/test_secret_rotation_api.py。
- [ ] **CP-R9（rule，覆盖 AC-9）**: DELETE/PATCH enabled=false 确定性终态；重新启用轮换中重新入列、轮换后幂等确保当前 URL。证据：test_delete_server_deterministically_settles_pending、test_disable_and_reenable_server_membership。
- [ ] **CP-R10（rule，覆盖 AC-10）**: grace 到期后 legacy 立即 401、当前密钥通过、grace_expired 标记；worker 继续修复至收敛删文件。证据：test_grace_expiry_revokes_legacy_but_keeps_current + manager 持续修复语义。
- [ ] **CP-R11（rule，覆盖 AC-11）**: GET /api/settings/webhook-rotation 与 plex_webhook/status 任何状态下响应原文不含新旧密钥字面值与带 token URL；GET /api/settings 仍 `****`。证据：API/设置测试的子串扫描。
- [ ] **CP-R12（rule，覆盖 AC-12）**: 未配置密钥回退 API token/首次设置走旧尽力注册、非 Plex 不参与、鉴权候选四位置+API token 回退不变、无轮换时 register/unregister 旧签名可用、URL 规范化不变。证据：test_webhooks.py/test_webhook_router.py/test_csrf.py 回归与首次设置用例。
- [ ] **CP-R13（rule，覆盖 AC-13）**: 连续保存第三密钥时 generation+1、legacy 含前两代且去重、三代过渡期可鉴权、最终只接受最新。证据：test_chained_rotation_carries_legacy_secrets。
- [ ] **CP-R14（rule，覆盖 AC-14）**: settings 被还原为第三方密钥时轮换 aborted、旧密钥 401、不改 settings、状态可见。证据：test_divergent_settings_abort_round。
- [ ] **CP-U1（rubric，覆盖 AC-15）**: 接口与 /automation 面板按代次/两阶段/失败原因/grace 清晰展示并可一键重试、收敛后自动隐藏、全程零密钥；评分 1-5，阈值 ≥4。证据：接口字段审查 + 模板/JS 结构测试与走查。

## Review History

### Review R1

- **审查人**：独立代码审查（只读走查 + 实跑命令 + 最小复现脚本，未修改任何生产代码）
- **日期**：2026-10-09
- **总结论：FAIL** —— 14 条 rule 中 CP-R3、CP-R4 未通过（共 4 个 actionable finding：2 medium 与规则直接相关，另有 1 medium 日志泄露、3 low）；rubric CP-U1 = **4/5（达标，阈值 4）**。
- **命令证据**：
  - 轮换四件套：`pytest tests/test_plex_webhook_reconcile.py tests/test_secret_rotation_manager.py tests/test_secret_rotation_api.py tests/test_webhook_secret_rotation.py -o addopts="" -q` → **52 passed in 0.86s**。
  - 回归：`PATH="/tmp/mpg-fakebin:$PATH" pytest tests/test_webhooks.py tests/test_webhook_router.py tests/test_csrf.py -m "not gpu and not e2e and not integration" -q --no-cov` → **2 failed / 218 passed**；`git stash -u` 基线同命令同样 **2 failed / 218 passed**（失败用例与失败数完全一致，为既有 xdist worksteal 跨文件污染，与本变更无关；单用例隔离运行通过）。
  - lint：`uvx ruff check media_preview_generator tests` → **All checks passed!**

#### CP 逐项结论

- [x] **CP-R1（AC-1）**：`POST /api/settings` 异值保存后 `settings.update` 先落盘新值（api_settings.py:1001-1016），`begin_rotation` 在锁内先持久化后入内存（secret_rotation.py:466-475）；状态文件结构/代次/逐启用 Plex 行与 0600 权限有断言（test_secret_rotation_manager.py:142-168，实测 mode=0o600）；old/new 两密钥 query 与 X-Auth-Token 双 202、无关 401（test_webhook_secret_rotation.py:153-195）。
- [x] **CP-R2（AC-2）**：`reconcile_webhook` 严格 list→add（仅缺失）→list 确认→delete→list 确认（plex_webhook_registration.py:636-762），有序 mock 精确断言 5 调用序列、已存在零 add、二次收敛仅 1 list（test_plex_webhook_reconcile.py:70-113）；异常分支见 CP-R4/CP-R5。
- [ ] **CP-R3（AC-3）**：**失败** —— 两台服务器的场景实现与断言正确（test_secret_rotation_manager.py:287-303），但完成判定 `if rows and all(...)`（secret_rotation.py:1001）在参与集合为**空**时永不完成。实测：纯 Emby/零启用 Plex 部署轮换后，run_once 与进程重建（resume_on_startup）均保持 active、状态文件永驻、面板 0/0 一直显示（见 F-3 复现输出）。违反 FR-4.1（"所有参与服务器均到终态"对空集应真空成立）与 FR-7.2（非 Plex 部署不受影响）。
- [ ] **CP-R4（AC-4）**：**失败**，两处缺陷：
  - **F-1（medium）**：首次 list 的确定性拒绝被误判为 uncertain。plex.tv 无 Plex Pass/401/403 最常见的发生点正是 `account.webhooks()` list 调用，但 reconcile 把 `_safe_list` 抛出的全部 `PlexWebhookError` 一律映射为 `RECONCILE_UNCERTAIN`（plex_webhook_registration.py:671-679），实测 `Unauthorized("(401)")` 与 `(403)` 均得到 `outcome=uncertain, reason=plex_pass_required`，服务器落 `uncertain` 而非 FR-3.3 要求的 `failed_rejected`，UI 显示"网络不确定"而非确定性拒绝。旧注册保留/退避重试等安全行为仍然正确。
  - **F-2（medium）**：add 阶段确定性拒绝的机器原因被抹平。`_write_with_confirmation` 已分类出 unauthorized/bad_request/plex_pass_required（:528-536，仅写日志），但上层把 add 的 WRITE_REJECTED 硬编码成 `reason="rejected"`（:684-693），实测 Unauthorized → `reason=rejected`。AC-4 明确要求 failure_reason 可区分 `unauthorized/plex_pass_required/rejected`；现有测试用假桩直接回传 reason（test_secret_rotation_manager.py:100-105）绕过了真实原语，故 52 全绿不能证明该 AC。delete=0、保留旧 URL、退避后重试本身正确（test_plex_webhook_reconcile.py:116-126）。
- [x] **CP-R5（AC-5）**：add/delete 网络不确定后下一轮必以 list 起步，按远端真值分支（已生效不重复 add、未生效重 add），两组场景均有断言（test_plex_webhook_reconcile.py:128-159、198-211）；代码上不确定时返回的 `current` 为写前快照，`remaining` 复核不为空即 uncertain，无猜测性删除（:730-756）。
- [x] **CP-R6（AC-6）**：销毁重建 manager 后 generation 不变、旧密钥按 grace 有效、已收敛服务器不再尝试（只调用 B）、最终收敛删文件（test_secret_rotation_manager.py:402-424）；reconcile 本身无状态、每次 list 驱动，跨重启不会重复 add。
- [x] **CP-R7（AC-7）**：同值/空白/`****`/空串在保存链路即被剔除（api_settings.py:650-653、977-987），manager 层同值 no-op 与重复 begin 同 new 不升代次均有断言（test_secret_rotation_manager.py:181-199；test_webhook_secret_rotation.py:198-225，pwh 调用 0 次）；远端写幂等见 CP-R2。
- [x] **CP-R8（AC-8）**：活跃轮换中手动 register 走 `add_webhook`（只确保当前密钥 URL），legacy `register()` 调用 0、目标 URL token==NEW（test_secret_rotation_api.py:128-149）；unregister 实测删除 OLD+NEW 两类 URL、legacy unregister 0 次、置终态并触发全局完成（:152-176）；无轮换旧路径回归（:179-194）。
- [x] **CP-R9（AC-9）**：DELETE 先存后确定性置 resolved_deleted（api_servers.py:985-997）、PATCH enabled=false 置 resolved_disabled、重启用在活跃轮次中重新入代次且不走 legacy register（:1469-1472；test_secret_rotation_api.py:202-248）；带外禁用/删除/新增由 `_refresh_membership_locked` 兜底（test_secret_rotation_manager.py:356-386）。
- [x] **CP-R10（AC-10）**：grace 到期后 legacy 立即 401、当前密钥通过、`grace_expired/old_secret_valid` 标记正确（secret_rotation.py:530、396-400；test_secret_rotation_manager.py:222-232）；到期只停鉴权不停 worker（worker 调度不读 grace，:982-995、1023-1043）。**测试盲区**：无"到期后服务器恢复→继续收敛→删文件"的直接断言（同一收敛机制在他处覆盖，风险低）。
- [x] **CP-R11（AC-11，响应范围）**：GET `/api/settings/webhook-rotation` 与 `plex_webhook/status` 响应原文对 OLD/NEW 子串扫描 0 命中且字段完整（test_secret_rotation_api.py:256-287、331-350；public_status 白名单字段 secret_rotation.py:770-815）；GET /api/settings 仍 `****`（test_webhook_secret_rotation.py:268-275）。**另见 F-4（FR-6.2 日志范围）**。
- [x] **CP-R12（AC-12）**：首次设置走一次性尽力 `register()`（test_webhook_secret_rotation.py:228-242、283-306）；非 Plex/无 URL/禁用不参与（:244-265）；鉴权候选顺序 X-Auth-Token→Bearer→Basic→query 与 API token 回退保持（webhooks.py:171-247）；回归 218 passed 与基线一致；reconcile 限定同 host+同 base/legacy path，实测 other-host URL 不被触碰（test_plex_webhook_reconcile.py:170-179）。
- [x] **CP-R13（AC-13）**：连续轮换 generation+1、legacy=[old,mid] 去重、三代 grace 内可鉴权（test_secret_rotation_manager.py:202-219）；stale_secrets 整链下发（secret_rotation.py:949），实测真实 reconcile 对远端 [old,mid,legacy-/plex-old,new] 一轮后仅余 new token。备注：链式续轮沿用首轮 grace_until（不延长窗口，:431-432），属更安全的设计选择，spec 未要求重置。**测试盲区**：manager 层未断言下发的 stale_secrets 内容与远端终态（本人已用真实原语实测补齐证据）。
- [x] **CP-R14（AC-14）**：settings 还原为第三方密钥时，入站 legacy 立即被拒（accepts_secret 要求 new_secret==当前值，:528-529），worker/resume 一致性检查置 aborted、warning、不改 settings（:490-509、1045-1058；test_secret_rotation_manager.py:266-279）。
- **CP-U1（AC-15）：4/5（达标）**。API 字段完整（代次/两阶段布尔/状态/reason/message/attempts/全部时间戳/grace/abort）；面板有代次徽标、整体状态、X/N 进度、grace 倒计时、逐服务器两阶段文本、failure_reason、一键 retry、5s 轮询、收敛自动隐藏，textContent/escapeHtml 无 XSS（secret_rotation_panel.js:56-129、模板 _automation_triggers.html:419-432）；服务器模态有轮换徽标（plex_webhook_panel.js:124-133）。扣分点：① aborted 时 API 仍返回详情但面板因 `!data.active` 直接隐藏（secret_rotation_panel.js:60-63），人工中止态在 UI 不可见；② 逐服务器只渲染 failure_reason 机器码，未展示更友好的 failure_message。

#### Findings

- **F-1（medium，CP-R4/FR-3.3/AC-4）确定性拒绝在首次 list 阶段被误分类为 uncertain**
  - 位置：media_preview_generator/web/plex_webhook_registration.py:671-679（`_safe_list` 已在 :454-464 把 Unauthorized/无 Pass 归为 plex_pass_required，但 reconcile 不区分 reason 一律回 RECONCILE_UNCERTAIN）。
  - 复现：patch `_account` 返回 FakeAccount，`webhooks()` 抛 `plexapi.exceptions.Unauthorized("(401) ...")`，调用 reconcile_webhook → 实测 `outcome='uncertain', reason='plex_pass_required'`，状态机落 S_UNCERTAIN（secret_rotation.py:900-909）。
  - 期望：list 失败时若 `is_deterministic_rejection(exc)` 为真，返回 `RECONCILE_REJECTED`（携带 exc.reason/message），服务器落 `failed_rejected`；仅网络/5xx/超时走 uncertain。
- **F-2（medium，CP-R4/AC-4）add 拒绝的细分 reason 被硬编码抹平**
  - 位置：plex_webhook_registration.py:684-693（恒为 `reason="rejected"`）；分类结果在 :528-536 仅用于日志；delete 拒绝 :718-729 恒为 `remove_rejected`。
  - 复现：addWebhook 抛 Unauthorized → 实测 result.reason == "rejected"，状态接口 failure_reason 无法区分 unauthorized/plex_pass_required/bad_request。
  - 期望：让 `_write_with_confirmation` 把分类后的 reason（或 PlexWebhookError）透出，ReconcileResult.reason 透传真实机器码；并补一条经真实 reconcile（非假桩）断言 reason 粒度的用例。
- **F-3（medium，CP-R3/FR-4.1/FR-7.2）零参与 Plex 时轮换永不完成**
  - 位置：secret_rotation.py:1001（`if rows and all(...)`）、:297-306（空成员种子）、:1033-1037（worker 见空活动行即退出）；连带 POST `/servers` 新增 Plex 不 kick（api_servers.py:834-885），worker 已退出场景下 FR-1.6"自动加入当前代次"失效，要等重启/手动操作。
  - 复现：FakeSettings 仅 1 台 Emby → begin_rotation 后 run_once、再重建 manager 调 resume_on_startup，实测两次后均 `active=True`、状态文件存在、`server_count=0`、old_secret_valid=True。
  - 期望：空参与集合视为真空完成（begin 时若无任何启用 Plex 行即不开启轮或立即清除状态；`_maybe_complete_locked` 改为 `all(terminal)` 对空集成立）；并在 create_server 后 `kick()`。
- **F-4（medium，FR-6.2/NFR-2）保留路径仍把带 `?token=` 的完整 URL 写入 INFO/WARNING 日志**
  - 位置：plex_webhook_registration.py:335（INFO "Removed stale Plex webhook with old auth: {stale_url}"）、:337-343（WARNING 同 URL）、:405（INFO "Removed Plex webhook: {match}"）；:346 DEBUG 亦含完整 URL。
  - 这些函数并非死代码：首次设置密钥的一次性注册（api_settings.py:706，FR-1.3 明确保留）与无轮换手动 register/unregister（FR-5.1/FR-7.4）仍调用；实测两次调用日志均明文输出 `?token=SECRETMARKER-...`；无密钥旧安装还会泄露嵌入的 app API token。
  - 期望：上述日志一律只记 `_base_url(url)`（新增原语已全部如此），或在日志前脱敏 query。
- **F-5（low，NFR-1/FR-3.5）worker 退出间隙 begin 新一代可导致新轮次无 worker、停滞至 kick/重启**
  - 位置：secret_rotation.py:1011-1021（`_ensure_worker` 仅看线程存活）与 :1033-1037（delay=None 分支退出前最后一次 run_once）。
  - 复现（确定性插桩）：gen1 全部 terminal（如全部 no_webhook）、worker 进入退出 pass 的瞬间 begin gen2（服务器补齐 token/URL），begin 见到旧线程仍 alive 不启新线程；旧线程退出 pass 顺手处理 gen2 目标后退出。实测最终 `active=True, gen=2, failed_rejected, 无存活线程`，`kick()` 可恢复。
  - 期望：worker 退出 pass 前后比对 generation，或 begin/kick 在释放锁后复检线程已死则重启线程。
- **F-6（low，NFR-2）轮换完成后状态备份文件仍残留明文旧/新密钥**
  - 位置：secret_rotation.py:250-253 使用 `atomic_json_save_with_backup`，完成时 :255-268 只 unlink 主文件；实测 `.20261009-*.bak` 权限 0o600 但含旧密钥字面值，默认保留 10 份且不按龄清理。
  - 期望：完成时一并清理该状态文件的时间戳备份，或对该文件改用不产生备份的原子写。
- **F-7（low，FR-4.3 边界）settings 当前密钥被清空时轮换不 abort**
  - 位置：secret_rotation.py:502-503（current 为空直接 return）。鉴权不会放宽（:528-529 已拒绝 legacy），但 public_status 仍显示 active，语义不一致。
  - 期望：空 current 同样走 abort 或显式标注，保持状态可见性一致。

#### 测试盲区汇总（断言不能完全证明对应 AC 之处）

1. CP-R4：rejected 用例的 Unauthorized 只挂在 add 上（test_plex_webhook_reconcile.py:116-126）且只断言 outcome/delete=0，未断言 reason 粒度；manager/API 层全部用直接回传 reason 的假桩（test_secret_rotation_manager.py:100-105、test_webhook_secret_rotation.py:117-122、test_secret_rotation_api.py:95），真实 reconcile 的 F-1/F-2 缺陷因此漏网。无 list 阶段 401/403/无 Pass 用例。
2. CP-R3：无零 Plex/纯非 Plex 开启轮换的用例（F-3 漏网）。
3. CP-R10：缺 grace 到期后远端恢复→继续收敛→删文件的端到端断言。
4. CP-R13：未断言 manager 下发的 stale_secrets 含两代旧密钥，也未断言远端终态只剩 new token（本次审查以真实原语实测代替：old/mid/legacy-old 全删、仅余 new）。
5. CP-R9/FR-1.6：POST 新建服务器加入轮次无端点级用例（仅 manager 层带外加入被覆盖）。
6. F-5 竞态、F-6 备份清理、F-7 空密钥均无对应用例。

### Review R2

- **审查人**：第二轮独立代码审查（只读走查 + 实跑命令 + 4 组独立实证脚本，未修改任何生产代码）
- **日期**：2026-10-09
- **总结论：PASS** —— F-1..F-7 全部 closed；CP-R1..R14 共 14 条 rule 全部通过，CP-U1 提升至 **5/5**。新增 2 个**低危非阻断** finding（F-8：plexapi 把 5xx/429 包成 BadRequest 导致的 reason 标签边界；F-9：F-5 修复后残余的微秒级线程退出窗口），另附 1 条观察项。
- **命令证据**：
  - `.venv/bin/python -m pytest tests/test_plex_webhook_reconcile.py tests/test_secret_rotation_manager.py tests/test_secret_rotation_api.py tests/test_webhook_secret_rotation.py -o addopts="" -q` → **64 passed in 4.08s**。
  - `PATH="/tmp/mpg-fakebin:$PATH" .venv/bin/python -m pytest tests/test_webhooks.py tests/test_webhook_router.py tests/test_csrf.py -m "not gpu and not e2e and not integration" -q --no-cov` → **220 passed in 4.37s**（R1 时 2 failed 为 xdist worksteal 跨文件污染，本次同命令零失败）。
  - `uvx ruff check media_preview_generator tests` → **All checks passed!**
  - 独立实证（heredoc，非测试假桩）：①真实原语三代链收敛（old/mid/legacy-/plex-old 全删、异主机 URL 保留、仅余 new、二次收敛仅 1 list）；②401/400/403/429/503/网络错误在 list/add/delete 的实际分类矩阵；③register/unregister/reconcile-reject 全路径日志捕获，密钥标记 0 命中；④屏障复现 F-5 竞态，gen2 worker 存活并收敛。

#### CP 逐项复核（重新裁决）

- [x] **CP-R1（AC-1）**：维持通过。异值保存落盘→begin 锁内先持久化（secret_rotation.py:497-503）；0600 权限断言仍在（test_secret_rotation_manager.py:165-168，本轮实测 mode=0o600）；old/new 四位置鉴权用例 64 套件内全绿。
- [x] **CP-R2（AC-2）**：维持通过。真实原语实证调用序列为 `list,add,list,del,list,del,list,del,list`（同主机 3 条旧 URL），二次 reconcile 恰 1 次 list，幂等成立。
- [x] **CP-R3（AC-3）**：**改判通过**。F-3 三处修复独立核实：①begin 在采用/落盘状态**之前**对 `prior is None 且 servers 为空`返回 None（secret_rotation.py:488-493），不产生状态文件、不持有旧密钥（test_begin_without_any_plex_is_a_noop 实测无文件、old 不被接受）；②全员终态即时完成（:504-507，test_all_plex_without_webhook_completes_immediately 断言状态文件消失且返回 `{"active":false,"servers":[]}`）；③`_maybe_complete_locked` 改为对空集真空成立（:1039）。空集不会误删活跃状态：`_refresh_membership_locked` 只改 state 不删键，begin 携带旧轮仅搬运 disabled/deleted/unregistered 终态行。
- [x] **CP-R4（AC-4）**：**改判通过**。F-1：首次 list 异常按 `is_deterministic_rejection` 分流（plex_webhook_registration.py:676-694），真 plexapi `Unauthorized("(401)")` 实测 rejected、ConnectionError 实测 uncertain；3 个新用例均经真实异常类型。F-2：`_write_with_confirmation` 第三返回值 reason 透传（:539/:547），add/delete 两路径实测 401→`unauthorized`、400→`bad_request`、add 401 零 delete（test_add_unauthorized_propagates_machine_reason + R2 补充的 delete 路径实证）。delete=0、旧 URL 保留、退避重试语义不变。标签边界残留见 F-8（低危，不影响本 AC 的安全语义）。
- [x] **CP-R5（AC-5）**：维持通过。两组不确定两用例仍断言 list 起步与真值分支；实证 add/delete 超时后远端真值驱动，无猜测删除。5xx 被贴错标签时下一轮仍以 list 开始且不做任何写（F-8），安全语义未破坏。
- [x] **CP-R6（AC-6）**：维持通过（test_restart_resumes_same_generation_and_converges：gen 不变、A 不重试、只调 B、收敛删文件）。
- [x] **CP-R7（AC-7）**：维持通过（同值/空白/`****` 三处剔除 + 管理器 no-op，pwh 零调用断言）。
- [x] **CP-R8（AC-8）**：维持通过（手动 register 仅 add NEW、legacy register 0 次；unregister 删除 {OLD,NEW}、legacy 0 次、终态触发完成）。
- [x] **CP-R9（AC-9）**：维持通过并补齐端点盲区。POST `/api/servers` 后 kick（api_servers.py:876-884），test_new_plex_server_joins_active_round_via_create_endpoint 经**真实后台 worker** 轮询断言 plex-c 以当前 generation 入列；disable/reenable/delete 仲裁用例不变。
- [x] **CP-R10（AC-10）**：维持通过，R1 盲区已补：test_post_expiry_convergence_still_deletes_state 直接断言过期后 legacy 401、远端恢复后继续收敛并删除状态文件。
- [x] **CP-R11（AC-11）**：维持通过。aborted 场景补做原文三密钥子串扫描（test_aborted_round_is_visible_in_status_without_leaking_secrets，OLD/NEW/third 均 0 命中）；R2 另对日志做全仓+运行时双扫（见 F-4 裁决）。
- [x] **CP-R12（AC-12）**：维持通过。首次设置走一次性 register（test_first_secret_runs_best_effort_registration）；零 Plex 时 begin 返回 None 被 api_settings.py:819 安全忽略、无异常、无状态文件（无 Plex 场景实证）；回归 220 passed 全绿。
- [x] **CP-R13（AC-13）**：维持通过，R1 盲区已补：test_chained_rotation_passes_full_stale_chain_to_reconcile 断言下发 `stale==["old","mid"]`；R2 真实原语实证远端 [BASE?old, BASE?mid, /plex?old, 异主机?mid] 一轮后同主机仅余 new token、异主机不动。
- [x] **CP-R14（AC-14）**：维持通过；F-7 修复后清空密钥同样 aborted（test_cleared_current_secret_aborts_round：active=False/aborted=True、old/new 均 401）。
- **CP-U1（AC-15）：5/5（达标，阈值 4；R1 为 4/5）**。R1 两个扣分点均已关闭：① 面板对 aborted 不再隐藏并展示 abort_reason（secret_rotation_panel.js:60-89，test_aborted_round_is_visible_in_status_without_leaking_secrets 配套）；② 逐服务器在机器码后追加 failure_message 文本（:123-132，经 textContent 注入无 XSS）。其余能力复核无退化：代次徽标/整体状态/X-of-N/grace 倒计时/两阶段短语/一键 retry/5s 轮询/收敛自动隐藏齐全；retry 在 aborted/idle 时为安全 no-op（request_retry 返回 0，已核路由）。

#### F-1..F-7 逐条裁决

- **F-1 → closed**。plex_webhook_registration.py:676-694 首次 list 确定性/不确定分流；list 401/403（真 plexapi 异常与 RuntimeError `(403)` 子串两路）与网络错误均有用例，R2 实测一致。
- **F-2 → closed**。:499-568 第三返回值分类 reason，reconcile add(:699-709)/delete(:736-747) 不再硬编码；真 Unauthorized 用例断言 reason 粒度且零 delete；R2 另实证 delete 401→unauthorized、delete 400→bad_request。
- **F-3 → closed**。零 Plex no-op（不落盘、不持旧密钥）、全员终态即时完成、空集 all() 真空成立、create_server kick 四处均核实并有对应测试；begin 返回 None 在 api_settings.py:810-828 的两条分支（轮换/首次设置）行为正确。
- **F-4 → closed**。pwh 全部 INFO/WARNING/DEBUG 仅输出 `_base_url()`（:335/:341/:346/:359/:397/:406/:532-567）；R2 用 loguru sink 实测 register（含 stale 删除）、unregister（含删除失败异常文本）、reconcile 拒绝路径，密钥标记与旧密钥均 0 命中。另确认纵深防御：loguru 全局 patcher `redact_secrets` 对 `token=` 脱敏（logging_config.py:91-98），stdib→loguru 桥接（:216-228）也覆盖 plexapi 自身 `Setting webhooks` DEBUG 行。
- **F-5 → closed**。worker 末次 pass 后持锁复检（secret_rotation.py:1071-1086）。R2 以事件屏障把 begin(gen2) 精确插入退出 pass：实测旧线程存活、gen2 新服务器在 ~2s 内被同一 worker 尝试并最终收敛删文件。无忙循环（事件驱动 + delay 下限 0.5s + 退避 ≥1s）。残余微窗口见 F-9。
- **F-6 → closed**。状态写改用无备份的 `atomic_json_save(permissions=0o600)`（:246-258；mkstemp 临时文件本身 0600），完成时 `_remove_state_backups_locked` 清扫 `.bak/.*.bak/.corrupt`（:260-287）；test_state_writes_never_leave_secret_backups 断言目录内仅主文件。
- **F-7 → closed**。:534-538 当前密钥为空即 `_abort_locked`；run_once/worker/resume 三入口都会调用一致性检查；对应用例断言双密钥 401、状态 aborted。

#### 新 Findings（R2）

- **F-8（low，CP-R4/CP-R5 标签边界，非阻断）plexapi 5xx/429 被包成 BadRequest 后贴成"确定性拒绝"标签**
  - 位置：media_preview_generator/web/plex_webhook_registration.py:121-122（`_looks_like_plex_pass_required` 对任意 plexapi `BadRequest` 实例返回 True，被 `_safe_list` :460 使用）与 :489-490（`_classify_write_exception` 先判 isinstance(BadRequest)→`bad_request`，先于 `(NNN)` 子串判断）。
  - 证据：plexapi 4.17.1 `MyPlexAccount.query` 对除 401/404/422 外的**所有**非 2xx（含 403/429/5xx）统一 `raise BadRequest(f"({code}) …")`（.venv/.../plexapi/myplex.py:242-255）。R2 实测：list 503→`rejected/plex_pass_required`、list 429→`rejected/plex_pass_required`、list 400→`rejected/plex_pass_required`；add 503→`rejected/bad_request`；add 403→`rejected/bad_request`（与 list 403→plex_pass_required 还不一致）；ConnectionError→`uncertain/network_error` 正确。
  - 影响：FR-3.4 语义上 5xx/429 应 uncertain；当前会落 `failed_rejected` 且 reason 误导（plex.tv 宕机时提示"无 Plex Pass"）。**安全行为不变**：list 被拒时零写、add 被拒保留旧 URL、下轮仍以 list 复核、failed_rejected 属 ACTIVE_STATES 持续指数退避重试，故障恢复后自动收敛——故仅标签/可观测性偏差，定 low。
  - 建议：两个分类器先从异常文本提取前导 `(NNN)` 状态码再分类：5xx/429→`network_error`（uncertain），403→`plex_pass_required`，其余 4xx→`bad_request`；`_looks_like_plex_pass_required` 中 isinstance(BadRequest) 短路前同样先查状态码。补 503/429 用例。
- **F-9（low，NFR-1，非阻断）F-5 修复后残余的线程退出微窗口**
  - 位置：secret_rotation.py:1074-1085（持锁复检结束→释放锁→`return` 之间无线程存活保证）；begin 的 `_ensure_worker()` 仍在锁内只看 is_alive()（:1049-1059、:509-510）。
  - 问题：若 begin_rotation 的整个锁内临界区（含 atomic_json_save 的 fsync）恰好全部发生在旧 worker 释放复检锁之后、真正 return 之前的调度间隙，begin 见到 is_alive()=True 不启新线程，旧线程随后退出，gen2 无 worker，停滞至下一次 kick（手动注册/注销、服务器增改、立即重试）或重启。R1 原窗口（整次 pass、含网络 I/O，秒级）已由 :1074-1086 关闭并有屏障实证；此残余窗口为亚毫秒~毫秒级调度竞态，需病态调度配合，后果仅延迟收敛、无正确性/安全性影响。
  - 建议：begin/kick 在释放管理器锁之后再复检一次 `_thread.is_alive()`，已死则重启线程（或 worker 退出时比较退出时记录的 generation 与当前值）。

#### 观察项（不开 finding）

- 旧版 `unregister()` 把 plexapi 异常文本包进 PlexWebhookError，路由 jsonify `error: str(exc)`（api_plex_webhook.py:428-438）；plexapi 客户端侧 `BadRequest("Webhook does not exist: {url}")`（plexapi/myplex.py:759）理论上可把带 token 的完整 URL 带进错误响应。实际不可达：删除目标来自同一 account 对象刚取回的 webhooks() 缓存，URL 为我方注册时的归一化值，raw 与 normalized 恒等；且端点仅 setup/auth 管理员可达、属本变更之前的既有路径。后续可顺手在 PlexWebhookError 文本经 API 返回前对 URL 做 query 脱敏。

#### 最终裁决

- 14 条 rule（CP-R1..R14）：**14/14 通过**；rubric CP-U1：**5/5（≥4 达标）**。
- **总体结论：PASS**。F-8/F-9 均为 low 级标签精度/理论竞态，安全语义（旧注册保留、list 复核、退避重试、零密钥泄露）不受影响，可在后续小版本处理，不阻断本次交付。

### Review R3

- **审查人**：第三轮独立复核（只读；仅复核 R2 后针对 F-8/F-9 的追加修复，未修改生产代码，独立 heredoc 实证）
- **日期**：2026-10-09
- **总结论：维持 PASS（不降级）** —— F-8 **closed**；F-9 **closed（残留一条与 R2 等价的非阻断理论窗口，实证仅能被确定性锁序注入触发，60 次自然快节奏循环 0 命中）**。
- **命令证据**：
  - `.venv/bin/python -m pytest tests/test_plex_webhook_reconcile.py tests/test_secret_rotation_manager.py tests/test_secret_rotation_api.py tests/test_webhook_secret_rotation.py -o addopts="" -q` → **69 passed in 1.17s**（与期望一致）。
  - `PATH="/tmp/mpg-fakebin:$PATH" .venv/bin/python -m pytest tests/test_webhook_router.py -m "not gpu and not e2e and not integration" -q --no-cov` → **42 passed in 2.27s**（单文件全绿）。
  - `uvx ruff check media_preview_generator tests` → **All checks passed!**

#### F-8 → closed

修复点核实：[plex_webhook_registration.py](file:///Users/ding/Documents/swe/0910091/project-04/media_preview_generator/web/plex_webhook_registration.py#L25-L33) 新增 `_STATUS_RE/_http_status()`（正则提取首个 `(NNN)`）；[_classify_remote_exception](file:///Users/ding/Documents/swe/0910091/project-04/media_preview_generator/web/plex_webhook_registration.py#L467-L519) 状态码优先（401→unauthorized、403→plex_pass_required、429/5xx→network_error、其余 4xx→bad_request），无码再按 plexapi 类型/文本回退；[_safe_list](file:///Users/ding/Documents/swe/0910091/project-04/media_preview_generator/web/plex_webhook_registration.py#L522-L527) 与 [_classify_write_exception](file:///Users/ding/Documents/swe/0910091/project-04/media_preview_generator/web/plex_webhook_registration.py#L530-L532) 统一走它；PlexWebhookError 原样透传。

- **① 429/5xx 三路径 uncertain 且不发生猜测性 delete**：用**真实 plexapi 异常类型**（非测试中的 RuntimeError）实测矩阵——list `BadRequest("(503)/(429)")` → `uncertain/network_error`，调用仅 `list`；add 503/429 → `uncertain/network_error`，delete 调用 0 次；delete 路径（NEW 已确认、OLD 残留）503/429 → outcome=`uncertain`（WRITE_UNCERTAIN，snapshot 仍见旧 URL 故 reason=`old_url_still_present`；network_error 在写层 reason 上）。二轮实证严格 list 驱动：远端已删→仅 1 次 list 即 converged（不重复 del）；仍在→`list,del,list` 重试。
- **② 400/401/403 确定性正确**：list/add/delete 三路径实测 401→`unauthorized`、403→`plex_pass_required`、400→`bad_request`，均 rejected；list/add 拒绝时零 delete；额外验证 404→bad_request（保守确定性，可接受）。reconcile list 分流（:714-730）与写层 reason 透传（:567-583）对状态机的映射（secret_rotation.py:935-945 → S_UNCERTAIN/S_FAILED_REJECTED）均正确，退避重试语义不变。
- **③ legacy 对外行为未变**：静态确认 `register()/unregister()/list_webhooks()` 不调用新分类器（新分类器仅 `_safe_list`/`_write_with_confirmation` 使用；legacy 仍走未改动的 `_looks_like_plex_pass_required`）。实测 legacy list/register 的 401 与 503 仍为 `plex_pass_required`、连接错误仍为 `unknown`、unregister 的 list 失败仍包 `unknown`——reason 兼容，无意外改变（legacy 503 贴 plex_pass 标签的旧怪癖保留，本就不在 F-8 范围）。
- **④ 无码网络异常回退正确**：`ConnectionError`、`socket.timeout`、`requests.exceptions.ReadTimeout`（均无 `(NNN)`）→ `uncertain/network_error`；无码 `Unauthorized`→unauthorized、无码 `BadRequest`→bad_request；`_http_status` 对无码消息返回 None。
- 新增测试 `test_transient_http_statuses_are_uncertain`（list/add × 503/429 共 4 例）与 `test_explicit_400_on_add_is_deterministic_rejection` 均通过，既有 22 用例无回归。小备注（不影响结论）：新用例以 RuntimeError 携带 `(NNN)` 注入，本人已用真 plexapi `BadRequest/Unauthorized` 补证，因解析先于类型判断，二者等价。

#### F-9 → closed（残留理论窗口，维持非阻断）

修复点核实：[_worker_loop 的 try/finally](file:///Users/ding/Documents/swe/0910091/project-04/media_preview_generator/web/secret_rotation.py#L1094-L1101) 在线程真正死亡前、持锁且 `self._thread is threading.current_thread()` 时置空，使"后到"（detach 之后）的 begin/kick 经 `_ensure_worker` 确定地看到无 worker 并启动后继。

- **① 不会错误清空后继线程引用**：身份守卫成立——后继一旦安装，旧线程 finally 中身份不等即不清空。60 次"收敛瞬间立即 begin 下一代"的快节奏循环中后继轮全部收敛，无引用被误清。
- **② 正常收敛后引用确实置空**：`test_background_worker_converges_without_manual_pass` 末尾轮询 `_thread is None` 通过；60/60 循环退出后均观测到 `_thread is None`（detach 先于线程死亡）。
- **③ 无忙循环/死锁**：空闲即退出、有活走 `_wake.wait(timeout=min(delay,5))` 事件驱动；屏障脚本中被卡住的 worker 放行后正常退出，kick 路径正常收敛。
- **残留（与 R2 评级一致的 low，不阻断）**：以确定性 GatedRLock 强制锁序 **R（worker 释放空闲复检/顶部锁）< B（begin 整个临界区，含 fsync）< F（worker 进入 finally 获取锁）** 时，begin 内 `_ensure_worker` 仍只凭 `is_alive()=True`（旧线程正阻塞在 finally 获取前）不启后继，随后 finally 身份匹配而置空——gen2 无 worker，实测 1s 无自发恢复；任意一次 `kick()` 即恢复并收敛删文件（实证 active False、_thread None）。即"begin 整体先于 detach 抢到锁"这一亚微秒~微秒级、需病态调度配合的线性序仍未被覆盖（R2 建议的"begin/kick 释放锁后复检 is_alive"未实现，现方案只覆盖 detach 先于 begin 的序）。60 次自然快节奏循环该窗口 **0 命中**；后果仅延迟收敛（ACTIVE 状态持续退避、状态文件在、resume_on_startup/任何 kick 均恢复），无正确性/安全性影响，不构成降级理由。可选加固（后续小版本一行级）：begin/kick 在释放管理器锁后复检 `_thread and _thread.is_alive()`，已死则重启。

#### 最终裁决（R3）

- F-8：**closed**（三路径 + legacy + 无码回退全部实证，69/42 测试与 ruff 全绿）。
- F-9：**closed**（三项指定断言均成立；残留更窄的理论锁序窗口仅能被注入触发、自然压测 0 命中、后果与 R2 已接受的 low 残余相同）。
- **总体结论：维持 PASS，不降级。**
