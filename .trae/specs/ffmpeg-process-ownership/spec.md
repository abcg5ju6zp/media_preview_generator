# FFmpeg 帧提取进程所有权与启动回收 - 产品需求文档

## Overview

- **Summary**: 为每次帧提取 FFmpeg 子进程发布可持久验证的所有权凭证（作业、worker、命令身份、OS 进程启动标识），在输出收敛后撤销；应用重启时只终止能被严格证明"属于本实例且原 owner 已死亡"的进程；终止/清理失败保留可观察待办；并在优雅关闭时主动收敛在途进程。
- **Purpose**: 外层 supervisor（s6）只重启 Web 主进程、或应用等待 worker 超时后继续退出时，在途 FFmpeg 被 PID 1 收养并继续占用 GPU、CPU 与临时目录；新进程只能把作业标中断、提示管理员手工清理。本变更让进程归属可证明、可回收、可审计，杜绝误杀 PID 复用/他实例进程。
- **Target Users**: 部署本应用（Docker/s6 或裸机）的管理员；以及在同一宿主上运行多个实例的用户。

## Goals

- 每次帧提取 FFmpeg 启动都落盘一份原子写入、可跨重启验证的所有权凭证。
- 凭证撤销与进程真实退出、输出收敛严格绑定；取消/自然退出/降级重试并发下幂等。
- 启动回收在作业失败/自动重排/恢复决策**之前**完成：身份四重验证通过才发信号，任何一项不可证实都不发信号。
- 温和终止超时、强制终止失败、临时目录清理失败、身份不可确认分别形成持久化、可观察、可消除的待办。
- 优雅关闭时在 gunicorn 优雅窗口内主动收敛在途 FFmpeg，未收敛的留给新实例回收。
- 保持普通成功、硬件→CPU 降级、暂停（SIGSTOP/SIGCONT）、既有错误诊断的原有语义。

## Non-Goals

- 不覆盖响度分析 FFmpeg（`loudness/analyze.py`，已有独立 session、仅用管道、无临时帧目录）、ffprobe/Inspector 短命探测（已有超时 + reaper 机制）、textdet 助手等其他子进程。
- 不做宿主级跨容器/跨 PID namespace 的进程回收；回收只作用于同一 procfs 视图内可验证的进程。
- 不新增作业状态机：待办是运维层对象，不改变 Job 的状态与计数语义。
- 不改变 FFmpeg argv 构造、过滤链、重试级联与诊断文案本身。

## Background & Context

- 帧提取 FFmpeg 全部经统一入口 `processing/ffmpeg_runner.py::create_ffmpeg_runner`（预览抽帧 `processing/generator.py:1727` 与章节抓帧 `processing/chapters.py:378/418`），当前以裸 `subprocess.Popen` 启动：无独立会话、无所有权记录；取消用 `proc.terminate()→wait(5)→kill()`，暂停用对单 PID 的 SIGSTOP/SIGCONT。
- 生产进程模型：s6-overlay 监督 `wrapper.sh` 内的单 worker gunicorn（`--graceful-timeout 30`）。应用退出只 `atexit` 关闭 APScheduler（`web/app.py:979-986`），`JobDispatcher.shutdown` 无退出钩子，在途 FFmpeg 不接收任何信号即被收养。
- 启动恢复现状：`JobManager._load_jobs` 把 RUNNING 作业标 FAILED（"Job was interrupted by server restart"），`_requeue_interrupted_on_startup` 依据设置重排；两者对旧 FFmpeg 进程无感知。
- 临时目录：帧缓存槽位 `<tmp_folder>/frame_cache/frames-<hash>`（跨作业复用，**不得**被回收删除）；非缓存模式临时目录 `<working_tmp_folder>/frames-<hash>`（`multi_server.py:2048-2049,2351-2352`，用完即删）；章节帧写在 `TemporaryDirectory`（上下文管理器自动清理）；FFmpeg stderr 落 `<系统临时目录>/ffmpeg_output_<pid>_<tid>_<ns>.log`，正常结束 finally 删除。
- 持久卷 `config_dir`（`$CONFIG_DIR`，默认 `/config`）存放 jobs.db 与设置，是凭证与待办的存储位置；不得放入可能被清空的 tmp_folder。
- argv 中的 `-i` 路径可能含凭据（代码中明确不以 INFO 记录完整 argv）：凭证只存 argv 的 SHA-256，不存原始 argv。
- 运行环境为 Linux 容器；开发/测试可能在 macOS（无 /proc）：procfs 不可用时回收降级为只读、不发信号。

## Functional Requirements

### FR-1：所有权发布
- 每次 `create_ffmpeg_runner` 内 `Popen` 成功后、进入轮询前，必须在 `config_dir/process_claims/active/<claim_id>.json` 原子写入（临时文件 + `os.replace`，权限 0600）一份凭证，字段至少包含：
  - **作业**：`job_id`（可为空但字段必须存在）、`kind`（`preview`/`chapter`）；
  - **worker**：worker id/类型/显示名/GPU 类型/GPU 设备路径/分组标识；
  - **命令身份**：ffmpeg 可执行路径、argv 的 SHA-256（按 `/proc/<pid>/cmdline` 的 NUL 连接规则对字节求哈希）；
  - **OS 进程启动标识**：`pid`、`pgid`、`/proc/<pid>/stat` 第 22 列 `starttime`（clock ticks）；
  - **owner**：`instance_id`、本次启动随机 `run_id`、owner pid 与其 starttime；
  - **环境令牌**：每个进程随机生成的 `token`，同时以 `MPG_PROCESS_CLAIM=<token>` 注入子进程环境；
  - **路径**：输出目录、stderr 日志路径、是否为可删除临时目录（`ephemeral_output`）；
  - `version`、`claim_id`、`created_at`、`state="active"`。
- 子进程必须以 `start_new_session=True` 启动（独立会话/进程组），使暂停/取消/回收可针对整个进程组。
- 作业/worker 身份通过线程边界的 ClaimContext（contextvar，由 jobs worker 线程与 markers 作业线程设置）获取，并允许 `create_ffmpeg_runner` 显式参数覆盖。
- 凭证无法持久化时，本次 FFmpeg 尝试不得启动（抛出可被现有逐文件失败路径捕获的异常，队列继续），避免制造无法回收的孤儿。

### FR-2：所有权撤销（输出收敛后）
- 正常完成：轮询结束、尾部 stderr 读取收敛、returncode 确定后撤销（删除 active 文件）。
- 用户取消：SIGCONT（若暂停中）→SIGTERM 进程组→限时等待→SIGKILL 进程组→确认退出后撤销；取消信号路径的诊断与异常类型保持不变。
- 降级重试：GPU→CPU 回退以及重试级联的每一次新尝试，都必须在旧进程确认退出后撤销旧 claim，再为新 Popen 发布新 claim。
- 撤销、终止、状态翻转必须经每 claim 句柄的锁与终态标志保证幂等：自然退出与取消并发时信号只发一轮、claim 只撤销一次、不产生线程异常。

### FR-3：运行期容量与既有语义
- worker/GPU 槽位只在 runner 返回（进程已确认退出）后释放——现状即如此；唯一例外是章节路径在卡死挂载上 SIGKILL 后 detach 的 reaper（"A blocked mount must not hold a chapter worker after SIGKill"），该语义保留，但 claim 不得撤销、必须落待办，由后台 reaper 确认退出时补撤销或由启动回收收敛。
- 暂停仍为 SIGSTOP/SIGCONT（改为对进程组），冻结停滞时钟等行为不变；普通成功、硬件→CPU 降级、停滞看门狗、GPU 不可解码裁决、章节活跃超时、既有错误诊断文案与计数语义保持不变。

### FR-4：启动孤儿回收
- `create_app` 中、`_requeue_interrupted_on_startup` **之前**执行回收；回收异常不得阻断启动。
- 对 active 目录每条凭证：
  - **owner 仍存活**（owner pid 存在且 starttime 一致）：完全不触碰（共享配置卷上的他者活跃实例场景）。
  - **owner 已死亡**且目标进程不存在：视为已退出，按凭证登记清理临时资源（仅 `ephemeral_output=true` 的目录与 stderr 日志），撤销凭证，解除关联待办。
  - **owner 已死亡**且目标进程存在时，必须同时满足四项才允许发信号：pid 存在、starttime 一致、`/proc/<pid>/environ` 中令牌一致、cmdline 哈希一致。
  - 验证通过的回收序列：SIGCONT 进程组（解除可能的暂停挂起）→SIGTERM 进程组→有界等待（默认 10s）→SIGKILL 进程组→有界等待（默认 5s）；确认退出后再清理临时资源、撤销凭证、解除关联待办。
  - starttime 不符（PID 复用）、environ 不可读/令牌不符、cmdline 哈希不符：**绝不发信号**；记录 `identity_unconfirmed` 待办。starttime 不符意味着凭证指向的进程已不存在，撤销凭证；令牌/cmdline 不可证实则保留凭证待下次启动再判。
  - `instance_id` 与本实例不同的凭证：完全不触碰、不写待办（属于共享卷上其他实例）。
- 损坏/无法解析的凭证文件：跳过并记录 WARNING 与 `identity_unconfirmed` 待办，不中断扫描。
- 回收输出汇总（已终止/已自然消失/跳过/待办数）写入日志。

### FR-5：可观察待办
- 待办原子落盘于 `config_dir/process_claims/todos/<todo_id>.json`（0600），类型固定为：
  - `graceful_terminate_timeout`（SIGTERM 超时）；
  - `kill_failed`（SIGKILL 后仍未确认退出，含 D 状态 detach）；
  - `cleanup_failed`（登记的临时目录/日志删除失败）；
  - `identity_unconfirmed`（PID 复用/身份不可证实/损坏凭证）。
- 每条含 todo_id、类型、创建/更新时间、job_id、worker 信息、pid、claim_id、相关路径、原因、状态（open/resolved）、消除说明；按（类型,pid,claim_id/路径）去重更新而非刷屏。
- 条件自动解除：回收或 recheck 观察到进程已退出且目录清理成功时，关联待办自动 resolved；管理员也可手工 dismiss。
- 现有 `_cleanup_temp_directory` 的清理失败 WARNING 必须同时落 `cleanup_failed` 待办。

### FR-6：优雅关闭收敛
- 注册进程级关闭协调器（atexit 为主，SIGTERM/SIGINT 尽力链式处理，不与 gunicorn 信号管理冲突；任何异常不得阻断退出）。
- 触发时对全部在途 claim：SIGCONT→SIGTERM 进程组→有界等待→SIGKILL 进程组→短等待；并行等待且总预算有界（默认 ≤25s，小于 gunicorn 30s 优雅窗口）。
- 确认退出者撤销 claim；未确认者保留 active 凭证并落 `graceful_terminate_timeout`/`kill_failed` 待办，交由新实例启动回收。

### FR-7：待办界面与 API
- 提供 `GET /api/system/process-cleanup`（开放待办 + 在途 claim 摘要 + 最近回收汇总）、`POST /api/system/process-cleanup/reconcile`（立即重新核对并自动消除已解除项）、`POST /api/system/process-cleanup/<todo_id>/resolve`（手工确认）；鉴权与 CSRF 行为与现有 system API 一致。
- 通知中心在存在开放待办时显示一张持久卡片（稳定 id，如 `process_cleanup_todos`），展示数量与首要原因并可进入待办列表；列表展示 PID、作业、worker、类型、路径、时间，提供 Recheck / Dismiss。

### FR-8：实例身份与跨平台
- `instance_id` 首次启动时生成（uuid4 hex），存 `config_dir/instance_id`（0600），之后稳定复用；`run_id` 每次进程启动随机生成、仅存内存并写入当次凭证。
- procfs 读取集中在单一抽象层（便于在 macOS/Windows 与单测中注入）：/proc 不可用时，正常发布/撤销仍工作（凭证内 procfs 字段为空），启动回收只做只读扫描、不发信号、不抛异常。

## Non-Functional Requirements

- **NFR-1 安全性**：任何 kill/terminate 必须通过 FR-4 的四重验证；凭证文件 0600；不持久化原始 argv、不持久化任何媒体侧凭据；token 用密码学随机源。
- **NFR-2 可靠性**：凭证/待办全部原子写、损坏容忍；文件系统错误只降级为 WARNING/待办，不阻断抽帧主流程之外的启动路径与作业调度。
- **NFR-3 性能**：每次 Popen 仅增加一次小文件原子写与删除；启动回收为有界 I/O，不主动扫描整个 procfs（只检查凭证内 PID）。
- **NFR-4 可测试性**：procfs、时钟、信号发送均可注入；关键竞态（取消 vs 自然退出、PID 复用、owner 存活、令牌不符）具备确定性单测。
- **NFR-5 兼容性**：现有测试套件（runner、取消旅程、暂停释放槽位、章节边界、重启遗留作业、通知 API 等）全部保持通过。

## Constraints

- **Technical**: Python 3 标准库实现（不新增依赖）；Linux procfs 语义；gunicorn gthread 单 worker + s6-overlay；持久化位置限定在 config_dir。
- **Business**: 不得改变用户可见的作业成败、降级、暂停与错误诊断既有语义。
- **Dependencies**: 依赖 config_dir 可写（不可写时按 FR-1 使当次 FFmpeg 尝试失败并给出明确日志）；无新增外部服务。

## Assumptions

- 同一容器内 Web 主进程与其 FFmpeg 子进程共享同一 PID namespace 与 /proc 视图（s6 只重启服务、不重建容器的场景成立）。
- 容器重建时旧进程随 PID namespace 销毁，残留凭证在新容器中表现为"目标进程不存在"，走只清理路径，安全。
- FFmpeg 自身不衍生需要同组信号的子进程；即便衍生，进程组信号也是期望行为。
- config_dir 卷不与不可信容器共享；0600 + 随机 token 已提供同主机纵深防御。

## Acceptance Criteria

### AC-1: 每次抽帧启动都发布完整所有权凭证
- **Type**: `rule`
- **Given**: 一次预览抽帧或章节抓帧即将 Popen FFmpeg
- **When**: Popen 成功且轮询开始前
- **Then**: active 目录出现 0600 原子写入的凭证，含 job_id、worker 身份、ffmpeg 路径与 argv SHA-256、pid/pgid/starttime、owner（instance_id/run_id/pid/starttime）、随机 token、输出/stderr 路径、ephemeral 标志、state=active；子进程环境含 `MPG_PROCESS_CLAIM` 且以独立会话启动
- **Pass Condition**: 确定性单测断言上述字段存在且值与真实 Popen/procfs 一致，文件为原子替换产生
- **Evidence**: 新增单测（procfs 与 Popen 以假件注入）

### AC-2: 完成/取消/降级在输出收敛后撤销且幂等
- **Type**: `rule`
- **Given**: 一个已发布凭证的抽帧进程
- **When**: 正常返回；或取消触发 SIGTERM/SIGKILL 后确认退出；或 GPU→CPU/级联重试发起新尝试
- **Then**: 对应 active 文件在进程确认退出且 stderr 收敛后被删除；新尝试持有新 claim；取消与自然退出并发时信号仅一轮、claim 最终仅撤销一次、无异常逃逸
- **Pass Condition**: 单测覆盖三种路径与并发取消竞态（确定性屏障），文件系统与信号假件记录可验证
- **Evidence**: 新增单测 + 现有 `test_retry_cascade.py`/取消旅程通过

### AC-3: 启动回收先于作业决策且只杀四重验证通过的进程
- **Type**: `rule`
- **Given**: config_dir 中存在若干前次运行遗留的 active 凭证，目标 PID 分别为：可验证孤儿、PID 已复用、令牌不符、cmdline 不符、owner 仍存活、他实例凭证、目标已退出
- **When**: create_app 执行启动回收（位于作业重排之前）
- **Then**: 仅"可验证孤儿"被 SIGCONT→SIGTERM→(超时)SIGKILL 并在退出后撤销+清理；"目标已退出"只清理+撤销；PID 复用撤销凭证但不发信号；令牌/cmdline 不符不发信号且保留凭证；owner 存活与他实例完全不触碰
- **Pass Condition**: 注入 procfs/信号假件的单测逐条断言发送的信号集合与凭证终态；并断言回收先于 `_requeue_interrupted_on_startup`
- **Evidence**: 新增回收单测 + 启动顺序断言

### AC-4: 不可证实身份绝不发信号并保留可观察待办
- **Type**: `rule`
- **Given**: starttime 不符/environ 不可读或 token 不符/cmdline 哈希不符/凭证文件损坏
- **When**: 启动回收扫描到这些凭证
- **Then**: 不产生任何 SIGTERM/SIGKILL/SIGCONT；分别生成 `identity_unconfirmed` 开放待办（损坏凭证同样），含 pid、claim_id、原因、时间
- **Pass Condition**: 单测断言信号假件零调用且待办落盘内容正确
- **Evidence**: 新增单测

### AC-5: 三类失败分别落持久化待办且可消除
- **Type**: `rule`
- **Given**: SIGTERM 超时、SIGKILL 后仍存活（含 detach reaper）、临时目录删除失败
- **When**: 对应事件在运行期或关闭/回收期发生
- **Then**: 分别产生 `graceful_terminate_timeout`、`kill_failed`、`cleanup_failed` 待办（0600 原子写、字段完整、同类去重更新）；条件解除后经回收/recheck 自动 resolved，也可管理员 dismiss
- **Pass Condition**: 单测构造三类事件并断言落盘、去重、自动/手工消除；`_cleanup_temp_directory` 失败路径有待办
- **Evidence**: 新增单测

### AC-6: 优雅关闭在有界窗口内收敛在途进程
- **Type**: `rule`
- **Given**: 关闭协调器触发时存在正常退出、SIGTERM 即退、SIGKILL 才退、SIGKILL 仍存活四类在途 claim
- **When**: 协调器执行
- **Then**: 前三类在总预算 ≤25s 内并行收敛并撤销凭证；最后一类保留凭证 + `kill_failed` 待办；协调器异常不外抛、不阻断解释器退出；atexit 已注册
- **Pass Condition**: 注入时钟/信号假件的单测断言信号序列、预算上限与凭证/待办终态
- **Evidence**: 新增单测

### AC-7: 待办 API 与通知中心可观察
- **Type**: `rule`
- **Given**: 存在开放待办
- **When**: 管理员访问 system API / 通知中心 / 待办面板
- **Then**: GET 返回开放待办与在途 claim 摘要；通知中心出现 `process_cleanup_todos` 卡片；reconcile 能自动消除已解除项；resolve 可手工消除；POST 受现有 CSRF/鉴权保护
- **Pass Condition**: API 单测与 e2e 旅程断言卡片、列表、Recheck/Dismiss 行为
- **Evidence**: 新增 API 单测 + 一条 e2e 测试

### AC-8: 非 Linux/procfs 缺失安全降级
- **Type**: `rule`
- **Given**: procfs 读取层返回"不可用"（macOS/Windows 或 /proc 不可读）
- **When**: 抽帧发布、正常撤销与启动回收执行
- **Then**: 发布/撤销不因 procfs 缺失失败（procfs 字段留空）；回收只读扫描、零信号、零异常，应用正常启动
- **Pass Condition**: 单测以"procfs 不可用"假件跑通发布-撤销与回收路径
- **Evidence**: 新增单测

### AC-9: 既有语义零回归
- **Type**: `rule`
- **Given**: 现有全部相关测试
- **When**: 执行 pytest（含 runner golden args、进度解析、停滞/GPU 不可解码、章节活跃超时与边界、暂停释放槽位、重启遗留预览作业、取消旅程、通知、Windows 兼容）
- **Then**: 全部通过；FFmpeg argv 逐字节不变（golden args 测试）
- **Pass Condition**: 测试套件全绿
- **Evidence**: pytest 输出

### AC-10: 信号安全与实现质量
- **Type**: `rubric`
- **Dimension**: 所有权/回收子系统的安全边界与可维护性
- **Scale**: 1-5
- **Anchors**: 1 = 存在凭 PID 直接发信号路径或原始 argv 落盘；3 = 验证齐全但逻辑分散在 runner/启动多处难以审计；5 = 凭证模型、procfs 验证、信号发送、待办存储各自单一边界模块，默认拒绝（任何不确定即不发信号），无凭据落盘，竞态有确定性测试
- **Pass Threshold**: >= 4
- **Evidence**: 代码结构与独立评审

## Open Questions

- 无（覆盖范围、关闭行为、待办形式已经用户确认：仅帧提取 FFmpeg；优雅关闭主动温和终止+兜底强杀；持久化+API+通知中心卡片）。
