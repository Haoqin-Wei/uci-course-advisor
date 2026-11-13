# 课程注释识别修复与验收（2026-11-09）

## 修改范围

- 课程关系、更名、既有学分能否再修的事实问题走 `single_query`。意图规则与推荐预筛使用同一判断，不因“两门课＋和／compare”误启用推荐展示。明确要求推荐或选择的混合问题仍保留推荐路径；否定、引用的推荐词不算明确推荐请求。
- 工具整理结果增加 `course_code_normalizations`：原文简称、规范课程代码、`existing_department_alias_rules` 依据、原始注释 JSON pointer。沿用现有 INF → IN4MATX 等院系简称规则，不新建课程身份表，不删除或重连课程 ID。
- 所有原始注释与 meetings 保持不变。更名或禁修关系由原注释提供，简称规范化本身不提供关系或权限。`ICS COURSES 5` 等带无关词的标题不生成课程简称依据。
- 事实查询通过 `get_live_sections` 获取 Anteater/WebSoc 原始注释；历史 `get_sections` 的 HTML 提取结果可能没有结构化课程注释。回答区分“曾以旧编号开设”和“已有旧课学分不得再修”，不自动推断抵扣、注册拦截、成绩单记账或生效日期。此类回答使用聚焦的事实格式。
- Winter/Fall 查询目标保持独立；不修改默认学期标签、六条学期规则或既有时间解析缺口。

实现入口：`app/llm/intent_rules.py`、`app/agent/prefilter.py`、`app/data/websoc_payload.py`、`app/agent/tools.py`、`app/llm/adapter.py`。当前提示词快照已同步。

## 离线验证

```sh
.venv/bin/python -m pytest --no-cov -q
```

最终结果：**1501 passed, 5 skipped, 2 deselected**。跳过的是五项显式启用的 Chrome 检查；默认排除的 live/manual 项不计入通过数。该命令包含 `term-rules.md` 列出的八组相关回归。

新增回归覆盖中英文事实查询、混合选择、否定与引用、别名证据、未知简称、误识别标题、空注释、无班次注释、作用域、全部 meetings、重复整理不变，以及真实 adapter/agent 工具消息中的证据与学期限制。脚本模型用于验证传递与分流，不用于证明模型理解正确。

## 真实模型与真实来源

模型：`deepseek-v4-flash`，两个推荐配置开关均保持启用。使用隔离的合成学生上下文和当前应用提示词；不覆盖业务逻辑，不伪造公开来源。运行范围为真实 adapter → agent → 实际工具 → 配置模型；显式传入本问题的两个已解析查询学期，不属于浏览器或完整 HTTP 聊天入口验收。

问题：

> 请核实 SWE43 和 IN4MATX43 是什么关系，并说明依据。假设我在 2026 Winter 修过 IN4MATX43，而且已经获得这门课的学分，还能在 2026 Fall 再修 SWE43 吗？请根据课程说明回答。

全部尝试保留于 `artifacts/solon-course-comment-fix-2026-11-09/`：

| 运行 | 模型请求 | 结果 |
| --- | ---: | --- |
| run-01 | 6 | 分流已修复，但历史 HTML 结果没有结构化的 INF 简称依据，回答仍含关系不确定与额外推论；未通过。 |
| run-02 | 4 | 原始 API 注释和别名依据已到达模型，核心关系与禁修结论正确；仍扩展到截止日期和学分记账，回答范围未通过。 |
| run-03 | 2 | 正确连接注释中的 INF43 与 IN4MATX43，并根据 `prior credit … may not take` 回答不能再修 SWE43；没有再扩展截止日期或断言成绩单记账。核心验收通过。 |

[最终模型原始回答](../artifacts/solon-course-comment-fix-2026-11-09/run-03/answer.txt) 保留原文，没有人工替换。答案中的“旧用／现用”简称措辞不作为简称使用年代的证据；程序提供的是别名拼写，未建立 INF → IN4MATX → SWE 的三代课程历史，也未确认更名生效日期。本次成功不保证每次模型生成都相同。

最终运行没有启用受控推荐输出，未触发推荐筛选或生成推荐卡片，默认学期保持不变。实际输入包含两次 HTTP 200 的原始 Anteater WebSoc 查询：2026 Fall SWE43、2026 Winter IN4MATX43。

使用 `audit_live_payload.py run-03` 把 HTTP 原始响应逐字段对比模型实际收到的 tool message：

| 查询 | 注释字段 | meetings 数组 | 结果 |
| --- | ---: | ---: | --- |
| 2026 Fall SWE43 | 18 | 13 | 全部保持原值；INF43 → IN4MATX43 依据可见 |
| 2026 Winter IN4MATX43 | 12 | 7 | 全部保持原值；空 course comment 也保留 |

逐项报告：[live-payload-verification.json](../artifacts/solon-course-comment-fix-2026-11-09/run-03/live-payload-verification.json)。

## 真实模型的合成边界阅读检查

另用同一真实模型和当前提示词做两次阅读检查。输入明确标为**合成样本**，没有将其冒充 API 返回，也没有自动执行工具；这些检查不能代替应用完整路径验收。

| 给定证据 | 实际回答 |
| --- | --- |
| 仅 `Previously offered as INF 43` 与已知简称依据 | 能说明旧编号关系，但指出没有禁修条款，无法确认是否能再修。 |
| course comment 为空 | 关系与能否再修均无法确认；明确说明空注释不等于没有限制。 |

原始回答见 `artifacts/solon-course-comment-fix-2026-11-09/synthetic-boundaries/`。全部尝试、请求、公开来源响应和验证报告已保存；凭据与模型内部推理不写入这些交付记录。
