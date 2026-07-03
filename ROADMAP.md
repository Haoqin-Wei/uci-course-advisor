# UCI Course Advisor Roadmap

> 更新日期：2026-07-03
>
> 当前目标：把功能丰富的本地 Demo 收敛成可验证、可持续开发的私测版本。
>
> 执行规则：严格按阶段推进。每一阶段通过验收后，再进入下一阶段；README 在全部工程调整完成后最后更新。

## 1. 当前定位

项目已经完成主要产品形态，但工程上仍处于“高级本地 Demo”阶段。

### 已实现

- [x] FastAPI 后端和 SSE 流式聊天
- [x] Agent loop、工具调用、调用预算、错误降级和继续生成
- [x] 课程、section、教授、成绩、先修、政策和冲突查询工具
- [x] `propose_recommendation` 结构化推荐卡片
- [x] 可点击课程卡片和 Weekly Schedule
- [x] 邮箱验证码注册、密码登录和 Session Cookie
- [x] Onboarding、Profile、跨会话记忆和会话历史
- [x] 本地课程、section、教授评价和专业要求数据
- [x] Catalog、Provenance 和基础 Validation 框架

### 当前阻塞

- 新旧两套聊天执行链同时存在
- 内存 Session 与持久化 Session 同时保存状态
- Memory schema、文件写入和内存缓存不一致
- 推荐卡片和加课流程没有统一的课表组合校验
- 先修判断把 OR 条件当作 AND，并把未知状态当作满足
- 主 Agent 路径没有接入 Validation
- Fall 2026 部分数据被当作完整学期
- 本地已有 `courses.csv`，课程详情仍依赖外部 API
- 没有 pytest、CI 和浏览器回归测试
- 前端集中在一个超过 7,500 行的 HTML 文件中
- 当前工作区约有 5,000 行以上未提交修改，需要先建立安全基线

## 2. 本轮目标与非目标

### 本轮目标

完成以下结果后，项目进入“Private Beta Candidate”：

1. Agent loop 成为唯一聊天主链路。
2. Session、Memory 和 Schedule 各自只有一个真相源。
3. 课程、先修、冲突和学期覆盖状态可以被确定性验证。
4. 核心流程有离线自动化测试和 CI。
5. 前端完成模块拆分，但不进行框架重写。
6. 新环境可以按最终 README 一次启动。

### 暂不开发

- 协同过滤
- 教授联网搜索
- 多学期 AI 规划
- 全部 87 个专业的 Degree Audit
- Waitlist 实时提醒
- React/Vue/Svelte 全量重写
- PostgreSQL 迁移
- 新导航页面、主题和动画扩展

这些功能等私测稳定、产生真实用户反馈后再评估。

## 3. 执行总览

| 阶段 | 目标 | 预计工作量 | 前置依赖 |
|---|---|---:|---|
| M0 | 冻结当前基线 | 0.5–1 天 | 无 |
| M1 | 建立自动化安全网 | 2–4 天 | M0 |
| M2 | 统一 Session 与 Memory | 3–5 天 | M1 |
| M3 | 建立可靠的课表约束层 | 3–5 天 | M2 |
| M4 | 修正数据、先修与 Validation | 4–7 天 | M1、M3 |
| M5 | Agent 主链切换与旧代码清理 | 3–5 天 | M2–M4 |
| M6 | 前端收敛与性能优化 | 4–7 天 | M5 |
| M7 | 私测安全与工程化 | 3–5 天 | M1–M6 |
| M8 | Roadmap 收尾与 README | 1 天 | M7 |

工作量按 1 名开发者估算，不是发布日期承诺。

## 4. M0 — 冻结当前基线

目标：先保护当前跨 Agent、政策和 UI 的大规模修改，避免清理过程中丢失有效工作。

### 任务

- [x] 将运行生成的数据与源码改动分开：用户 memory、turn log、auth DB、grade cache、教授 summary。
- [x] 把可复用 demo fixture 移到明确的 `tests/fixtures/` 或 `data/demo/`。
- [x] 检查 `.gitignore`，确保真实用户运行数据不会进入提交。
- [x] 保存当前代码 checkpoint，不在同一个提交中混入后续重构。
- [x] 记录当前可演示流程：注册、onboarding、发送消息、生成卡片、加课、继续生成、恢复会话。
- [x] 记录当前已知失败场景，作为 M1 characterization tests 的输入。

### 验收

- [x] `git status` 中不再混有真实用户运行数据。
- [x] 当前功能有可回退的代码 checkpoint。
- [x] 后续每个重构阶段可以独立提交和回滚。

基线记录见 [`docs/m0-baseline.md`](docs/m0-baseline.md)。

## 5. M1 — 建立自动化安全网

目标：在删除旧代码前固定当前正确行为，并让错误路径可以重复验证。

### M1.1 测试基础设施

- [x] 引入 `pytest`、FastAPI TestClient 和覆盖率配置。
- [x] 测试默认禁止外网访问和真实 LLM 调用。
- [x] 建立 fake LLM client，支持 token、tool call、error 和 limit reached 事件。
- [x] 建立最小课程、section、profile、memory 和 session fixture。
- [x] 修复 smoke 脚本必须手动设置 `PYTHONPATH=.` 才能运行的问题。
- [x] 将 live API / live LLM 测试标记为手动测试，不进入默认 CI。

### M1.2 第一批 Characterization Tests

- [x] 新会话第一轮到第二轮状态连续。
- [x] 已登录用户只能读取自己的 session 和 memory。
- [x] Profile 可以加载到聊天上下文。
- [x] 记忆新增、删除、进程重启后结果一致。
- [x] Agent tool call、错误降级、limit reached 和 continue 协议稳定。
- [x] 推荐卡片能够通过 SSE meta 持久化并在历史会话中恢复。
- [x] Schedule add/remove/clear 的当前行为被固定。
- [x] Spring 2025 离线 smoke 校验能够识别不存在的课程与教师。

### 验收

- [x] 一条命令可以执行全部离线测试。
- [x] 测试不需要 API key、不调用 Anteater、不调用真实 LLM。
- [x] 核心测试失败时返回非零退出状态。
- [x] 后续 M2–M6 的每个 bug 修复先增加失败测试，再修改实现。

## 6. M2 — 统一 Session 与 Memory

目标：消除最容易导致用户数据丢失和状态漂移的重复存储。

### M2.1 修复立即存在的状态错误

- [x] 修复 `load_student_into_session()` 对 `get_student_profile()` envelope 的错误读取。
- [x] 新会话创建 `sess_xxx` 后，当前请求立即切换到该 ID；禁止使用空字符串保存第一轮状态。
- [x] `ChatRequest.student_id` 不再参与身份判断，身份只来自签名 Cookie。
- [x] Pydantic list/dict 默认值改用 `Field(default_factory=...)`。

### M2.2 选择唯一 Session 真相源

- [x] 以 `app/data/sessions.py` 为持久化 Session repository。
- [x] Session state 增加 term、当前规划偏好和 pending schedule。
- [x] 聊天历史只保存在 `sessions/{session_id}/turns.jsonl`。
- [x] 移除 Memory 中重复的完整 `turn_log.jsonl` 和 session-end history snapshot。
- [x] 完成数据迁移后删除 `app/modules/state.py::_sessions`。
- [x] 删除 `demo_session` 到 persistent session 的临时映射逻辑。

### M2.3 统一 Memory schema

- [x] Profile、facts、preferences 只通过 MemoryRepository 读写。
- [x] Router 不再直接读写 JSON 文件，避免绕过 provider cache。
- [x] `facts.json` 统一为一个确定的数据结构，不再同时按 list 和 dict 解释。
- [x] preference 统一为 `{id, text, learned_at, last_confirmed_at}`。
- [x] `add_preference()` 不再写入裸字符串。
- [x] 修复 preference 单条删除和 size-limit 计算。
- [x] 用户新陈述与旧偏好冲突时，以新陈述为准并更新时间。

### 验收

- 新会话第一轮保存的信息在第二轮仍然存在。
- 服务重启后 term、profile、history 和 pending schedule 不丢失。
- 删除 preference 后重启服务不会重新出现。
- 同一轮对话不再保存三份完整副本。

## 7. M3 — 建立可靠的课表约束层

目标：推荐卡片和课表使用同一套确定性约束逻辑。

### M3.1 合并重复实现

- [ ] 以 `app/modules/conflict.py` 或新建 `app/scheduling/` 作为唯一 scheduling domain service。
- [ ] Agent tool、推荐卡片和 Schedule API 全部调用同一个日期/时间解析器。
- [ ] 删除 `app/agent/tools.py` 中重复的 `_parse_days/_parse_time/_section_overlap`。
- [ ] 删除 `chat.py` 中重复的 `_parse_days`。
- [ ] 删除 `modules/query.py` 中永远返回“无冲突”的 placeholder。
- [ ] 统一 Mon–Sun 编码，明确 TBA 的 unknown 状态。

### M3.2 实现 Schedule Bundle Validation

- [ ] 新增 `validate_schedule_bundle()`。
- [ ] 检查推荐课程之间的上课时间冲突。
- [ ] 检查推荐课程与 pending schedule 的冲突。
- [ ] 检查 final exam 冲突。
- [ ] 检查 Lec/Dis/Lab pairing 是否完整。
- [ ] 检查 cancelled、FULL、waitlist 和 section restriction。
- [ ] 返回结构化结果：`valid / warnings / conflicts / unknowns`。
- [ ] `propose_recommendation` 和 `/schedule/add` 同时调用该服务。

### M3.3 持久化与权限

- [ ] Schedule API 使用当前认证用户验证 session ownership。
- [ ] pending schedule 写入持久化 Session。
- [ ] Add 操作遇到硬冲突时要求确认，不静默加入。
- [ ] Lec/Dis/Lab 不完整时不允许展示为可执行完整课表。

### 验收

- 同一组输入在 Agent 推荐和手动加课时得到相同冲突结果。
- 重启和切换会话后课表保持一致。
- 无法通过猜测 session ID 修改其他用户课表。
- TBA 不被误报为“无冲突”，而是明确显示 unknown。

## 8. M4 — 修正数据、先修与 Validation

目标：让“有数据”“没开课”和“数据不完整”成为不同状态。

### M4.1 Catalog 本地化

- [ ] `UCIRelationalLoader` 加载 `courses.csv`。
- [ ] CourseRecord 填充 title、units、description、level、restriction 和 prerequisite tree。
- [ ] `get_course_info()` 优先返回本地课程元数据，Anteater 只作为 fallback。
- [ ] 修复 loader 跳过坏 section 后 `zip(section_rows, sections)` 可能错位的问题。
- [ ] `search_courses()` 使用稳定排序，不再从 set 产生不确定顺序。
- [ ] 对常用课程信息增加进程内 cache 和批量 enrich 接口。

### M4.2 Prerequisite Engine

- [ ] 使用现有 `prerequisite_tree_json`，不再使用 flat list 直接判断。
- [ ] 支持 AND、OR、corequisite 和 minimum grade。
- [ ] 明确当前学期 in-progress 是否可以满足目标学期先修。
- [ ] 返回 `met / not_met / unknown` 三态。
- [ ] 查询失败时不再默认 `prereq_met=True`。
- [ ] 推荐卡片显示具体缺失分支和 unknown 原因。

### M4.3 数据覆盖和新鲜度

- [ ] 为每次数据构建生成 manifest：term、记录数、部门覆盖、来源、更新时间、schema version。
- [ ] 学期状态统一为 `complete / partial / stale / unavailable`。
- [ ] 将当前 Fall 2026 标记为 partial，禁止将 452 条数据视为完整学期。
- [ ] `/api/terms` 返回 coverage status，不只返回名称。
- [ ] 前端 term selector 显示 partial/stale 标识。
- [ ] API 失败时区分“没有开课”和“无法确认”。

### M4.4 主 Agent Validation

- [ ] Agent 最终文本和结构化 cards 都进入 Validation。
- [ ] 删除使用其他学期 catalog 验证当前学期回答的 fallback。
- [ ] 验证课程、section code、教师、时间、seat、政策日期和 tool provenance。
- [ ] 错误卡片禁止进入 Schedule。
- [ ] 实现明确的 KEEP、ANNOTATE、REMOVE、BLOCK 行为。
- [ ] Validation 输出写入 session turn，历史恢复时可重现。

### 验收

- `MATH 3A or MATH H3A` 完成其中一门即可通过先修。
- 先修数据缺失时显示 unknown，不显示已满足。
- partial 学期的空结果不会被回答为“确定不开课”。
- 所有 section code、时间、教师和政策日期都能回溯到 tool result 和数据版本。

## 9. M5 — Agent 主链切换与旧代码清理

目标：只保留一套聊天业务逻辑，缩小维护面。

### M5.1 唯一聊天入口

- [ ] `/api/chat/stream` 成为唯一产品聊天入口。
- [ ] Agent pre-flight 失败时返回确定性的 grounded fallback，不再启动第二套 LLM 推荐流程。
- [ ] 确认无外部客户端依赖后，弃用并删除非流式 `/api/chat`。
- [ ] Intent 结果不再控制主链路，只保留必要的确定性 decision detection。
- [ ] 删除 streaming 前串行执行的非必要 intent LLM 调用。
- [ ] 将 hard-fact extraction 合并进 Agent/tool，或移动到回答后的后台任务。

### M5.2 删除 Legacy Recommendation

- [ ] 删除 `_handle_recommendation()`。
- [ ] 删除 `_handle_single_query()`，保留一个最小确定性单课程降级查询即可。
- [ ] 删除 legacy `_build_card()`。
- [ ] 删除 `modules/query.py` 的旧推荐和排序流程。
- [ ] 删除 `modules/answer.py` 的模板回答。
- [ ] 删除未使用的 `query_professor()` 和 `generate_professor_answer()`。
- [ ] 评估 structured followup chips 是否仍需要；需要则基于 Agent/cards 重写，否则删除 `modules/followup.py`。
- [ ] `clarification.py` 只保留仍被事实提取使用的部分，或整体迁移后删除。

### M5.3 统一课程号解析

- [ ] 修复 `catalog/normalization.py` 的中文相邻字符边界。
- [ ] 所有课程 ID 提取改用同一 canonical parser。
- [ ] 删除 chat.py 和 decision detector 中重复的课程号正则/normalize 实现。
- [ ] 增加 `我想选CS122A这门课`、`ICS 33`、`I&C SCI 33`、`SOC SCI 178C` 测试。

### 验收

- 聊天请求不再进入 legacy query/answer 路径。
- 代码中只保留一个课程推荐实现和一个卡片 enrich 实现。
- LLM 不可用时仍返回明确、真实、非幻觉的降级响应。
- 每轮正常请求不再先执行 intent LLM + extraction LLM + Agent LLM 三次串行调用。

## 10. M6 — 前端收敛与性能优化

目标：降低 7,500 行单文件带来的回归风险，不进行框架迁移。

### M6.1 立即清理

- [ ] 删除无调用方的 `appendTyping()`。
- [ ] 删除无调用方的 `appendAI()`。
- [ ] 抽取统一 `consumeSSE(response, handlers)`，供 send 和 continue 共用。
- [ ] 删除已经被后端 `/api/terms` 替代的静态 term 假数据 fallback；API 不可用时显示 unavailable。
- [ ] JavaScript 颜色从 CSS custom properties 读取，不再维护第二份 hardcoded palette。

### M6.2 模块拆分

- [ ] `styles/tokens.css`
- [ ] `styles/components.css`
- [ ] `js/api-client.js`
- [ ] `js/chat.js`
- [ ] `js/cards.js`
- [ ] `js/schedule.js`
- [ ] `js/sessions.js`
- [ ] `js/auth.js`
- [ ] `js/profile-memory.js`
- [ ] `js/onboarding.js`
- [ ] 保留原生 HTML/CSS/JS，不在本阶段引入 React/Vue/Svelte。

### M6.3 浏览器回归

- [ ] 覆盖登录、onboarding、发送消息、tool chip、卡片、加课、继续生成和会话恢复。
- [ ] 增加移动端、键盘焦点、对比度和基础 screen-reader 检查。
- [ ] 历史消息与实时消息共用同一个 card/followup/validation renderer。

### 验收

- 业务 JavaScript 不再集中在单个 HTML 文件。
- send/continue 不再重复解析 SSE。
- 登录到生成可执行课表的核心路径有浏览器自动化覆盖。
- 拆分前后主要界面截图无非预期差异。

## 11. M7 — 私测安全与工程化

目标：达到可以邀请真实用户测试的最低安全和运维标准。

### M7.1 安全

- [ ] 公开环境禁用共享可写的 `demo_001`；Guest 使用隔离、可过期的临时身份。
- [ ] Schedule、Memory、Session 写接口统一验证用户身份和 ownership。
- [ ] 登录、验证码和 LLM 请求增加 rate limit。
- [ ] 生产 Cookie 开启 `Secure`，增加 Origin/CSRF 防护。
- [ ] 生产密钥必须来自环境变量，禁止自动生成本地 fallback。
- [ ] 自定义 system prompt 和 `/api/system_prompt` 仅在开发模式或管理员模式开放。
- [ ] 日志禁止记录密码、验证码、Cookie 和不必要的完整个人资料。

### M7.2 依赖与 CI

- [ ] 补全运行依赖：FastAPI、LLM client、requests、bcrypt、itsdangerous 等。
- [ ] 单独声明开发/数据脚本依赖：pytest、python-dotenv、openpyxl、beautifulsoup4 等。
- [ ] 固定 Python 版本和启动命令。
- [ ] GitHub Actions 执行安装、lint、离线测试和最小构建验证。
- [ ] 增加 `.env.example`，只列变量名和说明，不包含真实密钥。

### M7.3 可观测性

- [ ] 增加 `/health/live` 和 `/health/ready`。
- [ ] 每个请求生成 trace ID，串联路由、Agent、tools、data source 和 SSE 结束状态。
- [ ] 记录首 token 延迟、总延迟、tool failure、limit reached、LLM token 和成本。
- [ ] 数据 partial/stale、刷新失败和外部 API 限流产生明确日志与告警。

### 验收

- 核心 CI 在干净环境通过。
- 无有效身份无法修改其他用户数据。
- 任意私测报错可以通过 trace ID 定位到具体 tool 和数据版本。
- 高风险安全问题为 0 后才开放真实用户私测。

## 12. M8 — 文档收尾

文档必须描述已经落地的实现，不能提前承诺尚未完成的架构。

### M8.1 更新本 Roadmap

- [ ] 将已完成任务打勾。
- [ ] 为每个阶段记录完成日期、commit/PR 和验收结果。
- [ ] 将私测反馈转换成下一阶段计划。
- [ ] 再决定多学期规划、Degree Audit、提醒和个性化的优先级。

### M8.2 README 最后更新

- [ ] 删除 “Initial Demo”、mock data 和 placeholder 等过时说明。
- [ ] 更新真实架构图和目录结构。
- [ ] 写清 Python 版本、安装、环境变量、数据准备、启动和测试命令。
- [ ] 区分离线模式、外部 API fallback 和 live LLM 模式。
- [ ] 说明支持的学期、专业范围和正确性边界。
- [ ] 说明项目不是 UCI 官方 advisor，不能替代正式学业审核。

### 验收

- 新开发者只按 README 可以在干净环境启动应用并运行离线测试。
- README、代码、应用版本和 Roadmap 状态一致。

## 13. 跨阶段 Definition of Done

每个任务只有同时满足以下条件才算完成：

- 有自动化测试覆盖成功、失败和 unknown 路径。
- 不通过 silent fallback 掩盖数据缺失或实现错误。
- 用户可见事实携带 term、source 和 updated_at。
- 数据或 API schema 变化有迁移与兼容说明。
- 不引入新的重复状态源、重复正则或重复业务链路。
- 不提交真实用户数据、密钥、验证码或 Cookie。
- 代码、测试和验收证据在同一个 PR/commit 范围内可审查。

## 14. 建议提交顺序

为了降低当前大工作区的审查风险，建议拆成以下提交：

1. `chore: isolate runtime data and checkpoint current card work`
2. `test: add offline fixtures and characterize core flows`
3. `fix: unify session identity and profile hydration`
4. `fix: normalize memory schema and repository writes`
5. `feat: persist schedule and validate schedule bundles`
6. `feat: load local course metadata and evaluate prerequisite trees`
7. `feat: expose data coverage and validate agent output`
8. `refactor: remove legacy chat and duplicate domain logic`
9. `refactor: split frontend modules and share SSE handling`
10. `chore: add CI security defaults and observability`
11. `docs: update roadmap status`
12. `docs: rewrite README for the stabilized architecture`

## 15. 进度记录

| 日期 | 阶段 | 变更 | Commit/PR | 验收结果 |
|---|---|---|---|---|
| _待填写_ |  |  |  |  |
