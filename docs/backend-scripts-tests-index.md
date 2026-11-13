# 后端脚本与测试索引

配合 [后端代码逐文件导览](/Users/luoweipotter/Downloads/uci-course-advisor1/docs/backend-code-map.md) 使用。本索引覆盖当前工作区 `scripts/` 的 26 个脚本与 `tests/` 的 92 个 Python 文件，另列后端启动和验证配置。脚本用于维护、迁移或检查；测试用于说明已覆盖的反例，它们都不是在线请求默认逐个执行的业务模块。

前端相关脚本与测试列出位置以免漏项，具体前端实现留到下一部分。`tests/fixtures/` 保存公开响应、最小目录、合成用户资料等测试数据；夹具是回归输入，不是部署时的事实来源。本次没有运行数据迁移、抓取脚本或真实模型诊断。

## 脚本逐文件清单

| 文件 | 做什么 | 执行性质 |
| --- | --- | --- |
| [scripts/_bootstrap.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/_bootstrap.py:1) | 给直接执行的 Python 脚本补仓库根目录的 import 路径。 | 公共辅助 |
| [scripts/benchmark_dynamic_prefilter.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/benchmark_dynamic_prefilter.py:1) | 用离线合成候选衡量动态预筛耗时与排除结果，不测真实模型推荐质量。 | 离线检查 |
| [scripts/fetch_uci_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/fetch_uci_data.py:1) | 从 Anteater 抓课程元数据或指定学期班次，转换并写入本地 CSV，保留新版注释和 meetings 字段。 | 联网并写数据 |
| [scripts/import_professor_reviews.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/import_professor_reviews.py:1) | 把 RMP ndjson 评论导入 SQLite 并建检索索引；重跑会重建 reviews 表。 | 写数据 |
| [scripts/import_term_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/import_term_data.py:1) | 把清洗过的 xlsx 关系表并入 data/uci 的课程班次关联 CSV，按主键去重。 | 写数据 |
| [scripts/migrate_memory_to_sqlite.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/migrate_memory_to_sqlite.py:1) | 把旧 JSON 档案、事实与偏好提前迁入 SQLite 记忆库；支持 dry-run。 | 迁移数据 |
| [scripts/migrate_preferences_schema.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/migrate_preferences_schema.py:1) | 将旧的字符串偏好数组转成带 ID 和时间的偏好对象，以支持逐条遗忘。 | 迁移数据 |
| [scripts/migrate_sessions_schema.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/migrate_sessions_schema.py:1) | 把旧单聊天 turn_log.jsonl 转成新 sessions 目录下的 Legacy chat 会话。 | 迁移数据 |
| [scripts/migrate_term_metadata.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/migrate_term_metadata.py:1) | 调用幂等迁移，将会话旧的手动学期元数据转到自动模式。 | 迁移数据 |
| [scripts/probe_anteater_api.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/probe_anteater_api.py:1) | 只读探查院系、专业、GE、要求树等 Anteater 接口是否可用。 | 联网诊断 |
| [scripts/scrape_major_requirements.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/scrape_major_requirements.py:1) | 抓官方本科专业页面，保存要求原文与课程提及到 major_requirements_raw JSON。 | 联网并写数据 |
| [scripts/smoke_limit_reached.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/smoke_limit_reached.py:1) | 用模型替身检查 Agent 预算结束、fallback 和 Continue 的执行协议。 | 离线检查 |
| [scripts/smoke_restriction_evidence.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/smoke_restriction_evidence.py:1) | 只读访问官方 WebSoc 和允许的相关链接，检查限制类型、时间、例外和来源。 | 联网检查 |
| [scripts/smoke_term_state.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/smoke_term_state.py:1) | 只读访问实际学期数据源，检查校历、可选学期与目标学期可用性；不保存运行学期缓存。 | 联网检查 |
| [scripts/test_llm.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/test_llm.py:1) | 诊断保留的 stream_answer_llm 与分层上下文路径，真实调用模型；其旧注释不代表当前聊天主链。 | 真实模型诊断 |
| [scripts/verify_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_data.py:1) | 按子命令检查课程、班次、教授、成绩、冲突，trace 可运行 Agent 并打印工具调用；是否联网取决于所选命令。 | 数据与模型诊断 |
| [scripts/verify_db.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_db.py:1) | 只用本地目录检查 data.db 公开返回结构。 | 离线检查 |
| [scripts/verify_professor_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_professor_data.py:1) | 核对本地班次教授名在 RMP 快照中的匹配覆盖，写入命中样本和漏匹配名单。 | 本地检查并写报告 |
| [scripts/verify_registration_ui.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_registration_ui.cjs:1) | 用本地服务器和合成 API 检查注册界面，不创建真实账户。 | 前端检查 本部分不展开 |
| [scripts/verify_schedule_api_ui.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_schedule_api_ui.cjs:1) | 连接集成测试启动的临时真实 FastAPI，检查课表增删刷新和日历；部分外围 API 用替身。 | 跨前后端检查 |
| [scripts/verify_solon_ui.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_solon_ui.cjs:1) | 用浏览器和合成 API 回归主要交互与布局。 | 前端检查 本部分不展开 |
| [scripts/verify_student_profile_ui.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_student_profile_ui.cjs:1) | 用合成数据检查 Student Profile 与课程选择器界面。 | 前端检查 本部分不展开 |
| [scripts/verify_transcript_import_ui.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_transcript_import_ui.cjs:1) | 检查 PDF.js 阅读器、worker、导入控制器与档案展示；可只读本地 PDF 样本，API 持久化使用替身。 | 前端检查 本部分不展开 |
| [scripts/verify_welcome_motion.cjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_welcome_motion.cjs:1) | 用本地浏览器检查欢迎区动画、发送过程和失败等交互。 | 前端检查 本部分不展开 |
| [scripts/test_transcript_import.mjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/test_transcript_import.mjs:1) | 用合成文本与坐标检查前端成绩单解析器，覆盖 GPA、学分和 PDF 行重建兼容。 | 前端解析检查 |
| [scripts/verify_transcript_parser.mjs](/Users/luoweipotter/Downloads/uci-course-advisor1/scripts/verify_transcript_parser.mjs:1) | 只读实际本地 PDF 样本并输出聚合解析结果，不输出原文和身份字段。 | 前端解析检查 |

## 测试逐文件清单

默认 pytest 配置排除 `live` 与 `manual`，`conftest.py` 阻断意外联网和真实模型。标记为可选浏览器的检查还需要对应环境开关。测试文件存在不代表其中每个场景都已在当前环境执行；本次实际运行的 82 项课程身份相关回归见主导览。

| 文件 | 主要覆盖内容 |
| --- | --- |
| [tests/__init__.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/__init__.py:1) | 测试包入口。 |
| [tests/conftest.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/conftest.py:1) | 全局隔离测试环境、临时存储与课程档案夹具，默认阻断真实网络和模型访问；调试测试行为先看这里。 |
| [tests/fakes/__init__.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/fakes/__init__.py:1) | 测试替身包入口。 |
| [tests/fakes/llm.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/fakes/llm.py:1) | 可编排的离线模型客户端，按预定顺序返回 token 与 tool calls，并记录模型收到的 messages。 |
| [tests/live/test_live_integrations.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/live/test_live_integrations.py:13) | 手动启用的 Anteater 与真实模型连通性冒烟；默认测试集排除。 |
| [tests/test_academic_transcript_import.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_academic_transcript_import.py:76) | 成绩单鉴权、输入白名单、整快照替换、幂等、重复修课、GPA 与已修镜像同步。 |
| [tests/test_agent_deep_search.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_deep_search.py:24) | Agent 深搜工具、访问去重、父链接、深度与页面预算及 URL 审计。 |
| [tests/test_agent_deep_search_history.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_deep_search_history.py:44) | 历史搜索提示进入模型的边界、本轮重新抓取要求、轨迹保存与个人内容排除。 |
| [tests/test_agent_department_restrictions.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_department_restrictions.py:24) | 固定院系限制工具 schema、院系解析、官方查询及相关来源证据交接。 |
| [tests/test_agent_live_websoc.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_live_websoc.py:23) | 实时班次工具参数、学期传递、来源字段与历史查询停止规则。 |
| [tests/test_agent_loop_protocol.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_loop_protocol.py:21) | 模型 token、工具执行、final、工作流以及正常和续答的循环协议。 |
| [tests/test_agent_sse_protocol_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_sse_protocol_characterization.py:37) | chat 到 Agent 的事件转发、预算和卡片合并、首轮错误与本地依据兜底。 |
| [tests/test_agent_tools_schedule_validation.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_tools_schedule_validation.py:63) | 推荐工具的方案校验、未知先修及 Agent 与手动加课冲突一致性。 |
| [tests/test_agent_web_search.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_web_search.py:25) | 搜索工具 schema、调用理由校验、分发和本地化进度。 |
| [tests/test_agent_zotistics.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_agent_zotistics.py:41) | 历史成绩工具证据范围、请求复用、失败状态与课程教授配对。 |
| [tests/test_anteater_payload_preservation.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_anteater_payload_preservation.py:58) | 实际 WebSoc 摘录中的取消状态、所有注释作用域与全部 meetings 在转换后保留。 |
| [tests/test_anteater_term_sync.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_anteater_term_sync.py:31) | Anteater 校历和可用学期参数、班次存在性探查与停止条件。 |
| [tests/test_app_sanity.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_app_sanity.py:9) | 应用路由、后端学期元信息、静态资源缓存与会话默认上下文等基础契约。 |
| [tests/test_auth_ownership_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_auth_ownership_characterization.py:33) | 用户身份来自 cookie，路径 user_id 不越权，会话和聊天不能冒用其他用户。 |
| [tests/test_automatic_term_planning.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_automatic_term_planning.py:47) | 美西时区、第八周、跨年、Spring 跳 Fall、当前与默认学期分离。 |
| [tests/test_catalog_coverage.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_catalog_coverage.py:14) | 目录完整性和过期判断，本地缺项不能证明未开课。 |
| [tests/test_comment_code_normalization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_comment_code_normalization.py:19) | 注释简称规范化及原文路径，不改原注释、不发明课程等价或政策。 |
| [tests/test_controlled_recommendation_browser.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_controlled_recommendation_browser.py:15) | 可选离线真实 Chrome 检查 Continue 替换旧推荐文字与主班卡片。 |
| [tests/test_controlled_recommendation_output.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_controlled_recommendation_output.py:78) | 模型不可靠正文不能进入开启受控模式的推荐输出，预算与续答仍沿用后端证据。 |
| [tests/test_conversation_term_memory.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_conversation_term_memory.py:42) | 会话默认自动学期、拒绝手动值、查询不修改默认标签。 |
| [tests/test_course_comment_agent_pipeline.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_course_comment_agent_pipeline.py:22) | 从公开来源摘录到工具 JSON 和模型 messages 的课程注释及简称链路，拒绝越界学期；模型客户端为替身。 |
| [tests/test_course_identity_intent.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_course_identity_intent.py:45) | 课程关系、旧代码和已有学分问题不会误入推荐；明确混合推荐仍保留推荐路径。 |
| [tests/test_course_normalization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_course_normalization.py:14) | 中英文课号、院系别名与特殊课号解析；聊天、决策和规则复用规范解析器。 |
| [tests/test_data_db_catalog.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_data_db_catalog.py:8) | 本地课程元数据优先、批量返回字段一致、搜索顺序稳定。 |
| [tests/test_deep_search_history.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_deep_search_history.py:32) | 公开搜索轨迹白名单、个人内容排除、本地相似度与聚类次数。 |
| [tests/test_deep_search_page.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_deep_search_page.py:8) | URL 规范、私网目标拒绝、页面提取、重定向和请求日志。 |
| [tests/test_discussion_policy_presentation.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_discussion_policy_presentation.py:38) | 完整官方课表得到的 Dis 规则在推荐、关联课程和展示中的边界。 |
| [tests/test_dynamic_prefilter_cards.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_dynamic_prefilter_cards.py:34) | 动态预筛和后端排名元信息在现有前端卡片中的显示，以及未开启时兼容。 |
| [tests/test_dynamic_prefilter_engine.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_dynamic_prefilter_engine.py:45) | 纯筛选器反例：GE 已完成、软时间偏好、先修、未知班次和硬条件等。 |
| [tests/test_dynamic_prefilter_integration.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_dynamic_prefilter_integration.py:105) | 开关与全候选池、预筛先于排名、工具不能绕过、选中候选补证据。 |
| [tests/test_filter_plan.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_filter_plan.py:35) | 推荐计划的 schema、本轮原句依据、GE 范围和时间硬约束校验。 |
| [tests/test_fixture_contracts.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_fixture_contracts.py:8) | 最小课程、用户档案与会话夹具能被生产加载器读取，测试副本相互隔离。 |
| [tests/test_frontend_static_contract.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_frontend_static_contract.py:35) | 前端 SSE、只读默认学期、懒加载与资源约定；本部分只列位置。 |
| [tests/test_ge_evidence.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_ge_evidence.py:36) | 官方整版 Catalogue 与真实 CSV 的 GE 多标签、学年、来源和缺证据边界。 |
| [tests/test_grade_evidence_pipeline.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_grade_evidence_pipeline.py:17) | 精确课程教授历史、确认无数据才扩大范围、主讲与助教分离、卡片前补证据。 |
| [tests/test_live_websoc_availability.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_live_websoc_availability.py:39) | 实时班次查询和五分钟缓存、强制刷新、限流与最近成功快照。 |
| [tests/test_memory_persistence_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_memory_persistence_characterization.py:18) | 记忆 API 统一通过 manager，旧事实格式、缓存刷新与持久化恢复。 |
| [tests/test_multi_meeting_constraints.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_multi_meeting_constraints.py:38) | 所有上课时段参与冲突和硬时间规则，未知与明确冲突的优先关系。 |
| [tests/test_multi_meeting_presentation.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_multi_meeting_presentation.py:37) | 多时段在推荐卡片、替代班次、受控文字和日历中的保留。 |
| [tests/test_observability.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_observability.py:8) | 日志长度和 URL 保留、trace、readiness 与指标。 |
| [tests/test_observability_prompt_layers.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_observability_prompt_layers.py:6) | token 估算覆盖完整模型上下文，各层只计算一次。 |
| [tests/test_onboarding_catalog.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_onboarding_catalog.py:8) | 全课程选择器优先本地 CSV，并复用重启可用缓存。 |
| [tests/test_pairing_contract.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_pairing_contract.py:40) | 配对证据课程学期来源绑定、完整选中组合、旧布尔与字母不能作验证证明。 |
| [tests/test_pairing_v2.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_pairing_v2.py:75) | 按主班的配对证据、部分关系覆盖、跨作用域拒绝与已选组合。 |
| [tests/test_password_registration.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_password_registration.py:25) | 无需邮件的注册、密码确认、重复邮箱、旧账户数据库兼容及限流。 |
| [tests/test_prerequisite_engine.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prerequisite_engine.py:7) | 先修 OR、最低成绩、缺成绩与考试证据的 met 或 not_met 或 unknown。 |
| [tests/test_private_beta_security.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_private_beta_security.py:9) | 生产密钥、匿名访问策略、隔离访客、CSRF 与安全响应头。 |
| [tests/test_profile_context_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_profile_context_characterization.py:56) | 档案到会话和模型上下文的读取、封装解包与已修字段传递。 |
| [tests/test_prompt_alignment.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_alignment.py:26) | 主提示词与推荐模式开关、GE 证据、目标学期和实际期限保持一致。 |
| [tests/test_prompt_filter_gate.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_filter_gate.py:13) | 未解决意图或缺审计时暂停正式推荐，未知时段保留但不能声称满足硬条件。 |
| [tests/test_prompt_injection_safety.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_injection_safety.py:50) | 应用指令层级、序列化证据与恶意上下文的结构边界；不代表真实模型抗攻击保证。 |
| [tests/test_prompt_runtime_alignment.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_runtime_alignment.py:59) | 普通请求、预算 fallback 与 Continue 都携带当前预筛、工作流和历史执行规则。 |
| [tests/test_prompt_term_range.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_term_range.py:12) | 用户指定历史年份范围优先，最多四年，过大范围澄清而不静默回退。 |
| [tests/test_prompt_tool_contracts.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_prompt_tool_contracts.py:19) | 提示词与工具对教授样本、成绩口径、排名、先修和时间的契约。 |
| [tests/test_pydantic_defaults_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_pydantic_defaults_characterization.py:14) | Pydantic 请求模型不使用可变字段默认值。 |
| [tests/test_query_scope.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_query_scope.py:20) | 本轮学期范围、明确双学期、追问、歧义年份和默认值。 |
| [tests/test_recommendation_cards_persistence_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_cards_persistence_characterization.py:102) | 卡片随 SSE 元数据保存并可从会话恢复，增量读取也可恢复。 |
| [tests/test_recommendation_presentation.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_presentation.py:59) | 确定性推荐文字与卡片事实、实际主班、多 GE 标签、证据缺口与给分口径。 |
| [tests/test_recommendation_public_evidence.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_public_evidence.py:12) | 公开 GE 与目标学期证据，旧卡片恢复规范化及关闭开关时兼容。 |
| [tests/test_recommendation_ranking.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_ranking.py:19) | 纯数学排名权重、样本收缩、缺失特征、并列、无分与配置错误。 |
| [tests/test_recommendation_ranking_integration.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_ranking_integration.py:72) | 全池评分后截断、缓存与本地 RMP 读取、不同开关下旧排序兼容。 |
| [tests/test_recommendation_real_sources.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_real_sources.py:127) | 抓取公开响应回放贯穿 db、预筛、卡片和课表，保留实际证据；不等同实时联网或真实模型。 |
| [tests/test_recommendation_source_badges.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_recommendation_source_badges.py:11) | 卡片来源徽章与证据认证、时效分别展示，未知来源和警告去重。 |
| [tests/test_response_language.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_response_language.py:28) | 本轮语言切换、工具进度与上下文中的语言约束一致。 |
| [tests/test_restriction_timeline.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_restriction_timeline.py:24) | 限制类型、正文块、跨行时间事件、例外与结构化证据完整性。 |
| [tests/test_reverse_course_notices.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_reverse_course_notices.py:14) | 旧课号单独查询时同学期反向发现新候选注释、失败保持未知，并覆盖相邻的先修树和成绩别名修复。 |
| [tests/test_runtime_context.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_runtime_context.py:7) | 后端统一 XML 运行上下文、主消息权限和用户输入隔离。 |
| [tests/test_schedule_mutation_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_schedule_mutation_characterization.py:90) | 手动加删课、班次编号兼容、持久化、实时刷新、并发及可选真实浏览器路径。 |
| [tests/test_schedule_pairing_refresh.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_schedule_pairing_refresh.py:53) | 配对快照从服务端卡片到课表的复用，显式刷新、失败失效与恢复。 |
| [tests/test_scheduling_service.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_scheduling_service.py:19) | 时间格式、日期代码、TBA、同学期冲突、考试与方案校验。 |
| [tests/test_script_entrypoints.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_script_entrypoints.py:28) | 脚本直接执行时能 import app，help 路径不触发网络。 |
| [tests/test_session_continuity_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_session_continuity_characterization.py:49) | 新会话连续两轮沿用同一持久化状态，拒绝旧临时会话 ID。 |
| [tests/test_session_repository_characterization.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_session_repository_characterization.py:6) | 会话 meta、state、turns 的独立存储和旧状态默认回填。 |
| [tests/test_sqlite_evidence_memory.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_sqlite_evidence_memory.py:20) | JSON 迁移一次、带来源召回、偏好检索、冲突版本和软遗忘。 |
| [tests/test_term_resolution.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_term_resolution.py:30) | 学期规范格式、常规季顺序、相对基准、显式与会话解析。 |
| [tests/test_term_state_machine.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_term_state_machine.py:55) | 切换瞬间、公布状态不阻止第八周、停机后跨多季恢复、Summer 不自动进入。 |
| [tests/test_term_state_sync.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_term_state_sync.py:51) | JSON 快照损坏恢复、30 天同步、45 天旧成功数据边界与失败兜底。 |
| [tests/test_unified_term_context.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_unified_term_context.py:45) | current/default/query 分离、忽略前端覆盖、成功或失败查询及跨季历史参考。 |
| [tests/test_web_search_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_web_search_data.py:24) | 搜索提供方配置、来源分类、可信度、查询失败和离线替身。 |
| [tests/test_websoc_co_class_lookup.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_websoc_co_class_lookup.py:87) | 同学期官方 co-class 有界跟进、失败与不完整引用、旧配对规则缓存迁移。 |
| [tests/test_websoc_notice_scopes.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_websoc_notice_scopes.py:11) | 官方 HTML 注释范围、多时段及被排除候选的原始证据保留。 |
| [tests/test_websoc_pairing_producer.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_websoc_pairing_producer.py:38) | 真实公开课表回放生产配对证明，完整行、多个主班、Dis 存在性和部分页面边界。 |
| [tests/test_websoc_payload_pipeline.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_websoc_payload_pipeline.py:63) | 公开响应经工具 JSON、离线 CSV 导入、loader、db 与课表恢复，注释与 meetings 不丢。 |
| [tests/test_websoc_workflow.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_websoc_workflow.py:19) | 官方表单 POST、开课与无匹配判定、不可用状态、限制说明和允许链接抓取。 |
| [tests/test_workflow_router.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_workflow_router.py:24) | 固定工作流识别、后端学期与院系解析、主工具执行计划与澄清。 |
| [tests/test_zotistics_cards.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_zotistics_cards.py:69) | 生产前端成绩图表的统计口径、缺数据、真实零值、P/NP 与原站 GPA 区分。 |
| [tests/test_zotistics_data.py](/Users/luoweipotter/Downloads/uci-course-advisor1/tests/test_zotistics_data.py:94) | Zotistics 原站结构解析、姓名与院系消歧、查询范围、图表、缓存和错误。 |

## 后端运行与验证配置

| 文件 | 作用 |
| --- | --- |
| [AGENTS.md](/Users/luoweipotter/Downloads/uci-course-advisor1/AGENTS.md:1) | 工程约定，特别要求学期改动先读 term-rules。 |
| [docs/term-rules.md](/Users/luoweipotter/Downloads/uci-course-advisor1/docs/term-rules.md:1) | 六条已确认学期规则、实现位置、已知反例与相关回归命令；规则不由现有缺口反向定义。 |
| [docs/current-agent-system-prompt.txt](/Users/luoweipotter/Downloads/uci-course-advisor1/docs/current-agent-system-prompt.txt:1) | 供人核对的主 Agent 基础 prompt 文档；运行时基础内容来自 app/llm/adapter.py 的 AGENT_SYSTEM_PROMPT。 |
| [README.md](/Users/luoweipotter/Downloads/uci-course-advisor1/README.md:1) | 运行方式、数据准备、功能边界与验证命令；其中提到的旧路径需与当前源码核对。 |
| [ROADMAP.md](/Users/luoweipotter/Downloads/uci-course-advisor1/ROADMAP.md:1) | 开发阶段与计划，不是运行时执行器。 |
| [SKILL.md](/Users/luoweipotter/Downloads/uci-course-advisor1/SKILL.md:1) | 仓库保存的选课助手行为说明，不是一个自动被 FastAPI 执行的 Python 模块。 |
| [Procfile](/Users/luoweipotter/Downloads/uci-course-advisor1/Procfile:1) | 部署进程命令，通过 Uvicorn 启动 main:app。 |
| [.python-version](/Users/luoweipotter/Downloads/uci-course-advisor1/.python-version:1) | 项目指定的 Python 版本。 |
| [requirements.txt](/Users/luoweipotter/Downloads/uci-course-advisor1/requirements.txt:1) | 后端运行依赖。 |
| [requirements-dev.txt](/Users/luoweipotter/Downloads/uci-course-advisor1/requirements-dev.txt:1) | 开发、测试与检查依赖。 |
| [pytest.ini](/Users/luoweipotter/Downloads/uci-course-advisor1/pytest.ini:1) | 测试发现、默认标记、覆盖率与严格配置。 |
| [.coveragerc](/Users/luoweipotter/Downloads/uci-course-advisor1/.coveragerc:1) | 覆盖率统计范围与排除规则。 |
| [.github/workflows/ci.yml](/Users/luoweipotter/Downloads/uci-course-advisor1/.github/workflows/ci.yml:1) | 持续集成中的依赖安装、语法检查、依赖检查和离线测试。 |
| [.env.example](/Users/luoweipotter/Downloads/uci-course-advisor1/.env.example:1) | 环境变量样例；实际进程配置由环境注入，主流程与独立脚本各自读取方式需看实现。 |
| [.gitignore](/Users/luoweipotter/Downloads/uci-course-advisor1/.gitignore:1) | 版本控制忽略范围；被忽略文件仍可能存在于当前工作区，本次 app 清单没有只按 Git 已追踪文件取样。 |
| [package.json](/Users/luoweipotter/Downloads/uci-course-advisor1/package.json:1) | 前端 PDF.js 包依赖；不是 Python 后端业务入口。 |
| [pnpm-lock.yaml](/Users/luoweipotter/Downloads/uci-course-advisor1/pnpm-lock.yaml:1) | 对应前端包解析锁定信息。 |

## 数据与运行状态去哪里找

| 位置 | 用途与读取方 |
| --- | --- |
| `data/uci/courses.csv` | 课程元数据、先修树、目录标签；由 CSV loader 与选课初始化数据接口读取。 |
| `data/uci/sections.csv` | 带 term_id 的班次、状态、meeting 与来源快照；由 CSV loader 读取。 |
| `data/uci/section_instructors.csv` | 班次与教授关联。 |
| `data/uci/section_ge.csv` | 班次的本地 GE 标签；不能单凭此认定目标学年官方认证。 |
| `data/professor/` | RMP 档案快照、评论 ndjson 与评论 SQLite，以及覆盖检查报告。 |
| `data/uci_general/major_requirements_raw/` | 抓取的专业要求原文与课程引用 JSON；人工专业规则找不到时的回退来源。 |
| `data/memory/<user>/sessions/<session>/` | `meta.json`、`state.json`、`turns.jsonl`，由 sessions 存储层维护。 |
| `data/memory/long_term_memory.db` | 默认 SQLite 长期记忆路径，可配置；同目录的旧用户 JSON 可作为迁移输入。 |
| `data/academic.db` | 默认结构化学业 SQLite 路径，可配置。 |
| `data/auth.db` | `auth/store.py` 当前通过模块常量 `DB_PATH` 指向此处，账户记录与记忆和学业库分开；这里没有独立的 `AUTH_DB_PATH` 环境变量入口。 |
| `data/runtime/` | 自动学期、全课程列表、GE 等运行缓存；来源、时效和未知状态由各服务解释。 |
| `tests/fixtures/` | 回归样本与来源回放，不作为真实用户数据。 |
| `artifacts/` | 历次验收的日志、截图或统计，不参与在线业务实现。 |
| `.local-backups/` | 变更前副本与恢复材料，不算当前运行实现。 |

修改一个功能时，先在主导览确定职责文件，再从本表找对应测试。新增反例应说明“输入是什么、错误结果是什么、期望规则是什么”，避免只测试函数能否返回而没有约束实际业务。
