# Independent Review — FFmpeg 帧提取进程所有权与启动回收

- Reviewer：独立通用代理（全新上下文，未采信实施者自评）
- Date：2026-10-10
- 输入：spec.md、tasks.md、9 个生产文件改动 + 2 个新模块、7 个新测试文件、2 个修改测试文件
- 运行环境：macOS 14（无 /proc；ffmpeg 在评审中段通过 brew 安装；e2e 为 Linux-only，本地不运行）

## Checkpoint 结果

| Checkpoint | 结果 | 关键证据 |
|---|---|---|
| CP-R1 信号安全（回收四重验证、默认拒绝、SIGCONT 优先） | PASS | `process_claims.recover_orphaned_processes` 七世界假 procfs 单测（tests/test_process_claims_recovery.py），EPERM 三态补测 |
| CP-U1 API 鉴权 / 不泄密 / CSRF | PASS | tests/test_process_cleanup_api.py 7 项（401、无 token/argv、resolve 404）；CSRF 走全局 before_request |
| CP-R2 AC 与证据覆盖 | PASS（修复后） | M1 已修复并补 store 构造失败测试；269 项相关测试全绿 |
| CP-R3 清理顺序与边界 | PASS | 先 resolve 历史 TODO 再清理；仅 ephemeral 目录+stderr 日志；TR-5.5 cleanup_failed 用例 |
| CP-C1 并发幂等 | PASS | 双线程 terminate/release 单测；句柄锁 + RLock；occurrences 计数为纯观测字段（评审备注） |
| CP-R4 优雅关闭 | PASS（修复后） | 四结局+优雅超时分支测试；L1 atexit 不叠加测试；总预算 ≤25s |
| CP-N1 非 Linux 降级 | PASS | procfs 缺失只读、零信号、零异常（test_process_claims_identity/recovery）；Windows 导入兼容由 test_windows_compatibility 覆盖 |

## 评审发现与处置

- **M1（Medium）存储层构造失败仍留活体孤儿** → 已修复：`ffmpeg_runner` 发布块改为捕获 `(ClaimPersistError, OSError)`，杀子进程并归一化为 ClaimPersistError；新增 `test_store_construction_failure_kills_child_and_aborts`。
- **L1 atexit 在 gunicorn 重复 create_app 下叠加** → 已修复：注册标志移到 gunicorn 分支外；新增 `test_atexit_hook_registers_once_even_repeated_calls`（模拟 gunicorn、注册一次、configure 每次执行）。
- **L2 EPERM 被当作 ESRCH** → 已修复：`_kill_process_group` 返回 True/False/None 三态；回收路径 denied 时不判亡、不发后续信号，保留 claim+TODO；补 `test_signalling_denied_is_not_treated_as_gone`。
- **L3 铃铛锚点不匹配** → 已修复：面板 section id 改为 `process-cleanup`（与 `/logs#process-cleanup` 一致）。
- **L4 cleanup_failed 缺 job_id** → 已修复：`_cleanup_temp_directory(tmp_path, job_id=None)`，multi_server 传 `originating_job_id`。
- **L5 reconcile 请求时长无界** → 已修复：用户触发的回收使用 grace=2s/kill=2s 小预算，启动回收仍为 10s/5s。
- **L6 resolve todo_id 未白名单** → 已修复：`resolve_todo` 仅接受 32 位 hex，非法形态返回 None（端点 404）。
- 评审第 2 节测试缺口 1/3/4/7 已补：store 构造失败、environ 不可读、优雅超时结局、级联两次 Popen claim 交接、reconcile 进程类 TODO 自动消除。
- 观察项（损坏 claim 文件重复告警）：符合 spec"跳过+TODO"要求，不改。

## 回归证据

- 新/改测试：269 项全部通过（procfs 身份 17、store 17、runner claim 16、上下文 6、回收 17、API 8 含新增 atexit、deadline/media_processing 等改造回归 188）。
- 扩展回归：gpu_cant_decode、partial/stale output、golden args、progress decode、retry cascade、chapter、worker、frame cache、app/basic、notifications、windows compatibility、restart leftover、orchestrator fallback 等 1186 项通过；唯一 2 个失败（NFD/NFC 文件名）在基线与 macOS 上同样失败，与改动无关。
- 全量前后失败集合零差异（329 个环境失败：本机缺 ffmpeg（评审中后安装）/jeKyll/`/proc`，及 Linux-only e2e boot payload），新增 73 个测试全部通过。
- `ruff check media_preview_generator tests`：All checks passed；ruff format 干净。
- e2e（tests/e2e/test_process_cleanup_panel.py）：本地无法运行（e2e 应用 boot payload 使用 Linux `libc.so.6` prctl，既有 e2e 同样本地崩溃），由 CI Linux 环境验证；面板 API/通知层已由单测覆盖。

## 遗留事项（非阻塞）

- e2e 旅程仅在 CI Linux 运行。
- 评审观察项：损坏的 active claim 文件每次启动重复提示，可在未来版本将其隔离到 quarantined 目录。

## Verdict

PASS — AC-1..AC-10 均有实现与证据；评审提出的 1 Medium + 6 Low 全部修复并补测试；无 Critical/High 残留；默认拒绝的信号安全模型、并发幂等与非 Linux 降级经独立验证。
