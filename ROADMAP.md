# UCI Course Advisor Roadmap

> 更新日期：2026-07-22
>
> 当前目标：M13 已完成；进入私测观察与后续阶段需求评估。
>
> 执行规则：严格按阶段推进。每一阶段通过验收后，再进入下一阶段；README 在全部工程调整完成后最后更新。

## 1. 当前定位

项目已经完成 M0-M13 的主要产品、数据验证、联网搜索、WebSoc workflow、统一学期上下文与跨学期 Schedule。

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
- [x] Controlled web search、agentic deep search 和 run 内 URL 去重
- [x] Anteater live availability 与 WebSoc restriction 固定 workflow
- [x] Developer-authored workflow registry 和公共 deep-search trace history

### 本轮完成

- 后端 `TermResolutionService` 统一 automatic、conversation、query 与 tool term。
- Anteater calendar/WebSoc 发布门禁、30/45 天缓存和显式 fallback 已落地。
- conversation 支持可持久化的 `auto/pinned` 语义与幂等旧数据迁移。
- 前端 term selector 已替换为只读 conversation term。
- Schedule entry 自带 canonical term，支持跨 term 展示、去重、删除和非阻断 overlap。
- M13 默认离线回归与手工真实 Anteater smoke 均有可重复验收路径。

## 2. 本轮目标与非目标

### 本轮目标

完成以下结果后，系统不再依赖用户手工维护全局学期：

1. 后端根据 UCI calendar、Week 2 Friday 和 Anteater WebSoc 发布状态确定 automatic term。
2. conversation 使用独立、可持久化的 `auto/pinned` term memory。
3. chat、workflow、agentic、catalog、validation 和 schedule 使用同一个 effective term。
4. 前端删除全局 selector，只显示 conversation 对应的只读 canonical term。
5. Schedule entry 保存自己的 term，并允许所有时间 overlap 非阻断加入。
6. 同步、缓存、stale 和 fallback 行为有离线自动化测试及可观测性。

### 暂不开发

- 协同过滤
- 教授联网搜索
- 自动生成完整多学期 Degree Plan
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
| M9 | 联网搜索与证据链 | 4–7 天 | M4、M5、M7 |
| M10 | Live WebSoc 与专业限制 Workflow | 已完成 | M9 |
| M11 | Agentic Deep Search 与 Workflow Registry | 已完成 | M9、M10 |
| M12 | WebSoc Restriction Workflow 可靠性 | 已完成 | M10、M11 |
| M13 | 自动学期上下文与跨学期 Schedule | 已完成 | M2–M7、M10–M12 |

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

- [x] 新会话第一轮保存的信息在第二轮仍然存在。
- [x] 服务重启后 term、profile、history 和 pending schedule 不丢失。
- [x] 删除 preference 后重启服务不会重新出现。
- [x] 同一轮对话不再保存三份完整副本。

## 7. M3 — 建立可靠的课表约束层

目标：推荐卡片和课表使用同一套确定性约束逻辑。

### M3.1 合并重复实现

- [x] 以 `app/modules/conflict.py` 或新建 `app/scheduling/` 作为唯一 scheduling domain service。
- [x] Agent tool、推荐卡片和 Schedule API 全部调用同一个日期/时间解析器。
- [x] 删除 `app/agent/tools.py` 中重复的 `_parse_days/_parse_time/_section_overlap`。
- [x] 删除 `chat.py` 中重复的 `_parse_days`。
- [x] 删除 `modules/query.py` 中永远返回“无冲突”的 placeholder。
- [x] 统一 Mon–Sun 编码，明确 TBA 的 unknown 状态。

### M3.2 实现 Schedule Bundle Validation

- [x] 新增 `validate_schedule_bundle()`。
- [x] 检查推荐课程之间的上课时间冲突。
- [x] 检查推荐课程与 pending schedule 的冲突。
- [x] 检查 final exam 冲突。
- [x] 检查 Lec/Dis/Lab pairing 是否完整。
- [x] 检查 cancelled、FULL、waitlist 和 section restriction。
- [x] 返回结构化结果：`valid / warnings / conflicts / unknowns`。
- [x] `propose_recommendation` 和 `/schedule/add` 同时调用该服务。

### M3.3 持久化与权限

- [x] Schedule API 使用当前认证用户验证 session ownership。
- [x] pending schedule 写入持久化 Session。
- [x] Add 操作遇到硬冲突时要求确认，不静默加入。
- [x] Lec/Dis/Lab 不完整时不允许展示为可执行完整课表。

### 验收

- [x] 同一组输入在 Agent 推荐和手动加课时得到相同冲突结果。
- [x] 重启和切换会话后课表保持一致。
- [x] 无法通过猜测 session ID 修改其他用户课表。
- [x] TBA 不被误报为“无冲突”，而是明确显示 unknown。

## 8. M4 — 修正数据、先修与 Validation

目标：让“有数据”“没开课”和“数据不完整”成为不同状态。

### M4.1 Catalog 本地化

- [x] `UCIRelationalLoader` 加载 `courses.csv`。
- [x] CourseRecord 填充 title、units、description、level、restriction 和 prerequisite tree。
- [x] `get_course_info()` 优先返回本地课程元数据，Anteater 只作为 fallback。
- [x] 修复 loader 跳过坏 section 后 `zip(section_rows, sections)` 可能错位的问题。
- [x] `search_courses()` 使用稳定排序，不再从 set 产生不确定顺序。
- [x] 对常用课程信息增加进程内 cache 和批量 enrich 接口。

### M4.2 Prerequisite Engine

- [x] 使用现有 `prerequisite_tree_json`，不再使用 flat list 直接判断。
- [x] 支持 AND、OR、corequisite 和 minimum grade。
- [x] 明确当前学期 in-progress 是否可以满足目标学期先修。
- [x] 返回 `met / not_met / unknown` 三态。
- [x] 查询失败时不再默认 `prereq_met=True`。
- [x] 推荐卡片显示具体缺失分支和 unknown 原因。

### M4.3 数据覆盖和新鲜度

- [x] 为每次数据构建生成 manifest：term、记录数、部门覆盖、来源、更新时间、schema version。
- [x] 学期状态统一为 `complete / partial / stale / unavailable`。
- [x] 将当前 Fall 2026 标记为 partial，禁止将 452 条数据视为完整学期。
- [x] `/api/terms` 返回 coverage status，不只返回名称。（历史实现；M13 收尾后旧 selector 接口已移除。）
- [x] 前端 term selector 显示 partial/stale 标识。
- [x] API 失败时区分“没有开课”和“无法确认”。

### M4.4 主 Agent Validation

- [x] Agent 最终文本和结构化 cards 都进入 Validation。
- [x] 删除使用其他学期 catalog 验证当前学期回答的 fallback。
- [x] 验证课程、section code、教师、时间、seat、政策日期和 tool provenance。
- [x] 错误卡片禁止进入 Schedule。
- [x] 实现明确的 KEEP、ANNOTATE、REMOVE、BLOCK 行为。
- [x] Validation 输出写入 session turn，历史恢复时可重现。

### 验收

- `MATH 3A or MATH H3A` 完成其中一门即可通过先修。
- 先修数据缺失时显示 unknown，不显示已满足。
- partial 学期的空结果不会被回答为“确定不开课”。
- 所有 section code、时间、教师和政策日期都能回溯到 tool result 和数据版本。

## 9. M5 — Agent 主链切换与旧代码清理

目标：只保留一套聊天业务逻辑，缩小维护面。

### M5.1 唯一聊天入口

- [x] `/api/chat/stream` 成为唯一产品聊天入口。
- [x] Agent pre-flight 失败时返回确定性的 grounded fallback，不再启动第二套 LLM 推荐流程。
- [x] 确认无外部客户端依赖后，弃用并删除非流式 `/api/chat`。
- [x] Intent 结果不再控制主链路，只保留必要的确定性 decision detection。
- [x] 删除 streaming 前串行执行的非必要 intent LLM 调用。
- [x] 将 hard-fact extraction 合并进 Agent/tool，或移动到回答后的后台任务。

### M5.2 删除 Legacy Recommendation

- [x] 删除 `_handle_recommendation()`。
- [x] 删除 `_handle_single_query()`，保留一个最小确定性单课程降级查询即可。
- [x] 删除 legacy `_build_card()`。
- [x] 删除 `modules/query.py` 的旧推荐和排序流程。
- [x] 删除 `modules/answer.py` 的模板回答。
- [x] 删除未使用的 `query_professor()` 和 `generate_professor_answer()`。
- [x] 评估 structured followup chips 是否仍需要；需要则基于 Agent/cards 重写，否则删除 `modules/followup.py`。
- [x] `clarification.py` 只保留仍被事实提取使用的部分，或整体迁移后删除。

### M5.3 统一课程号解析

- [x] 修复 `catalog/normalization.py` 的中文相邻字符边界。
- [x] 所有课程 ID 提取改用同一 canonical parser。
- [x] 删除 chat.py 和 decision detector 中重复的课程号正则/normalize 实现。
- [x] 增加 `我想选CS122A这门课`、`ICS 33`、`I&C SCI 33`、`SOC SCI 178C` 测试。

### 验收

- 聊天请求不再进入 legacy query/answer 路径。
- 代码中只保留一个课程推荐实现和一个卡片 enrich 实现。
- LLM 不可用时仍返回明确、真实、非幻觉的降级响应。
- 每轮正常请求不再先执行 intent LLM + extraction LLM + Agent LLM 三次串行调用。

## 10. M6 — 前端收敛与性能优化

目标：降低 7,500 行单文件带来的回归风险，不进行框架迁移。

### M6.1 立即清理

- [x] 删除无调用方的 `appendTyping()`。
- [x] 删除无调用方的 `appendAI()`。
- [x] 抽取统一 `consumeSSE(response, handlers)`，供 send 和 continue 共用。
- [x] 删除曾由后端 `/api/terms` 替代的静态 term 假数据 fallback；M13 后改为最小 `/api/term-state`，API 不可用时显示 unavailable。
- [x] JavaScript 颜色从 CSS custom properties 读取，不再维护第二份 hardcoded palette。

### M6.2 模块拆分

- [x] `styles/tokens.css`
- [x] `styles/components.css`
- [x] `js/api-client.js`
- [x] `js/chat.js`
- [x] `js/cards.js`
- [x] `js/schedule.js`
- [x] `js/sessions.js`
- [x] `js/auth.js`
- [x] `js/profile-memory.js`
- [x] `js/onboarding.js`
- [x] 保留原生 HTML/CSS/JS，不在本阶段引入 React/Vue/Svelte。

### M6.3 浏览器回归

- [x] 覆盖登录、onboarding、发送消息、tool chip、卡片、加课、继续生成和会话恢复。
- [x] 增加移动端、键盘焦点、对比度和基础 screen-reader 检查。
- [x] 历史消息与实时消息共用同一个 card/followup/validation renderer。

### 验收

- 业务 JavaScript 不再集中在单个 HTML 文件。
- send/continue 不再重复解析 SSE。
- 登录到生成可执行课表的核心路径有浏览器自动化覆盖。
- 拆分前后主要界面截图无非预期差异。

## 11. M7 — 私测安全与工程化

目标：达到可以邀请真实用户测试的最低安全和运维标准。

### M7.1 安全

- [x] 公开环境禁用共享可写的 `demo_001`；Guest 使用隔离、可过期的临时身份。
- [x] Schedule、Memory、Session 写接口统一验证用户身份和 ownership。
- [x] 登录、验证码和 LLM 请求增加 rate limit。
- [x] 生产 Cookie 开启 `Secure`，增加 Origin/CSRF 防护。
- [x] 生产密钥必须来自环境变量，禁止自动生成本地 fallback。
- [x] 自定义 system prompt 和 `/api/system_prompt` 仅在开发模式或管理员模式开放。
- [x] 日志禁止记录密码、验证码、Cookie 和不必要的完整个人资料。

### M7.2 依赖与 CI

- [x] 补全运行依赖：FastAPI、LLM client、requests、bcrypt、itsdangerous 等。
- [x] 单独声明开发/数据脚本依赖：pytest、python-dotenv、openpyxl、beautifulsoup4 等。
- [x] 固定 Python 版本和启动命令。
- [x] GitHub Actions 执行安装、lint、离线测试和最小构建验证。
- [x] 增加 `.env.example`，只列变量名和说明，不包含真实密钥。

### M7.3 可观测性

- [x] 增加 `/health/live` 和 `/health/ready`。
- [x] 每个请求生成 trace ID，串联路由、Agent、tools、data source 和 SSE 结束状态。
- [x] 记录首 token 延迟、总延迟、tool failure、limit reached、LLM token 和成本。
- [x] 数据 partial/stale、刷新失败和外部 API 限流产生明确日志与告警。

### 验收

- 核心 CI 在干净环境通过。
- 无有效身份无法修改其他用户数据。
- 任意私测报错可以通过 trace ID 定位到具体 tool 和数据版本。
- 高风险安全问题为 0 后才开放真实用户私测。

## 12. M8 — 文档收尾

文档必须描述已经落地的实现，不能提前承诺尚未完成的架构。

### M8.1 更新本 Roadmap

- [x] 将已完成任务打勾。
- [x] 为每个阶段记录完成日期、commit/PR 和验收结果。
- [x] 将私测反馈转换成下一阶段计划。
- [x] 再决定多学期规划、Degree Audit、提醒和个性化的优先级。

### M8.2 README 最后更新

- [x] 删除 “Initial Demo”、mock data 和 placeholder 等过时说明。
- [x] 更新真实架构图和目录结构。
- [x] 写清 Python 版本、安装、环境变量、数据准备、启动和测试命令。
- [x] 区分离线模式、外部 API fallback 和 live LLM 模式。
- [x] 说明支持的学期、专业范围和正确性边界。
- [x] 说明项目不是 UCI 官方 advisor，不能替代正式学业审核。

### 私测反馈转下一阶段计划

当前仓库内没有已记录的真实私测用户反馈；因此下一阶段先把 M7 暴露出的私测风险转成反馈闭环，而不是直接扩功能：

1. P0 — 私测运行闭环：收集 trace ID、失败截图、用户问题、term、coverage status 和 validation action；每个高风险反馈必须归类为数据缺失、工具错误、LLM 行为、前端交互或认证/权限问题。
2. P0 — 正确性修复：优先处理会导致错误课程/错误 section/错误可加课状态的反馈。
3. P1 — 多学期规划：在现有 session state、term coverage 和 schedule validation 稳定后再扩展跨 term plan。
4. P2 — Degree Audit：只有在 major requirement 数据结构、官方来源和 unknown 语义稳定后启动；不能把当前 CS/GE helper 伪装成完整 audit。
5. P3 — 提醒和个性化：在认证、隐私、通知偏好和数据保留策略明确后再做。

### 验收

- 新开发者只按 README 可以在干净环境启动应用并运行离线测试。
- README、代码、应用版本和 Roadmap 状态一致。

## 13. M9 — 联网搜索与证据链

目标：当本地数据库没有信息、数据不完整、或用户明确要求联网时，Agent 可以调用受控搜索工具；但所有联网信息必须和本地数据库信息分层展示，不能让用户误以为外部网页等同于数据库验证。

本阶段不做 Future Planning，不扩 Degree Audit。重点只解决问答式体验中的“数据库没有覆盖的问题怎么回答”和“用户如何知道信息来源”。

### M9.1 搜索触发规则

- [x] 新增 Search Skill / prompt block，明确什么时候允许调用搜索工具。
- [x] 允许搜索的情况：
  - 用户明确要求“上网查 / search / look up / 最新信息”。
  - 本地 DB tool 返回 `found=false`。
  - term coverage 是 `partial / stale / unavailable`。
  - 问题依赖近期变化：deadline、department restriction、department announcement、政策更新、教授页面。
  - 用户问的是本地数据库之外的信息，例如 department 网页、官方 announcement、外部教授评价。
- [x] 不应搜索的情况：
  - 本地 DB 已经确认事实，且 coverage 是 `complete`。
  - 普通推荐流程已有足够本地数据。
  - 只是为了让回答看起来更丰富。
  - 每轮默认搜索。
- [x] Agent 必须先使用本地 DB 工具；只有触发条件满足时才调用 web search。
- [x] 如果用户显式要求联网，则即使 DB 有结果，也可以搜索，但回答必须分别标注 DB 与 Web。

### M9.2 新增后端搜索工具

- [x] 新增 `app/data/web_search.py`。
- [x] 工具名使用 `web_search`，不使用 `web_search_official`，因为允许全网搜索。
- [x] 第一版搜索 API 只返回网页搜索结果，不做浏览器深度抓取。
- [x] 每次调用必须记录：
  - `query`
  - `reason`
  - `searched_at`
  - `max_results`
  - `preferred_domains`
  - `results`
- [x] 返回结果统一结构：

```json
{
  "ok": true,
  "query": "ART department major restriction release date UCI",
  "reason": "local policy data does not contain department-specific restriction release timing",
  "searched_at": "2026-07-05T00:00:00Z",
  "results": [
    {
      "title": "string",
      "url": "https://...",
      "domain": "uci.edu",
      "snippet": "string",
      "published_at": null,
      "source_class": "official_uci",
      "trust_level": "high",
      "why_trusted_or_not": "UCI official domain"
    }
  ]
}
```

- [x] 搜索失败时返回结构化错误，不抛出导致整轮 Agent 崩溃。
- [x] 搜索结果数量默认限制为 5，避免把大量不可靠网页塞进上下文。
- [x] 搜索请求加 rate limit 和 trace log。

### M9.3 来源分类与可信度规则

- [x] 新增 source classifier，将每个 URL 分类为：
  - `local_db_verified`
  - `official_uci`
  - `official_university`
  - `government`
  - `professor_page`
  - `rmp`
  - `reddit`
  - `commercial`
  - `news`
  - `unknown`
- [x] 可信度排序写入代码与 prompt：

```text
local_db_verified
> official_uci
> official_university / government
> professor_page
> external_web
> forum/social
> llm_inference
```

- [x] 默认 trust level：
  - `local_db_verified`: high
  - `official_uci`: high
  - `official_university`, `government`: medium_high
  - `professor_page`: medium
  - `rmp`: medium
  - `news`, `commercial`: medium_low
  - `reddit`, forum/social: low
  - `unknown`: low
- [x] 任何没有 URL 的 web 信息不能作为事实来源。
- [x] LLM 推断不能作为事实来源，只能作为解释或建议。

### M9.4 Agent Tool 接入

- [x] 在 `app/agent/tools.py` 增加 `web_search` tool schema。
- [x] 增加 `_tool_web_search()` dispatcher。
- [x] tool schema 参数：

```json
{
  "query": "string",
  "reason": "string",
  "preferred_domains": ["string"],
  "max_results": 5
}
```

- [x] `reason` 必填，迫使 LLM 说明为什么需要搜索。
- [x] 如果 `reason` 为空或明显无意义，后端可以拒绝或降级返回 warning。
- [x] tool result 只返回标题、URL、domain、snippet、source_class、trust_level，不返回大段网页全文。
- [x] SSE tool chip 显示“联网搜索 · query”。
- [x] observability 记录搜索次数、失败率、source_class 分布和 latency。

### M9.5 Search Skill / Prompt 硬规则

- [x] 在 Agent system prompt 中加入 Search Skill。
- [x] 规则必须明确：
  - 本地 DB 是默认最高可信来源。
  - Web 信息不能覆盖 `complete` 本地 DB，除非用户明确要求比较，并且回答必须说明冲突。
  - 如果本地 DB 是 `partial / stale / unavailable`，可以用 web 补充，但必须标为 web-sourced。
  - 如果 web 与 DB 冲突，必须同时展示双方来源和差异。
  - 不允许编 source、URL、rating、section、instructor。
  - 引用网页信息时必须附 markdown link。
  - Reddit/forum/social 只能作为 anecdotal，不可作为课程是否开设、政策 deadline、教授授课安排的事实依据。
- [x] 对教授评分增加规则：
  - 本地 professor DB/RMP snapshot 优先。
  - 外部 RMP 或其他评价网站必须标为 external。
  - 教授个人主页可用于研究方向，不可直接证明某学期授课。
- [x] 对开课信息增加规则：
  - 某 term 是否开课优先看 `get_sections(course, term)`。
  - coverage complete 且无 sections → 可以说本地确认未开。
  - coverage partial/stale/unavailable 且无 sections → 只能说本地无法确认，可联网查补充信息。

### M9.6 回答格式与引用

- [x] 如果使用 web result，回答中对应事实后必须带链接，例如：

```markdown
UCI Registrar 页面说明 add/drop deadline 由注册日历列出。[UCI Registrar](https://...)
```

- [x] 回答末尾增加可选 `Sources` 区块：

```markdown
Sources:
- Database verified: local catalog · Spring 2026 · coverage complete
- Web sourced: [UCI Registrar — Academic Calendar](https://...)
- External web: [RateMyProfessors — ...](https://...)
```

- [x] 本地 DB 信息不需要外链，但必须可显示 `Database verified`、term、coverage、updated_at。
- [x] Web 信息必须显示 `Web sourced`、domain、retrieved date。
- [x] 如果没有可靠来源，回答必须明确说“我无法验证”。

### M9.7 前端展示

- [x] 第一版先依赖 markdown link 正常渲染，不做复杂 evidence panel。
- [x] 在 tool chip 中区分：
  - 查询数据库
  - 联网搜索
  - 读取网页来源
- [x] 后续增加 source badge：
  - `DB Verified`
  - `Official UCI`
  - `Official Web`
  - `External Web`
  - `Anecdotal`
  - `Unverified`
- [x] 推荐卡片保留本地 provenance，不把 web source 混进 card 的 DB verified 字段。
- [x] 如果 card 里某个字段来自 web，字段旁单独显示 web badge。

### M9.8 DB 与 Web 冲突处理

- [x] 新增冲突表达规则：

```text
本地数据库显示：...
网页来源显示：...
判断：两者来源不同；本地 DB 用于结构化开课/section 判断，网页用于补充政策或公告。
```

- [x] complete DB 与 web 冲突：默认 DB 优先，web 作为冲突提示。
- [x] stale/partial DB 与 official web 冲突：可以引用 official web，但必须标为 web-sourced。
- [x] external web 与 DB 冲突：DB 优先，external 只能作为非官方补充。
- [x] 无法判断时返回 unknown，不做确定性结论。

### M9.9 测试计划

- [x] `web_search` tool schema 和 dispatcher 单元测试。
- [x] URL source classifier 测试：
  - `uci.edu` → `official_uci`
  - `catalogue.uci.edu` → `official_uci`
  - `reg.uci.edu` → `official_uci`
  - `ratemyprofessors.com` → `rmp`
  - `reddit.com` → `reddit`
  - 未知域名 → `unknown`
- [x] Agent 行为测试：
  - DB found + coverage complete → 不应调用 web search。
  - 用户明确要求上网 → 允许调用 web search。
  - DB missing → 允许调用 web search。
  - partial term → 允许调用 web search。
  - web result 没有 URL → 不能作为事实引用。
  - DB/Web 冲突 → 回答必须说明冲突。
- [x] 前端静态契约测试：
  - markdown link 可渲染。
  - web search tool chip 可显示。
  - source badge 不破坏现有 card。
- [x] 搜索 API 测试默认使用 fake provider，不联网，不依赖真实搜索服务。

### M9.10 配置与安全

- [x] 新增环境变量：
  - `WEB_SEARCH_ENABLED`
  - `WEB_SEARCH_PROVIDER`
  - `WEB_SEARCH_API_KEY`
  - `WEB_SEARCH_MAX_RESULTS`
  - `WEB_SEARCH_TIMEOUT_SECONDS`
- [x] 默认开发环境可以关闭 web search；关闭时 tool 返回明确 unavailable。
- [x] 生产环境必须通过环境变量显式开启。
- [x] 搜索 query 不记录完整敏感 profile；日志中截断 query 和 snippet。
- [x] 不把搜索结果自动写入本地数据库，避免污染 verified data。
- [x] 如需 cache，只 cache query/result/source/retrieved_at，并明确标为 web cache，不等于 DB。

### M9.11 最小可执行版本

第一版只要求完成：

1. `app/data/web_search.py` fake/provider interface。
2. `web_search` agent tool schema + dispatcher。
3. Search Skill prompt 规则。
4. 结果含 title/url/snippet/domain/source_class/trust_level。
5. 回答中带 markdown link。
6. fake provider 测试通过。
7. 不改变现有推荐卡片的数据可信度语义。

### 验收

- 用户明确要求联网时，Agent 可以调用 `web_search` 并在回答中附链接。
- 本地 DB 已确认的信息不会被默认 web search 覆盖。
- 所有 web 信息都显示来源 URL、domain、retrieved_at 和 trust_level。
- DB 与 Web 信息冲突时，回答明确区分双方来源。
- 搜索功能关闭时，系统不会崩溃，且会说明当前无法联网。
- 默认测试仍然不联网、不依赖真实搜索 API。

## 14. M10 — Live WebSoc 可用性与专业限制 Workflow

目标：把“课程是否还有位置 / OPEN-FULL-Waitl / waitlist / NOR / restriction code”与“专业限制什么时候解除”从通用 web search 中拆出来，做成固定、可测试、可追溯的 workflow。课程实时可用性按 AntAlmanac 的方式以 Anteater API WebSoc 数据为准；专业限制以 Registrar WebSoc department 页面为入口，并支持读取 WebSoc comments 中指向的官方部门链接。

本阶段不扩 Degree Audit，不改推荐算法目标。重点是让高风险、近期变化的问题不再依赖本地 CSV 或 agentic 搜索猜测。

状态：已在 `codex/m10-live-websoc-workflows` 分阶段实现并提交。默认测试使用 fixture/fake provider，不依赖真实网络。

### M10.1 问题分类与路由

- [x] 新增 search/workflow router，先判断用户问题是否属于固定 workflow，再决定是否交给 agentic `web_search`。
- [x] 定义 `availability` intent，覆盖：
  - 课程或 section 是否 `OPEN / FULL / Waitl`。
  - 当前 enrolled / capacity / seats open。
  - waitlist 当前人数和容量。
  - New Only Reserved / NOR 数量。
  - restriction code 当前值。
  - “现在能不能选这门课 / 还有位置吗 / waitlist 多长”。
- [x] 定义 `department_restriction` intent，覆盖：
  - major restriction 什么时候解除。
  - New Only Restrictions / NORS 什么时候解除。
  - 某学院或 department 的 add/drop/change 特殊规则。
  - “我不是这个 major，什么时候能选这门课”。
- [x] 固定 workflow 命中后默认不走 DuckDuckGo / 全网 agentic search。
- [x] 如果问题同时涉及 course availability 和 department restriction，先查 live WebSoc section，再查 department restriction workflow，并在回答中分开来源。

### M10.2 Live Availability 数据源规则

- [x] 将课程可用性问题的数据源规则改成：`live Anteater WebSoc > local cache/CSV fallback > unknown`。
- [x] 本地 CSV 不允许作为“当前还剩几个座位 / 现在 open 吗”的最终事实来源。
- [x] 当用户问普通 planning、历史 term、section 时间地点时，仍允许使用现有 DB-first `get_sections()` 逻辑。
- [x] 当用户问当前/未来 active term 的实时可用性时，必须优先调用 Anteater API WebSoc。
- [x] 如果 Anteater API 失败，可以返回本地数据作为非实时 fallback，但必须标注 `not_live` 和原因。
- [x] 如果 Anteater API 和本地 DB 冲突，availability/status/seat count 默认以 live Anteater WebSoc 为准，本地 DB 只作为对照。

### M10.3 Anteater API WebSoc 接口补强

- [x] 在 `app/data/anteater.py` 增加按 section code 查询 WebSoc 的能力，对齐 AntAlmanac/AANTS 的 `sectionCodes` 查询方式。
- [x] 保留按 `department + courseNumber + year + quarter` 查询课程 sections 的能力。
- [x] 增加 live availability 专用函数，例如 `fetch_live_sections()` 或 `fetch_sections(..., live=True)`。
- [x] live availability 返回时保留 Anteater 原始 `updatedAt` 字段。
- [x] 统一输出字段：
  - `status`
  - `max_capacity`
  - `enrolled`
  - `seats_open`
  - `waitlisted`
  - `waitlist_capacity`
  - `new_only_reserved`
  - `restrictions`
  - `updated_at`
  - `retrieved_at`
  - `source = live_anteater_websoc`
- [x] 对 `numCurrentlyEnrolled.totalEnrolled`、`maxCapacity`、`numOnWaitlist`、`numWaitlistCap`、`numNewOnlyReserved` 做安全整数解析。
- [x] `seats_open` 由 `max_capacity - enrolled` 计算；缺字段时返回 `null`，不猜。

### M10.4 5 分钟 Freshness 语义

- [x] 给 live Anteater WebSoc 查询增加 5 分钟 TTL cache，对齐 AntAlmanac 主页面的 freshness 语义。
- [x] cache key 至少包含 `year`、`quarter`、`department/courseNumber` 或 `sectionCodes`。
- [x] 用户明确说“最新 / 现在 / refresh / 重新查”时允许绕过 TTL cache。
- [x] tool result 中显示 `retrieved_at` 和 API 返回的 `updated_at`。
- [x] 回答必须说 “as of ...”，避免用户误以为 seat count 是永久事实。
- [x] 记录 cache hit/miss、API latency、non-200、rate limit 和 fallback 事件。

### M10.5 Agent Tool 接入

- [x] 新增 `get_live_sections` tool，或给现有 `get_sections` 增加 `freshness` / `mode` 参数。
- [x] 推荐方案：新增 `get_live_sections(course_id, term, section_codes?, force_refresh?)`，避免破坏已有 planning 行为。
- [x] `get_live_sections` 只回答 live availability，不用于泛化课程推荐。
- [x] `get_sections` 继续服务普通 course planning、时间冲突和 section 列表。
- [x] Agent prompt 增加硬规则：availability intent 必须调用 `get_live_sections`，不能只用本地 DB 或 web snippets。
- [x] SSE tool chip 区分：
  - `实时查询 WebSoc · COURSE TERM`
  - `查询排课 · COURSE TERM`
- [x] schedule/recommendation card 如果显示 live seat/status 字段，要标注 `live_anteater_websoc`，不能混进 DB verified provenance。

### M10.6 WebSoc Department Restriction Workflow

- [x] 新增固定 workflow：`websoc_department_restrictions`。
- [x] workflow 输入：
  - `term`
  - `department`
  - 可选 `course_id`
  - 可选 `restriction_type`，如 `major_restriction`、`nors`、`add_drop_change`
- [x] 如果用户只给 course_id，先通过本地 catalog 或 live WebSoc 解析出 department。
- [x] 如果 course_id 无法解析到 department，向用户要求 department；默认不改走 agentic search。
- [x] 固定访问 `https://www.reg.uci.edu/perl/WebSoc`，按 `YearTerm + Dept` 查询 department summary。
- [x] 请求参数中默认排除 cancelled courses，和 Registrar WebSoc 页面语义一致。
- [x] 不通过 DuckDuckGo 搜 WebSoc；URL 和参数由 workflow 构造。

### M10.7 WebSoc 页面解析

- [x] 解析 WebSoc 返回 HTML 顶部区域，而不是只解析 course sections。
- [x] 提取 `Search Criteria` 中的 department 和 cancelled-course 设置。
- [x] 提取 term registration end date，例如 “Registration for term ends on ...”。
- [x] 提取 school-level comments block，例如 `Claire Trevor School of the Arts comments`。
- [x] 提取 department-level comments block，例如 `Art department comments`。
- [x] 从 comments 中抽取常见字段：
  - add deadline
  - drop deadline
  - change grade option / variable units deadline
  - major restriction removal date/time
  - NORS removal date/time
  - waitlist policy
  - authorization-code policy
  - contact email
- [x] 不能抽取成结构化字段时，保留原始 comment text，并标注 `extraction_status = partial`。
- [x] 解析失败时返回 structured error，不让 agent 编日期。

### M10.8 WebSoc Comments 链接收集与深读

- [x] 收集 school comments 和 department comments 中出现的所有 `<a href>`。
- [x] 每个 link 记录：
  - `text`
  - `url`
  - `domain`
  - `source_block`
  - `source_url`
- [x] 只允许深读 WebSoc comments 中实际出现的链接。
- [x] 默认只深读 `*.uci.edu` 官方链接；非 UCI 链接只收集，不作为事实依据，除非后续显式允许。
- [x] 如果 WebSoc comments 已直接给出 major restriction 日期，不必深读链接。
- [x] 如果 WebSoc comments 说明 restriction details/update timeline 在链接中，必须继续 fetch 链接。
- [x] 深读链接时限制 timeout、content type、页面大小和重定向域名。
- [x] 链接正文转纯文本后，只返回必要片段和来源 URL，不把整页塞进 agent context。

### M10.9 ICS 特殊规则

- [x] 在 workflow registry 增加 ICS / I&C SCI 特例。
- [x] 对 ICS WebSoc comments 中的以下链接视为官方可深读来源：
  - `https://ics.uci.edu/course-enrollment-restrictions/`
  - `https://ics.uci.edu/academics/undergraduate-programs/majors-minors/undergraduate-student-policies/`
  - `https://ics.uci.edu/academics/graduate-academic-advising/course-updates/`
- [x] 对 undergraduate course restriction 问题，优先读取 `course-enrollment-restrictions` 或 WebSoc comments 中明确标为 Undergraduate courses 的链接。
- [x] 对 graduate course update 问题，优先读取 WebSoc comments 中明确标为 Graduate courses 的链接。
- [x] 对 general ICS policy 问题，允许读取 undergraduate student policies 链接。
- [x] 如果 ICS 链接页面与 WebSoc comments 冲突，回答必须同时列出：
  - `WebSoc comments 显示：...`
  - `ICS official page 显示：...`
  - `判断：WebSoc 是入口与本学期上下文，ICS 页面是 WebSoc 指向的官方补充。`

### M10.10 回答与引用规则

- [x] Availability 回答必须包含：
  - course/section
  - term
  - status
  - enrolled/capacity
  - seats_open
  - waitlist 信息，如存在
  - restrictions/NOR，如存在
  - `as of updated_at/retrieved_at`
  - source label `Live WebSoc via Anteater API`
- [x] Department restriction 回答必须包含：
  - term
  - department
  - WebSoc source URL
  - 如果用了 linked page，也包含 linked official URL
  - 哪些信息来自 WebSoc comments，哪些来自 linked page
- [x] 当 live data 不可用时，明确说“无法验证当前实时状态”，不能用旧 CSV 伪装成实时。
- [x] 当 WebSoc comments 没有提供 restriction timeline，回答应说“WebSoc 未列出具体解除时间”，并列出可验证来源。
- [x] 所有 web/link 来源都必须是 markdown link。

### M10.11 前端与 Tool Chip

- [x] tool chip 新增 `实时查询 WebSoc` 类型。
- [x] tool chip 新增 `读取 WebSoc 部门说明` 类型。
- [x] tool chip 新增 `读取官方部门链接` 类型。（当前深读官方链接合并在 `读取 WebSoc 部门说明` workflow 内返回，不单独发起第二个 tool event。）
- [x] 如果 source badge 已存在，增加或复用：
  - `Live WebSoc`
  - `WebSoc Comments`
  - `Official Department Link`
  - `Not Live`
- [x] 推荐卡片中的 seat/status 字段如果来自 live API，应显示 live badge 和更新时间。
- [x] card provenance 不把 live websoc 写成 local DB verified。

### M10.12 测试计划

- [x] `availability` intent 命中时必须调用 live Anteater WebSoc 路径。
- [x] 本地 DB 有旧 section 数据时，availability 仍优先 live API。
- [x] live API 返回 OPEN/FULL/Waitl 时，字段映射正确。
- [x] `numCurrentlyEnrolled.totalEnrolled`、waitlist、NOR、restriction codes 解析正确。
- [x] live API 缺字段时返回 `null`，不猜 seats。
- [x] 5 分钟 TTL cache hit/miss 测试。
- [x] `force_refresh` 绕过 cache 测试。
- [x] live API 429/non-200/network error 返回 structured fallback。
- [x] WebSoc department restriction workflow 构造固定 Registrar URL，不调用 DuckDuckGo。
- [x] ART fixture 能解析 school comments、department comments、major restriction date、NORS date 和 comments links。
- [x] ICS fixture 能解析 WebSoc comments 中的 undergraduate/graduate/policy links。
- [x] ICS workflow 能深读 comments 中出现的 `ics.uci.edu` 链接。
- [x] WebSoc comments 已给日期时，不强制深读链接。
- [x] WebSoc comments 指向链接但没有日期时，会深读链接并标注来源。
- [x] 非 UCI 或未出现在 comments 中的链接不会被 workflow 自行访问。
- [x] prompt 测试：availability 不允许只凭本地 DB；major restriction 不允许 agentic search 优先。
- [x] 前端静态契约测试：新 tool chip 和 source badge 不破坏现有卡片。

### M10.13 最小可执行版本

第一版只要求完成：

1. `get_live_sections` 或等价 live availability dispatcher。
2. availability intent 路由到 live Anteater WebSoc。
3. live result 返回 status、capacity、enrolled、seats_open、waitlist、NOR、restrictions、updated_at/retrieved_at。
4. 5 分钟 TTL cache 和 force refresh。
5. `websoc_department_restrictions` workflow 可以按 term + department 访问 Registrar WebSoc。
6. 能解析 WebSoc 顶部 comments 和 comments links。
7. ICS comments link 能被识别并按需深读。
8. fake/fixture 测试默认不联网。

### 验收

- 用户问“现在还有几个位置 / open 吗”时，系统使用 live Anteater WebSoc，不用本地 CSV 当实时事实。
- 用户问 major restriction / NORS 时，系统固定从 Registrar WebSoc department 页面开始。
- WebSoc comments 中的官方 department 链接可以被收集并按需深读。
- ICS 这类把 restriction timeline 放在部门网页的问题可以回答，并标注 WebSoc + ICS official page 双来源。
- 所有实时 availability 回答都显示 `updated_at` 或 `retrieved_at`。
- workflow 失败时返回无法验证，不编造日期、位置、restriction 或来源。
- 默认测试不依赖真实网络、真实 Anteater API 或真实 Registrar 页面。

## 15. M11 — Agentic Deep Search Tool 与 Workflow Registry 重构

目标：重新定义 `web_search` 的职责。`web_search` 不再代表“agentic 解决方案本身”，而是一个可被 workflow 和 agentic 路线共同调用的工具。问题解决方案只分为两类：开发者预先规定的 `workflow`，以及未被规定时进入的 `agentic`。两类方案都可以使用搜索工具补充信息，但 workflow 的固定入口、固定解析路径和主证据优先级由开发者写死。

本阶段不直接实现新的业务 workflow，而是改造搜索工具、深度抓取、历史记录和人工沉淀机制，为后续把高频 agentic 路径固化成 workflow 做准备。

状态：已完成。实现按独立阶段提交，完整离线回归 `202 passed`。

### M11.1 概念边界重定义

- [x] 将“解决方案路线”明确为：
  - `workflow`：开发者预先写死某类问题的固定路径、固定入口、固定 parser、主证据规则。
  - `agentic`：未命中开发者规定 workflow 的问题，由模型自行决定用哪些 tool。
- [x] 将 `web_search` 重新定义为普通 tool，而不是路线分类：
  - workflow 可以调用它获取补充信息。
  - agentic 可以调用它作为自主搜索入口。
  - `web_search` 本身不决定一个问题是否属于 workflow。
- [x] 保留 M10 已完成的 UCI WebSoc availability / department restriction workflow 作为固定 workflow 示例。
- [x] 重构 M10 的 `workflow_router` 概念：
  - 不再让 heuristic 自动“学习”哪些问题是 workflow。
  - 改成 developer-maintained workflow registry / rule table。
  - registry 命中后进入固定 workflow；未命中才进入 agentic。
- [x] workflow 主来源与 deep search 补充来源冲突时，回答同时列出双方来源，不替用户做隐式裁决。

### M11.2 Deep Search Tool 形态

- [x] 采用“方案 B 简化版”：
  - `web_search(query, ...)` 只负责找入口和返回搜索结果。
  - 新增 `fetch_page(url, ...)` 负责抓取页面、提取摘要、关键段落和 links。
  - 模型决定是否继续抓取页面里的链接。
- [x] `fetch_page` 返回内容粒度：
  - page title
  - summary
  - key passages
  - links
  - source URL / final URL / domain / retrieved_at / trust metadata
- [x] `fetch_page` 返回的 links 第一版不强过滤：
  - 全部返回可提取链接。
  - 标注 domain、source position、trust/source_class。
  - 由模型决定下一步访问哪些链接。
- [x] 允许 `fetch_page(url)` 从任意公开 URL 开始，不要求 URL 一定来自 `web_search`。
- [x] workflow 中已拿到的官方链接，例如 WebSoc comments 里的 ICS 链接，也可以作为 deep search 起点继续探索。

### M11.3 Run 内 Visited Memory 与去重

- [x] 新增 deep-search run state，用于一次 agent run 内的 hard dedupe。
- [x] 同一次回答中，已访问过的 normalized URL 不重复抓取。
- [x] 如果模型再次调用同一 URL，`fetch_page` 返回 structured result：
  - `ok=false`
  - `error_code=already_visited`
  - 已有页面摘要引用或 visited metadata
- [x] URL normalize 至少处理：
  - scheme/host lowercase
  - 去掉 fragment
  - canonical trailing slash
  - query 参数稳定排序
  - 常见 tracking 参数去除
- [x] 去重范围第一版只限一次 run；用户追问的新 run 可以重新抓同一 URL。
- [x] 每次 run 记录 link depth：
  - search result 是 depth 0。
  - 打开 search result 是 depth 1。
  - 从 depth 1 页面继续点链接是 depth 2。

### M11.4 Deep Search 预算

- [x] 单次回答最大链接深度：`depth <= 8`。
- [x] 单次回答最多抓取页面数：`fetch_page <= 8`。
- [x] 达到 depth 或 page 上限后，模型不能继续 deep fetch。
- [x] 达到 deep-search 上限但证据不足时，允许自动调用普通 `web_search` 找更多入口。
- [x] 上限后的普通 `web_search` 只作为补充搜索结果摘要，不允许继续 `fetch_page` 绕过 8 页限制。
- [x] 上限后 ordinary `web_search` 的结果不写入 deep-search 长期路径历史。
- [x] 回答必须说明 deep-search 已达到预算上限，并基于已有证据回答或说明证据不足。

### M11.5 页面访问边界

- [x] 产品语义上允许访问任何公开网页，不限定只访问 UCI 或官方域名。
- [x] 第一版主要边界是避免无用调用、重复调用和循环调用。
- [x] 保留最小工程安全底线：
  - 不访问 `file://`、`localhost`、private IP、link-local IP、metadata service。
  - 限制 timeout、redirect 次数、页面大小。
  - 默认只解析文本/HTML；二进制内容不进入 agent context。
- [x] 非官方 / 低可信页面可以返回给模型，但必须标注 trust/source_class，回答时不能伪装成官方来源。

### M11.6 长期 Deep Search History

- [x] 新增长期 deep-search history，使用 SQLite 存储。
- [x] 长期历史只记录公共 web-search 路径，全局共享。
- [x] 如果 query 或最终答案包含学生个人背景，不写入全局历史：
  - profile 信息
  - 已修课程
  - GPA
  - major / school year / class level
  - 个人计划、个人偏好、用户具体 schedule
- [x] 历史只记录：
  - URL 路径
  - 每个 URL 的 depth
  - 最终答案摘要
  - final answer source URLs
  - 是否使用 fallback ordinary `web_search`
  - 最大 depth
  - trace timestamps
- [x] 不长期保存页面全文、页面摘要、关键段落，避免存储过期内容或敏感内容。
- [x] 第一版先不做开发者手动标记更新入口，但 schema 预留：
  - `workflow_candidate`
  - `candidate_reason`
  - `review_status`

### M11.7 本地轻量相似匹配

- [x] 不使用付费外部 embedding API。
- [x] 第一版实现本地轻量 embedding / 关键词混合方案：
  - normalized query tokens
  - department / course / policy intent features
  - domain / URL path features
  - TF-IDF 或 hash vector
  - cosine similarity
- [x] 每条可记录 trace 生成本地向量或 feature signature。
- [x] 新 query 到来时，检索相似历史 trace / cluster。
- [x] 相似度不完美可以接受，但结果必须可解释，便于开发者审核。

### M11.8 历史提示注入给 Agent

- [x] 当新问题命中相似 deep-search history 时，自动把历史提示注入给 agent。
- [x] 历史提示是强建议：
  - 通常优先从历史 URL 路径开始。
  - 通常参考历史最大 depth。
  - 模型仍可自由判断是否访问额外网页。
- [x] 历史最终答案只能作为参考，不能直接复用为最终回答。
- [x] 即使命中历史，agent 必须至少重新检查一个来源页面后才能回答。
- [x] 注入内容不包含页面正文，只包含：
  - 相似 query/cluster 摘要
  - 推荐 URL 路径
  - 推荐 depth
  - 上次 final answer summary
  - 上次 source URLs
  - last_seen_at / hit count
- [x] 回答不能说“历史记录证明...”；必须基于本次重新抓取的来源作答。

### M11.9 Trace 与开发者后续固化 Workflow

- [x] 记录所有 deep-search traces，后续由开发者筛选高频问题是否固化成 workflow。
- [x] SQLite 统计字段覆盖：
  - normalized query
  - local embedding / feature id
  - similar cluster id
  - visited URL path
  - max depth
  - fallback ordinary `web_search` 是否使用
  - final answer summary
  - final source URLs
  - occurrence count
  - first_seen_at / last_seen_at
  - workflow_candidate
  - review_status
- [x] 第一版不做开发者后台 UI。
- [x] 第一版不做 candidate 更新 API。
- [x] 先提供日志 / SQLite 数据，后续再做开发者后台 UI。

### M11.10 与 M10 的关系和需要砍掉/调整的内容

- [x] 保留 M10 的 `get_live_sections`，它仍是课程实时 availability 的固定 workflow tool。
- [x] 保留 M10 的 `get_department_restrictions`，它仍是专业限制问题的固定 workflow tool。
- [x] 保留 WebSoc comments linked-page deep read，但后续可复用 M11 的通用 `fetch_page` 能力，减少重复 crawler/parser。
- [x] 调整 M10 `workflow_router`：
  - 从 heuristic intent router 改为 developer-authored registry。
  - 不让系统自动学习定义 workflow。
  - 历史 trace 只辅助开发者发现高频候选 workflow。
- [x] 调整 prompt：
  - workflow vs agentic 的选择来自 registry，不来自模型自由判断。
  - `web_search` / `fetch_page` 是工具，可被两条路线调用。
  - workflow 主证据和 deep-search 补充证据冲突时，同时列出。
- [x] 砍掉“deep search 自动递归抓链接”的方向；第一版坚持由模型逐步选择链接。
- [x] 砍掉“长期历史命中后直接复用答案”的方向；必须重新检查至少一个来源。

### M11.11 测试计划

- [x] `fetch_page` 返回 title、summary、key passages、links、source/trust metadata。
- [x] `fetch_page` 对同一 run 内重复 URL 返回 `already_visited`。
- [x] URL normalize / tracking 参数去重测试。
- [x] depth 从 search result 到 linked page 正确递增。
- [x] `depth > 8` 被拒绝。
- [x] 单 run `fetch_page > 8` 被拒绝。
- [x] deep limit 后 ordinary `web_search` 可作为补充，但不能再 `fetch_page`。
- [x] limit 后 ordinary `web_search` 不写入长期 history。
- [x] workflow 和 agentic 都可以调用 `fetch_page`。
- [x] developer registry 命中 workflow；未命中进入 agentic。
- [x] workflow 主来源与 deep-search 补充来源冲突时，tool/prompt 测试要求同时列出。
- [x] public query trace 写入 SQLite。
- [x] 含个人背景 query / answer 不写入 SQLite history。
- [x] history 只保存 URL path、depth、final answer summary、sources，不保存页面正文。
- [x] 本地相似匹配能把常见同义问法归到相似 cluster。
- [x] history hint 注入给 agent，且要求至少重新 fetch 一个来源。
- [x] 测试默认不联网，使用 fake search provider、fake page fetcher、fixture pages。

### M11.12 最小可执行版本

第一版只要求完成：

1. 新增 `fetch_page` tool。
2. `web_search` 继续作为入口搜索 tool。
3. 单 run visited URL memory 和 depth/page budget。
4. SQLite deep-search trace 存储。
5. 公共 query 才记录，全局共享；个人化 query 不记录。
6. 本地轻量相似匹配。
7. 相似历史强建议注入给 agent。
8. 命中历史后必须重新 fetch 至少一个来源页面。
9. M10 workflow router 调整为 developer-authored registry。
10. 全部默认测试离线。

### 验收

- 开发者规定的 workflow 优先于 agentic，但 workflow 可以调用 `web_search` / `fetch_page` 作为补充。
- 未命中 workflow registry 的问题进入 agentic，由模型自行决定搜索与深读路径。
- 模型可以逐步选择页面 links，但同一 run 内不能重复访问同一 URL，也不能超过 depth/page 预算。
- 长期 history 能记录公共 deep-search trace，并在相似问题中给 agent 强建议。
- history 命中不会直接复用旧答案，回答前必须重新检查至少一个来源。
- 含用户背景的问题不会写入全局 deep-search history。
- 高频 deep-search trace 能为后续人工固化 workflow 提供足够统计字段。
- 默认测试不依赖真实网络、真实搜索 API 或真实网页。

## 16. M12 — WebSoc Restriction Workflow 可靠性修正

目标：修复 Registrar WebSoc 首页和结果页 URL 相同导致的错误抓取。所有选课限制、专业限制、NORS、authorization code 和限制解除时间问题，必须由服务端先执行开发者定义的固定 workflow，提交真实 WebSoc 表单并验证返回结果，再把证据交给模型组织答案。

状态：已完成。按四个功能阶段和一个收尾阶段独立提交。

### M12.1 提交真实 WebSoc 表单

- [x] 先 GET `https://www.reg.uci.edu/perl/WebSoc` 读取当前可用的 term 和 department option。
- [x] 使用 Registrar 表单要求的 POST 方法提交 `YearTerm`、`Dept`、comments、finals 和 cancelled-course 参数。
- [x] 不再把 criteria 伪装成可复现的 GET URL；source URL 保留官方 endpoint，请求参数单独记录。
- [x] 终端日志显示 request method、form criteria、response URL、HTTP status 和 content type。

### M12.2 验证结果页而不是相信 HTTP 200

- [x] POST 前验证 term/department 确实存在于当前 WebSoc 表单选项。
- [x] POST 后验证页面标题是 `Schedule of Classes search results`。
- [x] 验证返回的 Search Criteria department 与请求一致。
- [x] 验证返回学期与请求一致，并确认存在 school/department comments。
- [x] 验证失败时返回结构化 error code，禁止模型把首页或错误学期当作有效结果。

### M12.3 服务端强制执行固定 Workflow

- [x] 扩展开发者维护的 restriction registry，覆盖专业/选课/院系限制、NORS、authorization code、A/B/X restriction、外专业选课和 add/drop/change deadline。
- [x] restriction workflow 在第一次 LLM 调用前由 agent loop 直接执行，模型不能跳过主来源。
- [x] 固定 workflow 要求用户明确给出 term 和 department/course；缺少或冲突时直接澄清，不静默使用 UI 默认学期。
- [x] availability + restriction 组合问题按固定顺序先查 live sections，再查 department restrictions。
- [x] 模型重复调用同一主 workflow 时复用本 run 的已验证结果，不重复联网。

### M12.4 WebSoc Comments 链接证据

- [x] WebSoc comments 没有直接给出目标信息时，读取其中实际出现的 UCI 官方链接。
- [x] linked page 返回限制字段、关键段落、页面内 links、domain 和 source URL。
- [x] 聚合 linked-page restriction evidence，供模型区分 WebSoc 主证据与院系页面补充证据。
- [x] 只有明确日期/截止时间才把解析状态标为 `complete`；一般政策说明保留为证据但标为 `partial`。

### M12.5 验收与可观测性

- [x] fixture 覆盖 ART 明确解除日期、ICS 跳转院系页面、错误 department、错误 term、首页误抓和 comments 缺失。
- [x] 每次主请求和 linked-page 请求均记录 `web_search_url`、criteria/depth、response metadata、提取字段、关键段落和错误。
- [x] 定向测试证明 server-forced workflow 发生在第一次 LLM 请求前。
- [x] 完整离线测试、compileall、真实 Fall 2026 ART WebSoc smoke check。

### M12 提交分组

1. `af3e8ea`：通过 POST 提交真实 WebSoc 表单。
2. `d0fd2d5`：验证 live form options 和返回结果页。
3. `a3cd274`：在 LLM 前强制执行 restriction workflow。
4. `099845a`：提取 linked-page restriction evidence。
5. 文档、完整回归和 live smoke check：本阶段收尾提交。

## 17. M13 — 自动学期上下文与跨学期 Schedule

目标：删除可操作的全局 term selector，把 term 变成后端统一管理的底层上下文。系统根据美西时间、UCI 学期日历、Week 2 Friday 截止点和 Anteater WebSoc 实际发布状态选择默认学期；每个 conversation 独立保存 auto/pinned 状态，所有工具、校验和 Schedule 使用同一套 term 解析结果。

状态：已完成（2026-07-22）。已严格按以下阶段推进，每个阶段测试通过后建立独立 Git commit。

### M13.1 范围与不变量

- [x] 自动默认学期只在 `Fall`、`Winter`、`Spring` 三个常规学期之间切换。
- [x] 常规顺序固定为 `2026 Fall -> 2027 Winter -> 2027 Spring -> 2027 Fall`。
- [x] Summer 不参与自动切换；用户明确指定时仍允许查询 `Summer1`、`Summer10wk`、`Summer2`。
- [x] term canonical format 统一为 `YYYY Quarter`，例如 `2026 Fall`。
- [x] 所有时间计算使用 IANA timezone `America/Los_Angeles`，不能使用服务器本地时区或固定 UTC offset。
- [x] LLM 不负责计算当前学期、截止时间或数据可用性；后端确定性服务是唯一权威。
- [x] 后端已有显式 `term` 参数继续保留，供内部工具、测试、多学期比较和显式查询使用。
- [x] 用户明确查询的数据 term 优先于系统默认 term，但只有成功查询后才能改变 conversation term。
- [x] 不在助手回答末尾重复显示 term；顶部只读 term 是用户可见的当前上下文。

### M13.2 `TermResolutionService` 与统一数据模型

- [x] 新增单一 `TermResolutionService`，禁止 chat、schedule、workflow、validation 各自实现 term fallback。
- [x] 定义 canonical `TermKey` / `ResolvedTerm`：
  - `year`
  - `quarter`
  - `canonical_name`
  - `instruction_start`
  - `week2_friday_cutoff`
  - `data_available`
  - `source`
  - `status`
  - `checked_at`
- [x] 支持解析标准名称、无空格写法、中文表达和相对表达，最终统一为 canonical format。
- [x] “当前学期 / 下学期 / 上学期”等相对表达始终以系统自动默认学期为基准，不以 pinned conversation term 为基准。
- [x] 一个问题只指定一个可用 term 时返回 single-term resolution。
- [x] 一个问题指定多个 term 时返回 multi-term resolution，但不得改变 conversation term。
- [x] 不合法、歧义或无法映射的 term 返回结构化错误，不允许静默使用默认学期。
- [x] 所有时间依赖可注入 clock，测试不能依赖真实当前时间。

### M13.3 Anteater Calendar 与 WebSoc 发布状态

- [x] 扩展 `app/data/anteater.py`，增加：
  - `GET /v2/rest/calendar/all`
  - `GET /v2/rest/websoc/terms`
  - `GET /v2/rest/websoc?year=...&quarter=...`
- [x] `/calendar/all` 负责提供 `instructionStart` 等学期日历字段。
- [x] `/websoc/terms` 只作为“term 已出现在 WebSoc”的第一层条件，不能单独判定可用。
- [x] 对候选下一学期按 `COMPSCI -> MATH -> BIO SCI` 做部门级 WebSoc 探测，首个非空结果至少包含一门 course 和一个 section 才标记 `data_available=true`；不再下载全校 WebSoc。
- [x] 空 term shell、只有 school/department 但没有 course/section 的响应不能触发自动切换。
- [x] WebSoc 有数据但 calendar 缺少该 term 的 `instructionStart` 时，不自动切换。
- [x] API non-200、timeout、invalid JSON、`ok=false`、schema 缺失分别返回结构化状态并记录日志。
- [x] 只在同步到期时探测候选 term，普通聊天请求不得重复执行 availability probe。

### M13.4 Week 2 Friday 截止点

- [x] 如果权威数据源提供明确的统一 add/drop deadline，优先使用官方字段。
- [x] 没有明确 deadline 时，根据 `instructionStart` 计算：
  1. 找到 instruction start 当天或之后的第一个 Monday，作为 Week 1 Monday。
  2. 加 11 天得到 Week 2 Friday。
  3. 截止时刻设为美西时间 `17:00:00`。
- [x] 截止点比较必须精确到 instant：17:00 前不能切换，17:00 到达后才满足 cutoff 条件。
- [x] DST 由 `America/Los_Angeles` 自动处理，禁止写死 `PST` 或 `UTC-8`。
- [x] calendar 中存在异常日期、重复 term 或 instruction start 缺失时，拒绝推断并保留当前默认学期。

### M13.5 `TermStateStore`、同步与 Fallback

- [x] 定义可替换的 `TermStateStore` 接口，隔离缓存、锁和业务判定。
- [x] 本地开发第一版使用文件缓存和进程锁，不引入尚未确定的数据库或 Redis。
- [x] 上线确定共享存储后，只替换 store 实现，不修改 `TermResolutionService` 规则。
- [x] 缓存至少保存：
  - calendar records
  - WebSoc term list
  - 已验证的 term data availability
  - current automatic term
  - source URLs
  - last_success_at
  - last_attempt_at
  - last_error
- [x] 运行期间每 30 天同步一次。
- [x] 应用启动时只有缓存年龄超过 30 天才同步；不再启动时预热约 90 页全校课程，Onboarding 按需加载。
- [x] 缓存 45 天内仍可用于解析和自动 term；同步失败时保留最近一次成功状态。
- [x] 缓存超过 45 天且同步失败时，使用代码内置、随版本更新的 fallback term。
- [x] fallback 状态必须返回 `source=code_fallback` 和 `status=fallback`，禁止伪装成实时 Anteater 判定。
- [x] 未取得跨进程共享锁时不得并发执行第二次完整同步；第一版记录此限制，生产多实例存储确定后补分布式锁。

### M13.6 自动默认学期算法

- [x] 自动切换必须同时满足：
  - 当前默认学期已超过统一 Week 2 Friday 17:00 截止点。
  - 下一常规学期存在完整 calendar 数据。
  - 下一常规学期出现在 `/websoc/terms`。
  - 下一常规学期的部门级 WebSoc 探测至少包含一门 course 和一个 section。
- [x] 任一条件不满足时，继续使用当前默认学期。
- [x] Summer 数据即使已经发布，也不能成为自动默认学期。
- [x] 系统恢复时如果已经跨过多个截止点，并且多个后续常规 term 都满足条件，一次前进到满足规则的最新 term。
- [x] 不能仅按 calendar 当前日期决定 term，也不能仅选择 WebSoc term list 中名称最大的 term。
- [x] 默认 term 变化记录 previous/current term、cutoff、availability evidence、source 和 changed_at。

### M13.7 Conversation `auto` / `pinned` Memory

- [x] conversation metadata 新增：
  - `term_scope`
  - `term_mode: auto | pinned`
  - `term_source`
  - `term_updated_at`
- [x] 新 conversation 一律创建为 `auto`，不得继承上一个 conversation 的 pinned term。
- [x] `auto` conversation 每次打开时解析系统默认 term；系统默认 term 更新后自动跟随。
- [x] 用户明确指定一个可用 term，并且该轮查询与回答成功后，conversation 才切换为 `pinned`。
- [x] 查询失败、tool 失败、validation block 或 term 尚无 course/section 数据时，不改变原 conversation term。
- [x] 用户指定另一个可用 term 后，成功回答时更新 pinned term。
- [x] 用户说“回到当前学期”时恢复 `auto`，之后继续跟随系统默认 term。
- [x] 用户指定过去学期时，只影响当前 conversation；新 conversation 仍使用 automatic term。
- [x] 用户指定尚无数据的 term 时拒绝切换，并明确说明该学期数据尚未发布。
- [x] 用户比较多个 term 时可以执行多学期工具调用，但 conversation term 保持不变。
- [x] 相对单 term 查询成功后，将解析结果保存为 pinned term；“当前学期”语义恢复 auto。
- [x] 打开历史 conversation 必须先读取自己的 `term_mode/term_scope`，不能被前端全局默认值覆盖。
- [x] 迁移所有旧 conversation 为 `auto`；旧 `term_scope` 不视为用户主动 pinned。
- [x] 迁移可重复执行，失败时不破坏原 meta/turn 文件，并记录迁移版本。

### M13.8 Chat、Workflow、Agentic 与 Validation 接入

- [x] chat turn 开始时由后端解析 effective term，再构建 session state、agent context 和 memory context。
- [x] 前端请求中的 term 不再作为普通聊天的权威默认值。
- [x] developer workflow、agentic tools、WebSoc、Anteater live availability、catalog 和 validation 使用同一个 effective term。
- [x] LLM 可以识别用户意图并提出 term 工具参数，但后端必须 canonicalize、验证可用性并拒绝非法切换。
- [x] tool 实际查询 term 与 UI/session term 不一致时，validation 必须使用 tool/query resolution，而不是旧 term。
- [x] 多工具调用涉及多个 term 时，validation 按每项数据的 term/provenance 检查，不能压成单一全局 term。
- [x] SSE `meta` 返回最终 effective term、term mode、term source 和 conversation 更新结果。
- [x] 只有最终回答成功且未被 validation block，才提交 pinned term metadata。
- [x] 保留显式后端 `term` 参数兼容，删除“前端下拉框每轮覆盖 session term”的链路。
- [x] terminal 日志显示 resolved term、mode、source、用户显式 term、tool term、validation term 和是否持久化。

### M13.9 前端只读 Term

- [x] 删除顶部可操作的全局 term selector 及其 option loading/change handler。
- [x] 在原位置显示稳定尺寸的只读文本：`Term: 2026 Fall`。
- [x] 使用代码 fallback 时显示：`Term: 2026 Fall · fallback`。
- [x] 不显示 `Auto` / `Pinned` 文案。
- [x] 创建新 conversation 后立即显示 automatic term。
- [x] 打开历史 conversation 后显示该 conversation 解析后的 effective term。
- [x] 用户指定 term 的回答成功后，根据 SSE meta 更新只读 term；失败时保持原显示。
- [x] 切换 conversation 时禁止先闪回系统默认 term，再加载 pinned term。
- [x] term 文本必须在桌面和移动端完整显示，不因长名称挤压导航或按钮。
- [x] 删除所有由 selector 向 chat、schedule、cards 隐式注入 term 的前端路径。

### M13.10 跨学期 Schedule

- [x] 每个 pending schedule entry 永久保存自己的 canonical `term`，不能依赖当前 conversation term 推断。
- [x] 旧 schedule entry 缺少 term 时执行兼容迁移；无法可靠确定时标记 unknown，不静默套用当前 term。
- [x] section lookup、calendar event materialization、删除和去重都使用 entry 自己的 term。
- [x] Schedule 页面同时展示所有已加入 term 的课程，不按当前 conversation term 隐藏其他课程。
- [x] 不同 term 的课程时间重合时允许加入和显示 overlap。
- [x] 同 term 的时间重合也不弹冲突确认框、不返回 409 阻止加入；冲突检测可保留为非阻断 metadata。
- [x] Calendar block 保持现有内容密度，不额外塞入 term 文本；entry 的 term 在列表、详情或数据属性中可查询。
- [x] 不同课程继续使用不同颜色；颜色不需要与 term 建立固定映射。
- [x] 当新 entry 的 term 与 Schedule 中已有 entry term 不同时：
  - 先成功加入。
  - 再显示纯提示 toast。
  - toast 有关闭按钮。
  - toast 数秒后自动消失。
  - toast 不要求确认，也不撤销已加入课程。
- [x] 同一 course/section 在不同 term 中视为不同 entry，不能跨 term 错误去重。

### M13.11 API、可观测性与运维

- [x] 新增只读 term-state API；浏览器只返回 automatic term、source、status，last success/attempt、next cutoff、availability 与 transition 留在 health/metrics。
- [x] session API 返回 resolved effective term 和 `term_mode`，前端不自行推断。
- [x] health/metrics 增加 calendar sync、WebSoc availability probe、cache age、fallback 和 term transition 状态。
- [x] 日志事件至少包括：
  - `term_sync_started/completed/failed`
  - `term_availability_checked`
  - `automatic_term_changed`
  - `conversation_term_pinned`
  - `conversation_term_reset_auto`
  - `term_fallback_activated`
- [x] Agent 真实联网使用统一审计日志：started/completed/failed 记录 URL、method、status、content length、duration、trigger、`cache_hit=false`，结束时列出实际 fetched URLs；页面正文、passages、restriction fields 和完整工具结果不写日志。
- [x] 记录 Anteater API attribution 要求，并在使用其数据的相关产品位置保留归属说明。
- [x] 本地文件 store 路径进入 runtime data 目录并加入 `.gitignore`，不得提交实时缓存。

### M13.12 测试计划

- [x] canonical term parsing：标准、无空格、中文、relative、invalid、ambiguous、multi-term。
- [x] 常规序列跨年：Fall -> Winter -> Spring -> Fall。
- [x] Summer 不参与 automatic sequence，但 explicit Summer 可查询。
- [x] Fall Thursday instruction start 正确计算第二个教学周 Friday 17:00。
- [x] Winter/Spring Monday instruction start 正确计算 Week 2 Friday。
- [x] 截止前一秒不切换，截止 instant 到达后允许切换。
- [x] `America/Los_Angeles` DST 边界测试。
- [x] calendar 完整 + term list 出现 + course/section 非空时才 data available。
- [x] 空 shell、无 course、无 section、calendar 缺失均不能切换。
- [x] 多个后续 term 已满足条件时选择最新合格常规 term。
- [x] 30 天同步、启动 freshness gate、45 天 stale 和 code fallback 使用 fake clock 测试。
- [x] API timeout/non-200/schema error 保留 last-known-good cache。
- [x] 新 conversation auto、自动跟随、single-term pin、reset auto、failed query 不 pin。
- [x] 旧 conversation 全部迁移为 auto，迁移幂等。
- [x] sidebar 切换后 pinned/auto term 显示与持久化一致。
- [x] multi-term query 不改变 conversation term。
- [x] tool/validation 使用 resolved query term，不再复现 Spring UI 校验 Fall WebSoc 回答的问题。
- [x] schedule entry 保存 term；跨 term 不错误去重；lookup 使用 entry term。
- [x] same-term 和 cross-term overlap 均允许加入，不触发冲突确认。
- [x] cross-term toast 在加入成功后出现、可关闭、自动消失。
- [x] 前端不再发送或依赖全局 selector term。
- [x] 默认测试全部使用 fake Anteater response、fixture calendar/WebSoc 和 fake clock，不联网。

### M13.13 分阶段提交计划

1. `M13-A`：Term model、canonical parser、timezone clock、Week 2 cutoff 计算与单元测试。
2. `M13-B`：Anteater calendar/terms/WebSoc availability client 与 fixture 测试；收尾优化为部门级短路探测。
3. `M13-C`：TermStateStore、30 天同步、45 天 stale、fallback 和 observability。
4. `M13-D`：automatic term state machine、auto/pinned conversation metadata 和旧 session 迁移。
5. `M13-E`：chat/workflow/agentic/validation 统一 effective term，删除前端 term authority。
6. `M13-F`：只读 term UI、SSE/session restore 与 sidebar 回归。
7. `M13-G`：schedule entry term、跨 term toast、非阻断 overlap 和 schedule 回归。
8. `M13-H`：完整离线回归、真实 Anteater smoke check、README/ROADMAP 收尾。

实际阶段提交：M13-A `4c791e9`、M13-B `cd1a8cb`、M13-C `a1d2777`、M13-D `f41a304`、M13-E `dfb703c`、M13-F `04012b9`、M13-G `7defe01`，M13-H 为本收尾提交。

每组提交只包含对应阶段文件。`.idea/`、实时 term cache、用户 session/memory 和 SQLite runtime 文件不得进入提交。

### M13 验收

- 全局 term selector 已删除，顶部只显示 conversation 对应的只读 canonical term。
- 所有后端业务使用唯一 `TermResolutionService`，不存在 UI term、session term、tool term、validation term 互相覆盖。
- automatic term 只有在 cutoff 和下一学期真实数据同时满足时才切换。
- auto conversation 跟随默认学期；pinned conversation 跨 sidebar 切换和重启后保持指定 term。
- 无数据的 explicit term 不会改变 conversation。
- multi-term 查询可以执行但不会隐式 pin。
- Schedule 可以保存并同时显示不同 term 的课程，所有 overlap 都是非阻断的。
- 跨 term 加课后显示可关闭、自动消失的提示，不要求二次确认。
- API 不可用时按 30/45 天 cache 规则降级，并明确显示 fallback。
- 完整默认测试离线通过，真实 smoke check 不进入默认 CI。
- M13-A 到 M13-H 每阶段都有独立 Git commit，可以单独回退。

## 18. 跨阶段 Definition of Done

每个任务只有同时满足以下条件才算完成：

- 有自动化测试覆盖成功、失败和 unknown 路径。
- 不通过 silent fallback 掩盖数据缺失或实现错误。
- 用户可见事实携带 term、source、updated_at；web-sourced 事实必须携带 URL、domain、retrieved_at 和 trust_level。
- 数据或 API schema 变化有迁移与兼容说明。
- 不引入新的重复状态源、重复正则或重复业务链路。
- 不提交真实用户数据、密钥、验证码或 Cookie。
- 代码、测试和验收证据在同一个 PR/commit 范围内可审查。
- 对实时数据链路，必须明确 freshness、cache TTL、source 和 fallback 语义。
- 对固定 workflow，必须有 fixture 测试证明不会绕到 agentic search。

## 19. 实际提交分组

工作已按可审查、可回滚的阶段提交。每组提交都对应 ROADMAP 中的阶段性验收：

1. M0：运行数据隔离、fixture/checkpoint、demo baseline。
2. M1：pytest 基础设施、fake LLM、离线 fixture、characterization tests。
3. M2：身份/session/memory/profile schema 与持久化修复。
4. M3：统一 scheduling domain service、schedule validation、ownership。
5. M4：本地 catalog/prerequisite/coverage/validation。
6. M5：唯一 streaming agent 主链路、legacy recommendation 删除、课程号解析统一。
7. M6：前端 SSE 收敛、静态资源模块拆分、浏览器回归。
8. M7：私测安全、依赖/CI、可观测性。
9. M8：Roadmap 和 README 与当前实现对齐。
10. M9：联网搜索 tool、Search Skill、来源分类、引用展示和离线 fake-provider 测试。
11. M10：Live WebSoc 可用性、专业限制 workflow、WebSoc comments 链接深读。
12. M11：Agentic deep search tool、run 内 visited memory、SQLite trace history、workflow registry 重构。（已完成。）
13. M12：WebSoc POST 表单、结果验证、服务端强制 restriction workflow、linked-page evidence。（已完成。）
14. M13：自动学期上下文、conversation auto/pinned term、只读 term UI 和跨学期 Schedule。（已完成。）

## 20. 进度记录

| 日期 | 阶段 | 变更 | Commit/PR | 验收结果 |
|---|---|---|---|---|
| 2026-07-04 | M0 | 运行数据隔离、demo fixture/checkpoint、baseline 记录 | `751af53`, `a7b56ab`, `86de71a` | 真实运行数据从提交范围中隔离；后续阶段具备可回滚 checkpoint。 |
| 2026-07-04 | M1 | pytest、fake LLM、离线 fixture、核心 characterization tests | `7455b79`–`4557643` | 默认测试不需要 API key、不联网、不调用真实 LLM；核心状态/ownership/SSE/card/schedule 行为被固定。 |
| 2026-07-04 | M2 | session identity、persistent session repository、memory schema 统一 | `2ba2652`–`39915b6` | 新会话首轮状态连续；重启后 term/profile/history/schedule 不丢；memory 不再重复保存完整 turn。 |
| 2026-07-04 | M3 | scheduling domain service、bundle validation、schedule API ownership | `39915b6`–`67ee8b4` | Agent 推荐和手动加课使用同一冲突结果；硬冲突和 incomplete Lec/Dis/Lab 不静默加入。 |
| 2026-07-04 | M4 | local catalog、prerequisite tree、coverage manifest、validation gating | `2d9833e`–`ab647d9` | 数据 coverage 区分 complete/partial/stale/unavailable；错误卡片不能进入 schedule。 |
| 2026-07-04 | M5 | 唯一 `/api/chat/stream` 主链、legacy recommendation 删除、课程解析统一 | `7c20bc9`, `48fd48d`, `18aea09` | 非流式旧入口和重复解析逻辑移除；Agent pre-flight fallback 行为确定。 |
| 2026-07-04 | M6 | 前端 SSE 收敛、资源模块拆分、浏览器回归 | `e8d2564`, `db9daca`, `5edad5e` | 登录/onboarding/chat/tool chip/card/add/continue/session restore/mobile/keyboard 基础回归通过。 |
| 2026-07-04 | M7 | 私测安全、依赖/CI、health/trace/metrics/logging | `ef4f779`, `a58f8d7`, `766cf94` | 生产安全默认值、rate limit、CI workflow、trace ID 和 health endpoints 已落地；`121 passed, 2 deselected`。 |
| 2026-07-04 | M8 | Roadmap 收尾、README 按当前架构重写 | `docs: close roadmap and README` | README 删除旧 demo/mock/placeholder；README 数据检查、compileall、pip check、`121 passed, 2 deselected` 通过。 |
| 2026-07-05 | M9 | 完成 controlled `web_search`、source classifier、Search Skill prompt、前端 chip/source badge 和离线 fake-provider 测试 | `feat: add controlled web search evidence chain` | `compileall`、`pip check`、`139 passed, 2 deselected` 通过；默认测试不联网，搜索关闭时结构化 unavailable。 |
| 2026-07-21 | M11 | 完成 developer workflow registry、`fetch_page`、run 去重与 depth/page 预算、SQLite 公共 trace、本地相似匹配、历史强建议与 fresh-source 门禁 | `fcebe7d`–`1439583` | `compileall` 与完整离线回归通过：`202 passed`；`.idea/` 未纳入提交。 |
| 2026-07-21 | M12 | 修复 WebSoc 同 URL 表单抓取，增加 live option/response validation、server-forced restriction workflow 和 linked-page structured evidence | `af3e8ea`–本阶段收尾提交 | `compileall`、完整离线回归 `219 passed, 2 deselected`；真实 ART POST 验证得到 major restriction `2026-08-24 noon`、NORS `2026-08-21 noon`。 |
| 2026-07-22 | M13 | 完成自动学期上下文、Anteater calendar/WebSoc 发布门禁、conversation auto/pinned、只读 term、跨学期 Schedule 和 live smoke | `4c791e9`–本阶段收尾提交 | `307 passed, 2 deselected`；calendar 108 条、WebSoc term 162 个；`2026 Fall` 发布探针只取 53 courses/458 sections、258,563 bytes，较原完整抓取 7,830,665 bytes 减少 96.7%；启动不再预热完整课表，runtime 数据未纳入 Git。 |
