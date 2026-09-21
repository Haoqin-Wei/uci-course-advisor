# 学期规则与实现核对

用户确认日期：2026-09-20。本文是学期行为的工程规范；后续修改以这六条规则为依据。下方的实现核对单独记录现状，已知缺口不代表规则发生改变。

## 一、默认学期按美西时间和 UCI 校历计算

- 使用 `America/Los_Angeles`，由时区库处理夏令时，不使用浏览器时区或固定 UTC 偏移。
- 第八教学周之前，默认学期为正在进行的常规学期；进入第八周时切换到下一个常规学期。
- 切换点固定为美西时间第八教学周周一 **00:00**。
- 常规学期顺序为 Fall → Winter → Spring → Fall，Spring 直接跳到同年的 Fall。
- 假期保持即将开始的常规学期。目标学期课表是否发布，不影响默认学期的日期切换。
- 根据 instruction start 计算教学周。现有算法把开课日当天或之后的第一个周一作为 Week 1，再加七周；Fall 开课后的短周按 Week 0 处理。
- `current_term` 表示实际正在进行的学期，`default_term` 表示规划默认学期。第八周后两者可以不同；假期内可以没有 `current_term`。

官方依据：[UCI Enrollment Windows](https://www.reg.uci.edu/enrollment/windows.html) 说明选课从第八教学周周一开始，个人窗口在美西时间 07:00–19:00 开放。**00:00 是本产品的默认学期切换时间，不是声称 WebReg 在午夜开放。**

例如，[2026–27 官方校历](https://reg.uci.edu/calendars/quarterly/2026-2027/quarterly26-27.html) 列出 Fall 2026 于 9 月 24 日开始上课，Winter 2027 选课于 11 月 16 日开始。按上述规则，默认学期在 `2026-11-16T00:00:00-08:00` 从 `2026 Fall` 切到 `2027 Winter`，实际正在进行的学期仍为 Fall。

## 二、本轮时间表达 > 相关对话 > 默认学期

1. 本轮明确的年份、季节或相对时间优先，例如 `2025 Winter`、`去年 Winter`、`本学期`、`下学期`。同一问题包含多个时间表达时，必须完整解析，不能只保留其中一个。
2. 没有新的时间表达且确实是相关追问时，沿用对话中的查询目标。例如上一问查询 ICS 33 的 2025 Winter，再问“那 ICS 32 呢”，仍查询 2025 Winter。
3. 无关的新问题重新判断，不能因为同一会话里出现过历史学期，就沿用那个年份。
4. 缺少年份的 `Winter`，结合相关上下文及过去／未来语气推断；没有其他线索时使用即将到来的 Winter，并在回答中明确年份。不能把推断年份伪装成用户明确指定的年份。
5. “本学期”使用实际正在进行的 `current_term`，不能被第八周之后已经前进的规划默认学期替代。没有正在进行的常规学期时，应说明或澄清。

实现中的角色必须分开：

| 字段 | 含义 | 是否能改变默认标签 |
| --- | --- | --- |
| `current_term` | 实际正在进行的常规学期 | 否 |
| `default_term` / `automatic_term` | 校历和时钟计算出的规划默认学期 | 自动同步时可以 |
| `query_terms` | 本轮要查询的一个或多个目标学期 | 否 |
| `recent_query_focus` | 供相关追问使用的对话查询目标 | 否 |
| `historical_reference` | 为目标学期提供依据的历史记录 | 否，也不能替代查询目标 |

## 三、跨学期比较固定围绕开课与教授

- 按目标学期分别查询，不把一个学期的结果复制到其他学期。
- 输出“学期｜是否开课｜授课教授”表格；表头和说明跟随用户语言。
- 同一学期有多位授课教授时完整列出并去重。未公布的教授标注待定，不能使用历史教授补齐目标学期。
- 讨论课助教不能被误写成主讲教授。
- 没有额外请求时，不扩展为评分、成绩、上课时间、先修资格或推荐卡片分析。
- 数据不可用与确认未开课是不同状态。只有可靠的官方无匹配证据才支持“未开课”；超时、解析失败和本地目录没有记录只能表示暂时无法确认。

## 四、开课规律默认参考近两年

- “是不是只在 Winter 开”必须同时检查 Fall、Winter、Spring，不能仅查询 Winter。
- 现有工程以最近六个已结束的常规学期表示默认两年窗口，不把正在进行或未来学期当成已完成历史。
- 回答限定为“近两年记录显示……”，并可列出实际查询范围；不能据此宣称永久只在某季开设。
- 只有其他季节均有可靠的未开课证据，才能得出这个历史窗口内的排他性结论。未知或缺失记录不能当成未开课。
- **用户明确指定其他年份或年份范围时，使用指定范围，不回退到默认近两年。** 对指定年份内的季节规律，检查该范围的 Fall、Winter、Spring。无法可靠解析时先澄清，不静默忽略年份。

## 五、未来课表未公布时使用历史依据

- 先检查目标学期。只有官方信息支持“未来课表未公布”时，才进入这个分支；网络错误不能被解释为未公布。
- 查询目标季节前两年的同季节，例如 `2027 Winter` 对应 `2025 Winter` 和 `2026 Winter`。
- 两年均确认开设时，可以回答：“近两年 Winter 均开设，2027 Winter 可能继续开设，具体以官方课表为准。”此句是格式示例，不代表已经核实任意课程。
- 只查到一年开设或另一个年份无法确认时，准确说明证据覆盖，不能说“两年均开设”。当前实现允许根据至少一年记录给出明确标注依据的“可能开设”，这不等于目标学期已确认。
- 目标学期教授以目标学期公布的信息为准；历史教授可作为单独标注的参考，不预测未来教授、课时、名额或班次代码。
- 历史依据不改变目标学期、对话查询目标或默认标签。

## 六、默认学期标签只读

- 提问框中的 `@ Fall 2026` 和其他默认学期标签复用后端默认学期，只展示，不提供手动学期选择。
- 查询历史学期、跨学期比较、沿用上一问，都不改变默认学期。
- 标签不能被拼接进用户输入或作为前端选定学期覆盖后端判断。
- 在后端切换时刻、页面恢复可见、会话恢复等同步场景中，读取自动默认值更新展示。
- 课表视图中切换查看某个学期，与修改对话默认学期是两件不同的事。

## 2026-09-20 实现核对

本次核对范围为仓库代码、现有离线测试及确定性解析复现，不代表对线上部署或每次真实模型输出的保证。本次仅补充工程文档，未修改时间解析逻辑。

| 规则 | 当前状态 | 主要实现与证据 |
| --- | --- | --- |
| 1. 美西时间、第八周、跳过 Summer、假期保持 | 已实现，有时区和边界测试 | `app/terms/clock.py`、`calendar.py`、`service.py`；`test_week8_uses_teaching_weeks_and_pacific_dst`、`test_utc_boundary_changes_default_without_publication_and_keeps_current`、`test_spring_skips_summer_and_breaks_keep_upcoming_default` |
| 2. 时间优先级、相关追问、无年份推断 | 主流程已实现，存在下列解析缺口 | `app/terms/query_scope.py`、`intent.py`、`app/routers/chat.py`；`tests/test_unified_term_context.py` |
| 3. 分学期查询、教授聚合、三列表格 | 数据查询和聚合已实现；表格由提示词约束 | `app/data/offerings.py`、`app/agent/loop.py`、`app/llm/adapter.py` 的 Term resolution and offering evidence 段；`test_comparison_fetches_both_terms_before_model_answer`。目前没有服务端强制表格渲染器 |
| 4. 两年多季节历史 | 默认六学期已实现；自定义年份范围有缺口 | `recent_regular_terms`、`resolve_query_scope`；`test_only_winter_question_checks_other_seasons_and_two_completed_years` |
| 5. 未公布与历史依据分离 | 已实现，有未公布／故障区分和教授不预测测试 | `app/data/offerings.py`；`test_unpublished_future_uses_same_season_history_without_predicting_professor`、`test_future_history_requires_publication_evidence_through_full_fallback` |
| 6. 默认标签只读 | 已实现 | `app/terms/conversation.py`、`static/js/api-client.js`；`test_query_resolution_never_changes_default_term`、`test_manual_default_is_rejected_without_mutating_session`、`test_frontend_default_term_is_read_only_and_query_scoped` |

### 已复现、待修正的解析缺口

共同复现条件：美西时间 `2026-11-20 12:00`，当前学期 `2026 Fall`，默认学期 `2027 Winter`；旧对话目标为 ICS 33 的 `2025 Winter`。以下来自确定性解析器，后续语义步骤会跳过这些 `inferred`、`history` 或 `explicit` 结果，不能依靠模型自动补救。

| 输入 | 目前结果 | 按规范应有结果 |
| --- | --- | --- |
| 无关新问题：`MATH 2A Winter 开课情况` | 沿用旧对话年份，得到 `2025 Winter` | 重新判断为 `2027 Winter`，说明年份是推断的 |
| `去年 Winter ICS33 通常开吗` | 扩展为默认六学期历史 | 本轮“去年 Winter”优先，查询 `2025 Winter` |
| `ICS33 在 2023 至 2024 年是不是只在 Winter 开` | 忽略指定范围，查询 `2024 Fall` 至 `2026 Spring` 六学期 | 查询 2023、2024 各年的 Fall、Winter、Spring |
| `比较 ICS33 在 2025 Winter 和去年 Fall 的教授` | 只查询 `2025 Winter` | 同时查询 `2025 Winter` 和 `2025 Fall` |

相关原因：`resolve_query_scope` 对未标年份的季节直接使用旧 focus；历史关键词分支先于相对年份分支；已有明确学期时提前返回；自定义历史年份范围尚未完整展开。修正时应为上述反例增加回归测试，不应通过放宽工具学期限制掩盖解析问题。

现有相关回归在本次核对中 **134 项通过**，但并未覆盖上面四个反例；因此不能把“现有测试全通过”表述为六条规则在所有边界上均已实现。

### 维护入口与验证

2026-09-21 显示偏好更新：周课表默认覆盖 08:00–22:00，末尾显示 22:00 边界；若已有真实班次在范围外，继续扩展显示以避免隐藏已选课程。第四栏检查当前所看学期的已选课：周五中午 12:00 后无课且时间信息完整时显示 `Free` 和随机庆祝／笑脸 emoji（每次加载页面选择一次，重绘时保持稳定）。有课显示 `Busy`，存在时间待定班次时仍为 `Unknown`；只改变展示，不改变默认学期或选课记录。

2026-09-21 课表显示修复：Schedule 只展示已加入的教学班，取消 Suggested 预览；TBA／时间不可用的班次在周课表上方单独显示，不推测时段。服务端刷新得到的同一教学班快照优先于旧目录，日历与冲突校验使用相同时间；学期仍取每条选课记录的 `term`，不改变对话默认标签。回归包括有时间的讲座／讨论课、TBA 保留、旧 TBA 被实际时间替换及实际时间变回 TBA、跨学期隔离和浏览器加号交互；不涉及上表待修正的时间表达解析缺口。

同日补充核对：上一轮模拟班次只覆盖标准 24 小时时间，未覆盖 Registrar 原始格式。实际会话中的 45C / 36120 保存为 `12:30`–`1:50p`，卡片可显示但日历解析失败。现统一解析 WebSoc 范围的共享上午／下午后缀，并兼容已保存的旧卡片和刷新快照；例如 `12:30–1:50p` → `12:30–13:50`、`3:30–4:50p` → `15:30–16:50`，跨正午的 `11:00–12:20p` 保持上午 11 点。真实 TBA 仍不生成时段。新增 `tests/fixtures/schedule/registrar_45c_legacy.json` 保存公开班次字段作为回归样本。

课表添加／删除／恢复复用**当前用户、当前会话、同一目标学期**中服务端生成的卡片，每个请求的兜底目录查询按课程和学期缓存；不接受客户端伪造的上课时间。显式刷新仍获取实时数据。同步读取不再阻塞 ASGI 事件循环，同一会话的写入串行处理；刷新结束时保留等待期间的新增／删除。浏览器回归 `SOLON_BROWSER_CHECK=1 ... pytest tests/test_schedule_mutation_characterization.py -k real_api_browser --no-cov -q -s` 使用真实临时 FastAPI 接口验证原始时间格式、加号、课表坐标、刷新恢复及删除，外部课程请求被禁止。

修改日历、默认标签、时间解析、工具范围、历史推断或相关提示词前，先阅读本文件。变更已确认规则时同步更新本文、README 和相应测试；修复实现缺口时更新核对状态并保留反例回归。

```sh
.venv/bin/python -m pytest \
  tests/test_automatic_term_planning.py \
  tests/test_term_resolution.py \
  tests/test_term_state_machine.py \
  tests/test_term_state_sync.py \
  tests/test_anteater_term_sync.py \
  tests/test_unified_term_context.py \
  tests/test_conversation_term_memory.py \
  tests/test_frontend_static_contract.py --no-cov -q
```
