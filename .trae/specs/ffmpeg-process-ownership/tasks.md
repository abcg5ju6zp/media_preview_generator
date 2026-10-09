# FFmpeg 帧提取进程所有权与启动回收 - 实施计划

实施顺序即依赖顺序；每个切片自带测试。除 T7 外均为 rule 型验收。
新增模块预期：`media_preview_generator/processing/procfs_info.py`、
`media_preview_generator/processing/process_claims.py`；
改动：`processing/ffmpeg_runner.py`、`processing/generator.py`、
`processing/multi_server.py`、`processing/chapters.py`、
`jobs/worker.py`、`markers/job_runner.py`、`web/app.py`、
`web/notifications.py`、`web/routes/api_system.py` 及一个模板/JS。

## Task 1: procfs 身份抽象层与实例身份
- **Status**: `in_progress`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  - 新增 `processing/procfs_info.py`，集中所有 Linux procfs 读取，全部函数可在测试中注入：
    - `procfs_available() -> bool`；
    - `read_start_ticks(pid) -> int | None`（解析 `/proc/<pid>/stat`，以最后一个 `)` 切分 comm，取第 22 列）；
    - `read_cmdline_bytes(pid) -> bytes | None`（NUL 连接、去尾 NUL）；
    - `read_environ(pid) -> dict[str,str] | None`（NUL 分隔，surrogateescape；不可读返回 None）；
    - `pid_alive(pid) -> bool`；
    - `argv_hash(args: list[str]) -> str`（与 cmdline 字节同规则：`b"\0".join(fsencode(...))` 的 SHA-256 hex）；
    - `self_identity() -> tuple[int,int|None]`（当前 pid + 自身 start ticks）。
  - 实例身份：`process_claims` 侧的 `instance_id`（`config_dir/instance_id`，uuid4 hex，0600，缺失时生成；本任务先实现 `load_or_create_instance_id` 与内存 `run_id`（uuid4 hex/启动））。
  - procfs 不可用（非 Linux、/proc 缺失）时读取函数返回 None/False，绝不抛异常。
- **Acceptance Criteria Addressed**: AC-1, AC-8, AC-10
- **Test Requirements**:
  - `rule` TR-1.1: 用伪造 /proc 目录（含 comm 带空格与括号的 stat、cmdline、environ）断言 start_ticks 列值、cmdline 字节、environ 解析正确；argv_hash 与伪造 cmdline 哈希一致。证据：pytest 单测。
  - `rule` TR-1.2: procfs 不可用注入下，全部读取函数返回 None/False 且无异常；`instance_id` 生成后稳定复用（两次调用同值、文件 0600）；`run_id` 同进程内稳定。证据：pytest 单测 + stat 权限断言。
  - `rule` TR-1.3: stat 损坏/字段不足/权限不可读分别返回 None，不抛异常。证据：pytest 单测。

## Task 2: 所有权凭证与待办存储（含 ClaimHandle 状态机）
- **Status**: `in_progress`
- **Priority**: `high`
- **Depends On**: Task 1
- **Description**:
  - 新增 `processing/process_claims.py`：
    - `configure_claims(config_dir)` / 进程级单例懒初始化（默认取 `CONFIG_DIR` 环境变量或 `/config`）；`get_claims_store()`；测试可注入 tmp 目录。
    - 目录布局：`process_claims/active/<claim_id>.json`、`process_claims/todos/<todo_id>.json`；原子写（同目录临时文件 + `os.replace`，0600，fsync 尽力）；目录创建 0700。
    - `publish_claim(*, kind, job_id, worker, ffmpeg_path, args, pid, pgid, output_folder, stderr_log, ephemeral_output, owner, token) -> ClaimHandle`：写 active 凭证（字段按 spec FR-1）；进程内注册表登记（供关闭协调器）。
    - `ClaimHandle`（每 claim 一把锁 + 终态标志）：
      - `mark_signalled()`/信号去重；`terminate_group(grace_s, kill_s, *, reason)`：SIGCONT→SIGTERM pgid→等待→SIGKILL pgid→等待；ESRCH 视为已退出；返回 `exited: bool` 与经过阶段；
      - `release_after_exit()`：仅在调用方能确认进程退出时删除 active + 注销注册；重复调用幂等；
      - `retain_with_todo(kind, detail)`：不撤销，落待办；
      - `attach_reaper(reap_fn)`：供章节 detach reaper 在真正 wait 到退出后补 `release_after_exit()`。
    - 待办：`add_todo(kind, *, job_id, worker, pid, claim_id, paths, detail)` 按 `(kind,pid,claim_id/path)` 去重更新 updated_at/detail；`list_todos(status)`、`resolve_todo(id, note)`、`resolve_matching(predicate)`；损坏 JSON 跳过不炸。
    - `scan_active_claims()`：读取全部 active 凭证，损坏文件以占位记录返回供上层记待办。
    - 写盘失败策略：`publish_claim` 持久化失败抛 `ClaimPersistError`（供 runner 放弃本次 Popen）；待办写失败只 WARNING。
  - 不改 runner；纯模块 + 单测（信号发送、时钟、procfs 全部注入）。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-4, AC-5, AC-10
- **Test Requirements**:
  - `rule` TR-2.1: publish→active 文件存在且字段/0600 正确；release 后文件消失；重复 release 无副作用。证据：单测检查文件系统。
  - `rule` TR-2.2: `terminate_group` 序列为 SIGCONT→SIGTERM→（grace 超时）→SIGKILL；SIGTERM 即退时不发 KILL；ESRCH 视为退出。证据：假 pgid/kill/sleep 记录调用序列。
  - `rule` TR-2.3: 待办四类型落盘字段完整；同键 add 两次只更新不新增；resolve 后 status=resolved；`resolve_matching` 能按 pid/claim_id 批量消除。证据：单测。
  - `rule` TR-2.4: `publish_claim` 在不可写目录抛 `ClaimPersistError`；损坏的 active/todo JSON 被 scan 跳过并可标识。证据：单测。

## Task 3: runner 集成——发布、进程组信号、幂等撤销、关闭协调器
- **Status**: `in_progress`
- **Priority**: `high`
- **Depends On**: Task 2
- **Description**:
  - `ffmpeg_runner.py`：
    - `create_ffmpeg_runner` 新增 `claim_context: ClaimContext | None = None`、`ephemeral_output: bool = False`（其余签名不变，golden argv 不受影响）。
    - Popen 改 `start_new_session=True`；env 为 None 时复制 `os.environ`，注入 `MPG_PROCESS_CLAIM=<token>`。
    - Popen 成功后读 pid/pgid/start_ticks，组装 argv hash 与 worker/作业上下文（显式参数 → contextvar → 缺省 kind 推断：`chapter_output` 非空为 chapter，否则 preview），调 `publish_claim`；持久化失败则终止刚启动进程并抛 `ClaimPersistError`（被生成器现有逐文件失败路径捕获）。
    - 轮询内取消分支：经句柄 `terminate_group(grace=5, kill=5, reason="cancel")`；确认退出并读完尾部 stderr 后 `release_after_exit()`，再抛 `CancellationError`（文案/类型不变）；未确认退出则 retain `graceful_terminate_timeout`/`kill_failed` 待办后照旧抛错。
    - 自然退出与取消并发：终态标志保证取消分支与"poll 观察到退出"只收敛一次；信号去重。
    - 暂停/恢复：改对进程组 SIGSTOP/SIGCONT（日志、停滞时钟冻结逻辑不变）。
    - 停滞看门狗、活跃超时、诊断输出上限、GPU 不可解码裁决：SIGKILL/terminate 改走句柄；章节 `reap_killed_process` detach 场景：retain 待办（kill 已发但未确认），reaper 线程 wait 成功后调 `release_after_exit()`；`PROCESS_EXIT_PENDING_LINE` 合成 rc 语义保留。
    - 正常完成路径：尾部 stderr 读取收敛后（现有 finally 之后、诊断逻辑前后均可，统一在 return 前）`release_after_exit()`；任何提前 return/异常路径不得漏释放（try/finally 中区分"已确认退出"与"未确认"）。
    - stderr 日志路径记入凭证。
  - 关闭协调器（`process_claims.py` 内）：`shutdown_all_claims(total_budget_s=25)` 对注册表内在途句柄并行 SIGCONT→SIGTERM→单轮共享截止→SIGKILL→短等待；确认退出者 release；未确认者 retain 待办；全程吞异常。在 `web/app.py` 注册 `atexit`（与 scheduler 关闭并列），并尽力在非 gunicorn 场景链式挂 SIGTERM/SIGINT（信号安装 try/except，不覆盖失败不报错）。
  - `generator.py`：`generate_images` 增加 `claim_context`/`ephemeral_output` 透传参数（默认 None/False，不破坏既有调用与测试）。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-6, AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-3.1: 用假 Popen/假 procfs 跑 `_run_ffmpeg` 成功路径：凭证在轮询前发布、return 前撤销；argv 列表与 golden 完全一致（复用 `test_ffmpeg_runner_golden_args.py` 夹具并扩展断言）。
  - `rule` TR-3.2: 取消路径断言 SIGCONT/TERM/KILL 组信号序列、CancellationError 文案与既有行为一致、claim 已撤销；TERM 超时→KILL 后仍存活时落 `kill_failed` 待办且 claim 保留。
  - `rule` TR-3.3: 并发取消与自然退出（屏障注入：cancel 在 poll 判定退出同刻返回 True）：信号一轮、claim 恰好撤销一次、无异常。
  - `rule` TR-3.4: 暂停路径断言组 SIGSTOP/SIGCONT 配对与恢复日志；停滞看门狗/活跃超时/GPU 不可解码三条路径均走句柄且诊断常量行不变。
  - `rule` TR-3.5: detach reaper 路径：未确认退出时 claim 保留+待办；reaper 回调后 claim 撤销；`PROCESS_EXIT_PENDING` 合成返回码不变。
  - `rule` TR-3.6: `shutdown_all_claims` 在假时钟下对四类句柄（自然退/TERM 退/KILL 退/不退）给出正确终态，总耗时不超过预算，异常不外抛；atexit 注册存在。
  - `rule` TR-3.7: `publish_claim` 失败时刚启动的子进程被终止、不进入轮询、异常类型可被现有失败路径捕获。

## Task 4: 作业/worker 上下文注入与临时目录清理待办
- **Status**: `in_progress`
- **Priority**: `high`
- **Depends On**: Task 3
- **Description**:
  - `jobs/worker.py`：在 `_process_item`（job_thread 上下文内）与 `_process_custom_item` 设置 ClaimContext（job_id=current_job_id、worker id/type/display_name、gpu/gpu_device、group_id/member_id）；线程结束复位（与 job_thread 同一 finally）。
  - `markers/job_runner.py`：`_run_intro_credits_pass` 的 `register_job_thread` 旁设置 markers 作业 ClaimContext（kind 由 runner 按 chapter_output 推断，job_id 真实；worker 字段取 markers 作业可用信息，缺省为空）。
  - `multi_server.py`：调用 `generate_images` 时透传 ClaimContext（线程已由 worker 设置，故主要补 `ephemeral_output`：仅 `cleanup_path is not None`（非缓存 ad-hoc tmp）时为 True）；`_cleanup_temp_directory` 失败分支除现有 WARNING 外落 `cleanup_failed` 待办（pid 可空、path=tmp_path、job_id 取 originating_job_id 尽力）。
  - `chapters.py`：runner 调用补 `claim_context`（默认从 contextvar 取，无需强制传参），确认章节 `ephemeral_output=False`（TemporaryDirectory 自行管理）。
  - 验证 GPU→CPU 回退：同文件两次 runner 调用各自独立 claim，旧 claim 已在旧进程退出后撤销（由 T3 机制保证），此处补一条端到端断言。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-5, AC-9
- **Test Requirements**:
  - `rule` TR-4.1: worker 执行在途时 contextvar 含正确 job_id/worker，结束后复位；兄弟并发作业不串值（参照现有 D5 测试风格）。
  - `rule` TR-4.2: 缓存模式 `ephemeral_output=False`；非缓存 ad-hoc tmp `ephemeral_output=True`；`_cleanup_temp_directory` 注入 rmtree 失败时出现 `cleanup_failed` 开放待办。
  - `rule` TR-4.3: GPU→CPU 回退测试（复用 orchestrator CPU fallback 测试夹具）断言两次 Popen 各自发布/撤销 claim，无 active 残留。
  - `rule` TR-4.4: markers 作业线程内章节抓帧凭证携带真实 job_id。

## Task 5: 启动孤儿回收与启动顺序
- **Status**: `in_progress`
- **Priority**: `high`
- **Depends On**: Task 4
- **Description**:
  - 在 `process_claims.py` 实现 `recover_orphaned_processes(config_dir=None) -> dict`：
    - 确保 instance_id；procfs 不可用：日志 INFO 说明跳过信号回收，返回零值汇总（不扫待办噪声）。
    - 扫描 active：他实例 `instance_id` → 跳过且不写待办；损坏凭证 → WARNING + `identity_unconfirmed`；
    - owner 存活（pid+start ticks 一致）→ 跳过保留；
    - owner 死亡：目标不存在→按凭证清理（仅 ephemeral 目录 + stderr 日志，best-effort，失败落 cleanup_failed）→release→resolve_matching；
    - 目标存在：四重验证（start ticks / environ token / cmdline hash / pid）。start ticks 不符 → 不发信号、release、identity 待办；token 或 cmdline 不符/不可读 → 不发信号、保留凭证、identity 待办；全过 → SIGCONT→TERM(10s)→KILL(5s)（复用句柄/序列器，直接对 pgid）；退出后清理+release+resolve；仍存活 → kill_failed/graceful 待办、保留。
    - 返回 `{killed, exited_already, skipped_owner_alive, skipped_foreign, unconfirmed, cleanup_failed, todos_open}` 并 INFO 汇总。
  - `web/app.py::create_app`：在 `_requeue_interrupted_on_startup(config_dir)` 之前调用（顺序需可被测试断言），异常 WARNING 不阻断启动。
  - 回收器为纯可注入设计（procfs 层、kill 包装、sleep/截止时钟均可替换），不真发信号。
- **Acceptance Criteria Addressed**: AC-3, AC-4, AC-8
- **Test Requirements**:
  - `rule` TR-5.1: 七种凭证世界（可验证孤儿/目标已退出/PID 复用/token 不符/cmdline 不符/owner 存活/他实例）+ 损坏文件，在假 procfs/假信号器下逐条断言信号集合、凭证终态、待办集合与汇总计数。
  - `rule` TR-5.2: 可验证孤儿的终止序列含先发 SIGCONT（覆盖暂停挂起场景）；退出成功后 ephemeral 目录与 stderr 日志被删除、非 ephemeral 目录不被删。
  - `rule` TR-5.3: procfs 不可用注入下回收零信号、零异常、零新增待办。
  - `rule` TR-5.4: 启动顺序测试（patch 两个函数记录调用序）断言 recover 先于 `_requeue_interrupted_on_startup`；recover 抛异常时重排仍执行。
  - `rule` TR-5.5: 清理失败落 cleanup_failed 且 claim 已撤销（进程已死，目录残留不影响撤销）。

## Task 6: 待办 API、通知中心卡片与管理面板
- **Status**: `completed`
- **Completion Evidence**: GET/POST reconcile/POST resolve 三端点实现（tests/test_process_cleanup_api.py 7 项全绿，含鉴权/无 token 泄露/reconcile 自动消除）；通知卡片 PROCESS_CLEANUP_ID 会话级（notifications.py）；Logs 页面板与交互（logs.html，e2e tests/e2e/test_process_cleanup_panel.py，CI Linux 运行）。
- **Priority**: `medium`
- **Depends On**: Task 5
- **Description**:
  - `web/routes/api_system.py`：
    - `GET /api/system/process-cleanup`：开放待办（类型/pid/job/worker/路径/时间/原因）、active claim 摘要（pid/job/kind/created_at，无 argv/token）、最近一次回收汇总（可由 store 内存保留最近一次结果）。
    - `POST /api/system/process-cleanup/reconcile`：重跑一次以既有凭证/待办为对象的核对（进程退出/目录消失即自动 resolve），返回前后计数。
    - `POST /api/system/process-cleanup/<todo_id>/resolve`：手工 dismiss。
    - 鉴权与 CSRF 与该蓝图其他 POST 一致。
  - `web/notifications.py`：新增稳定 id `process_cleanup_todos`，存在开放待办时返回卡片（数量 + 首要类型聚合，文案说明"重启后仍残留的帧提取进程/临时目录"与处理入口）；该卡片不可永久 dismiss（待办是持久事实，按会话消除即可，复用 session-only 机制）。
  - UI：在现有管理员可达页面（Logs 页或 Settings 高级区，实施时择一既有页面）加"Process cleanup"分区：列表 + Recheck all + Dismiss；`app.js` 或新增最小 JS 调用三 API；bell 卡片链到该分区。遵循现有模板/CSS 规范，不引入框架。
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `rule` TR-6.1: API 单测：无待办时空列表；构造待办后 GET 字段正确且不泄露 token/argv；reconcile 在进程已死/目录已删时自动 resolve；手工 resolve 生效；未登录/CSRF 行为与现有 system POST 一致。
  - `rule` TR-6.2: 通知构建器在有/无开放待办时分别返回/不返回 `process_cleanup_todos`，复用现有通知测试夹具。
  - `rule` TR-6.3: 新增一条 e2e（tests/e2e，playwright 风格与既有旅程一致）：制造开放待办→bell 出现卡片→进入面板看到行→Recheck/Dismiss 后卡片消失。

## Task 7: 全量回归、跨平台与质量收口
- **Status**: `completed`
- **Completion Evidence**: 改动后全套件 19126 passed/329 failed，与基线（stash 后 19053 passed/329 failed）失败集合逐行零差异——329 个失败全部为本 macOS 环境缺 ffmpeg/jeKyll/`/proc` 的既有环境失败；新增 73 个测试全部通过；ruff check 全仓通过、format 干净；非 Linux procfs 降级单测覆盖（test_process_claims_identity/recovery）。
- **Priority**: `high`
- **Depends On**: Task 6
- **Description**:
  - 跑受影响测试全集：ffmpeg runner 三件套、active deadline、retry cascade、processing 系列、gpu_cant_decode、partial output、stale frames、chapter 系列、pause 释放槽位、job dispatcher、worker、orchestrator CPU fallback、restart leftover preview jobs、notifications 系列、csrf、windows compatibility、e2e 取消旅程。
  - 核查无原始 argv / 媒体凭据 / token 之外敏感信息落盘或出现在 GET API；文件权限复核。
  - 复核 `pyproject` lint/类型约束（项目使用 pre-commit；按 `.claude/rules/python.md` 风格）。
- **Acceptance Criteria Addressed**: AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-7.1: `pytest tests/test_ffmpeg_runner_golden_args.py tests/test_ffmpeg_active_deadline.py tests/test_retry_cascade.py tests/test_processing_gpu_cant_decode.py tests/test_processing_partial_output.py tests/test_processing_stale_frames.py tests/test_previews_pause_releases_slot.py tests/test_restart_leftover_preview_jobs.py tests/test_chapter_extraction_edges.py tests/test_chapter_fatal_extraction.py tests/test_notifications_api.py tests/test_csrf.py tests/test_windows_compatibility.py -q` 全绿（以实际文件名为准运行等价集合）。
  - `rule` TR-7.2: 新增测试模块全部通过；整轮 `pytest -q` 无新增失败（若有预存失败需在证据中列明与本次无关）。
  - `rubric` TR-7.3: 信号安全与实现质量；1-5；锚点见 AC-10；阈值 >=4；证据：模块边界走查 + 独立评审。
