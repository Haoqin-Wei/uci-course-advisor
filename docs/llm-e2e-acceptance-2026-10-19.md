# 真实 LLM 本地端到端验收 · 2026-10-19

**结论：动态预筛验收未通过，不建议正式启用。** 五例都正常完成网页提交、真实模型调用、卡片展示和刷新恢复；但首轮筛选计划 0/5 通过，均回退到默认计划。随后串行仅复查解析，仍为相同的两例 Schema 错误、三例超时。串行复查不是额外五例端到端通过。

## 测试环境与边界

- 实际模型：`deepseek-v4-flash`，使用项目现有配置；没有模型或本地 API mock。
- 仅临时 localhost 进程开启两个功能开关；真实登录五个独立测试账号，使用相同合成学生档案，避免跨案例记忆污染。
- 合成档案：CS 大三，已修 ICS 31/32/33、MATH 2A/2B，在修列表为空；没有注入可信 GE 毕业审计，也没有使用真实学生记录。
- 无指定学期，五例实际查询均为 `2026 Fall`。没有替换历史学期、篡改时钟、放宽 8 秒限制或修改 Prompt 来通过测试。
- 课程／教授使用现有真实目录和 RMP 公开快照；WebSoc、Zotistics 等按实际工具联网补充。RMP 不代表实时重抓；没有数据时保持缺失。
- 本地 Fall 2026 班次覆盖主要为 COMPSCI，但官方 WebSoc 实际补回了多门 GE 开课记录；本地缺失没有被直接当成未开课。
- 临时服务已停止。正常 `.env` 和全部原有源码校验值未变；未部署、未正式开启功能、未改正常用户数据路径。

## 逐例总表

分数为后端实际输出、满分 100；“未评分”表示证据不足，不是 0 分。这些是系统实际提出的候选，不表示已满足所有用户条件。

| 案例 | 输入 | 实际筛选计划／原因 | 整课排除 | 实际推荐结果（分） |
| --- | --- | --- | --- | --- |
| C1 | GE 已经修完了，我想找容易拿 A 的课，请推荐 3 门。 | 回退默认计划：Schema 不符 | 0 门 | PUBHLTH 1（76.3）；I&C SCI 5（未评分）；ART HIS 55（未评分） |
| C2 | 推荐 3 门简单的 GE 课程。 | 回退默认计划：解析超时 | 0 门 | SOCIOL 1（83.0）；PUBHLTH 1（76.3）；ART HIS 55（未评分） |
| C3 | 只推荐能补齐我剩余 GE 要求的课，给我 3 门建议。 | 回退默认计划：解析超时 | 0 门 | I&C SCI 139W（64.3）；ANTHRO 2A（62.9）；PHILOS 5（未评分） |
| C4 | 请推荐 3 门课，周五绝对不能上课，其他时间都可以。 | 回退默认计划：解析超时 | 0 门 | PHILOS 3（69.6）；ANTHRO 2A（62.9）；I&C SCI 45C（未评分） |
| C5 | 请推荐 3 门课，最好不要早八。 | 回退默认计划：Schema 不符 | 0 门 | MATH 3A（64.3）；I&C SCI 45C（未评分）；STATS 67（未评分） |

所有案例的实际计划均为 `goal=balanced`、`course_scope=any`、`only_unmet_ge=false`、`excluded_weekdays=[]`、`earliest_start=null`，高 A 和避早课偏好均为 false。初始池均为 1562 门，保留 1562 门；初始 452 个班次仅去掉 1 个已取消班次，与本次用户意图无关。不能把这些零排除解释为动态筛选正确。

## 关键失败与已验证部分

1. **筛选计划契约不稳定。** C1/C5 将推荐数量原文放成字符串证据，Schema 要求 `{field,quote,source}` 对象。严格校验后整份计划回退，原本正确的 GPA 目标／避早课偏好也丢失。
2. **解析超时。** C2/C3/C4 触发现有 8 秒限制。串行复查同样失败；不能仅归因于浏览器并发。
3. **硬条件降级后回答仍作保证。** C4 未启用周五筛选；ANTHRO 2A 卡片仍含周五讨论课 60011/60012/60030/60031。PHILOS 3 讲座／讨论课时间为 TBA，回答却写“物理上不可能撞周五”。TBA 不能证明无周五课程。
4. **GE 缺项仍被猜测。** C3 明知没有官方 GE 审计，仍以“最可能仍缺”挑选类别。虽然回答附有不确定性说明，仍不能验收为只推荐剩余 GE 的实现。
5. **回答与来源口径不完全一致。** C1 将 Uban 的估算 GPA 3.659 当普通平均 GPA 使用，未说明估算；原站报告值为 3.633。又把 `class_count=3` 写成 3 个开课学期，实际只有 2020 Fall、2022 Fall 两个不同学期。
6. **上下文偏大。** 初始注入全部 1562 个候选及 1546 个待核实记录；日志中的约 168k 输入是本地字符换算估计，不是服务商实际计费 token。该注入发生在解析之后，不能直接解释解析器超时。

已验证：5/5 真实浏览器和 SSE 流完成；5/5 刷新后答案、卡片、预筛报告、排名、DOM 和默认学期标签一致，无页面 JS 错误。所有非空推荐分都能由保存的特征和有效权重重算。PUBHLTH 1 / Uban 的 76.3 分与 SOCIOL 1 / Carian 的 83.0 分确有 Zotistics 配对 A 率和 RMP 两维证据；多人主讲且无可靠合并证据时保持无分。PHILOS 3、ICS 139W 当前分数仅使用 RMP；ANTHRO 2A / Olson 使用教授跨课程历史汇总并标明该范围。

因此，本轮没有形成可用于验证“按意图先排除，再减少成绩／评论请求”的有效计划；请求量减少和误删／召回指标不应宣称通过。

## 每例完整计划、排除和推荐证据

### C1

输入：GE 已经修完了，我想找容易拿 A 的课，请推荐 3 门。

预期：GPA 优化、高 A 偏好；不因 GE 已完成而排除其他 GE。

实际：原始 JSON 的 GPA 目标和高 A 偏好正确，但 explicit_constraints 含字符串“推荐 3 门”，应为结构化对象；整份计划被退回默认值。

整例耗时（含登录、展示、截图及刷新恢复）：79.5 秒。解析耗时：3700.2 ms。

实际校验后计划：

```json
{
  "plan": {
    "goal": "balanced",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": false,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": ""
  },
  "status": "fallback",
  "issues": [
    "model_invalid_schema"
  ],
  "metrics": {
    "parse_elapsed_ms": 3700.2,
    "llm_calls": 1,
    "failure_reason": "model_invalid_schema"
  }
}
```

排除：0 门；最终保留 1562 门。由于解析降级，没有按该案例的选课目标排除课程。

| 课程 | 当前主讲 | 推荐分 | 有效特征 | A 证据范围 |
| --- | --- | ---: | --- | --- |
| PUBHLTH 1 | UBAN, K. | 76.3 | A 率 + RMP | 教授＋本课程 |
| I&C SCI 5 | TOMLINSON, W., LI, W., STAFF | 未评分 | 无可用特征 | 缺失 |
| ART HIS 55 | BETANCOURT, R., GRISSOM, H., D'AMBROSIO, V. | 未评分 | 无可用特征 | 缺失 |

真实页面刷新恢复：答案、卡片、预筛报告、排名和默认学期标签全部一致。

[完整 SSE、回答、卡片与恢复比对 JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C1.json)；[实际调用和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/traces/a7bed94d671b4cb5923c1f5ba4190d58.jsonl)

<details>
<summary>展开系统实际回答原文</summary>

**2026 Fall 选课 · 三门高 A 率课程**

> **结论：推荐。** 三门在 Fall 2026 都有可入 section，历史 A 率分别为 77%、69%、85%，且都不设先修课。

──────────────────────────────────

**PUBHLTH 1 · Principles of Public Health（4 学分）**

**数据**（Zotistics 历史成绩，统计截至 2023 Spring）

| 指标 | 数值 |
|---|---|
| 秋季主讲（A 班） | **Uban, K.（线上，TBA）** |
| 该师 A 率 | **77.1%** |
| 该师平均 GPA | 3.659 |
| A–F 有效样本 | 994 份 / 3 个开课学期 |
| 课程全量 A 率 | 60.9%（10,206 份，55 个班） |
| 另一 section | B 班 Farmer, S.，TuTh 2:00–3:20，A 率 67.6% |

教学口碑：RMP 上 33 条评分均分 4.5，整体属于好评如潮（该评价仅覆盖教学口碑，不构成给分承诺）。

──────────────────────────────────

**I&C SCI 5 · Global Disruption and Information Technology（4 学分）**

**数据**（Zotistics 历史成绩）

| 指标 | 数值 |
|---|---|
| 授课 | **Tomlinson, W.**（线上，TBA，section 35910） |
| 该师 A 率 | **68.6%** |
| 该师平均 GPA | 3.538 |
| A–F 有效样本 | 2,114 份 / 9 个开课学期（2015–2022） |
| 课程全量 A 率 | 66.3%（3,505 份，17 个班） |

我这边没有 Tomlinson 的 RMP 评分数据，所以只能提供上面的历史给分证据，不能评价讲得好不好。这门课内容对你（CS 大三）基本是零新知识，属于纯“保 GPA 学分”。

──────────────────────────────────

**ART HIS 55 · Disneyland（5 学分）**

**数据**（Zotistics 历史成绩）

| 指标 | 数值 |
|---|---|
| 授课 | **Betancourt, R.** + Grissom, H. + D'Ambrosio, V. |
| A 率 | **85.0%** |
| 平均 GPA | 3.816 |
| A–F 有效样本 | 559 份 / 仅 4 个班 |
| 覆盖区间 | 2020 Spring – 2021 Summer |

**风险点：** 这份样本只有 4 个班且全部落在疫情期（当时给分普遍宽松），加之本学期换成三人合开，85% 不能直接套到 Fall 2026。RMP 上 Betancourt 10 条评分均分 3.3，属于褒贬不一。想尝鲜、能接受不确定性可以选；想稳则用下面的备选。

──────────────────────────────────

**建议**

三门合计 13 学分（4+4+5），工作量结构上偏轻，且都不占用你已修完的 GE 名额，纯粹作为通往 180 学分的 elective。稳妥顺序是 PUBHLTH 1 ≈ I&C SCI 5 > ART HIS 55。若想换掉 ART HIS 55，**LPS 29**（Heis, J.，MWF 10:00，63.4% A / 3.406，1,071 份样本 / 18 个班，但需另配 1 个 Dis）历史记录更长、主讲人更稳定。

另需提醒：Fall 2026 已经过了第 1–2 周的自由加退课窗口，现在加课须在 Student Access 走 Enrollment Exceptions，需你主修 dean 与开课学院 dean 双重批准（IR 440）。历史成绩仅为过往记录，不保证未来给分。

**下一步：** 要不要我按“纯线上 / 无 discussion”这个条件，把这三门再优化一遍？

</details>

### C2

输入：推荐 3 门简单的 GE 课程。

预期：GE 课程范围；不自动启用只补未完成 GE。

实际：8 秒解析超时，没有可供校验的筛选计划；GE 范围没有进入实际预筛。

整例耗时（含登录、展示、截图及刷新恢复）：89.1 秒。解析耗时：8005.9 ms。

实际校验后计划：

```json
{
  "plan": {
    "goal": "balanced",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": false,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": ""
  },
  "status": "fallback",
  "issues": [
    "model_timeout"
  ],
  "metrics": {
    "parse_elapsed_ms": 8005.9,
    "llm_calls": 1,
    "failure_reason": "model_timeout"
  }
}
```

排除：0 门；最终保留 1562 门。由于解析降级，没有按该案例的选课目标排除课程。

| 课程 | 当前主讲 | 推荐分 | 有效特征 | A 证据范围 |
| --- | --- | ---: | --- | --- |
| SOCIOL 1 | CARIAN, E. | 83.0 | A 率 + RMP | 教授＋本课程 |
| PUBHLTH 1 | UBAN, K. | 76.3 | A 率 + RMP | 教授＋本课程 |
| ART HIS 55 | BETANCOURT, R., GRISSOM, H., D'AMBROSIO, V. | 未评分 | 无可用特征 | 缺失 |

真实页面刷新恢复：答案、卡片、预筛报告、排名和默认学期标签全部一致。

[完整 SSE、回答、卡片与恢复比对 JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C2.json)；[实际调用和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/traces/b282915a36174a2e958e4d6f08358043.jsonl)

<details>
<summary>展开系统实际回答原文</summary>

**Fall 2026 GE 推荐 · 3 门历史给分最宽松且已确认开课的课**

> **结论：推荐。** 三门都在官方课表确认 Fall 2026 开课，历史 A 比例 60–85%，且时间互不冲突。

──────────────────────────────────

**ART HIS 55 · Disneyland**（5 学分 · GE II + GE IV · 无讨论课）

| 指标 | 数值 |
|---|---|
| 历史 A 比例 | **85.0%** |
| 历史平均 GPA | 3.82（近似值，无 +/-） |
| 样本量 | 559 条 A–F 记录 / 4 个班，2020 Spring–2021 Summer |
| Fall 2026 | Lec A 代码 **21020**，TuTh 3:30–4:50，HIB 100 |
| 授课 | Betancourt / Grissom / D'Ambrosio |

Zotistics 历史成绩（数据截至 2023 Spring）。注意样本只有 4 个班，稳定性弱于另两门。授课教授中只有 Betancourt 有 RMP 记录：RMP 上 10 条评分均分 3.3，属于褒贬不一。5 学分是这门课唯一的成本。

──────────────────────────────────

**SOCIOL 1 · Introduction to Sociology**（4 学分 · GE III + GE VII）

| 指标 | 数值 |
|---|---|
| 历史 A 比例 | **62.1%** |
| 历史平均 GPA | 3.45 |
| 样本量 | 17,326 条记录 / 68 个班，2014 Summer–2023 Spring |
| Fall 2026 Lec C | 代码 **69040**，MWF 9:00–9:50，须配一个 Dis |
| Fall 2026 其他班 | Lec A 代码 69000 线上；Lec B 代码 69020 TuTh 2:00–3:20（也须配 Dis） |

Zotistics 历史成绩。Lec C 由 Carian 讲授：RMP 上 19 条评分均分 5.0，属好评如潮。另两位授课人 Ferguson、Victoria Torre 中，Victoria Torre 我这边没有 RMP 记录。

──────────────────────────────────

**PUBHLTH 1 · Principles of Public Health**（4 学分 · GE III · 无讨论课）

| 指标 | 数值 |
|---|---|
| 历史 A 比例 | **60.9%** |
| 历史平均 GPA | 3.44 |
| 样本量 | 10,206 条记录 / 55 个班，2014 Summer–2023 Spring |
| Fall 2026 | Lec A 代码 **81010** 线上 TBA（Uban）；Lec B 代码 81015，TuTh 2:00–3:20（Farmer） |

Zotistics 历史成绩。Uban 在 RMP 上 33 条评分均分 4.5，属好评如潮。课程备注写明护理学与公共卫生各专业有优先选课权，你作为 CS 专业不占优先，选课时段可能吃亏，必要时用 Lec B。

──────────────────────────────────

**建议**

三门的组合是 13 学分：ART HIS 55（TuTh 3:30–4:50）+ SOCIOL 1 Lec C（MWF 9:00–9:50 + 一个 Dis）+ PUBHLTH 1 线上，时间上不冲突。**SOCIOL 1 必须同时选 Lec 和一个 Dis 段，只填 Lec 代码 WebReg 会直接拒绝。** 历史成绩只是过去学生的分布，不代表本学期评分政策不变，尤其 ART HIS 55 的样本只有 4 个班。

**下一步：** 需要我按 GE 类别再收窄（比如只补 GE II 或 GE IV），还是把 SOCIOL 1 换成避开早八的线上 Lec A？

</details>

### C3

输入：只推荐能补齐我剩余 GE 要求的课，给我 3 门建议。

预期：识别只补剩余 GE 的诉求；无可信审计时明确无法核实，不猜测缺项。

实际：8 秒解析超时。没有可信 GE 审计；回答仍推测“最可能仍缺”的类别并提出三门课，不能认定满足只补剩余 GE 的要求。

整例耗时（含登录、展示、截图及刷新恢复）：64.9 秒。解析耗时：8001.8 ms。

实际校验后计划：

```json
{
  "plan": {
    "goal": "balanced",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": false,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": ""
  },
  "status": "fallback",
  "issues": [
    "model_timeout"
  ],
  "metrics": {
    "parse_elapsed_ms": 8001.8,
    "llm_calls": 1,
    "failure_reason": "model_timeout"
  }
}
```

排除：0 门；最终保留 1562 门。由于解析降级，没有按该案例的选课目标排除课程。

| 课程 | 当前主讲 | 推荐分 | 有效特征 | A 证据范围 |
| --- | --- | ---: | --- | --- |
| I&C SCI 139W | BLACK, R. | 64.3 | RMP | 缺失 |
| ANTHRO 2A | OLSON, V. | 62.9 | A 率 + RMP | 教授跨课程汇总 |
| PHILOS 5 | O'CONNELL, R. | 未评分 | 无可用特征 | 缺失 |

真实页面刷新恢复：答案、卡片、预筛报告、排名和默认学期标签全部一致。

[完整 SSE、回答、卡片与恢复比对 JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C3.json)；[实际调用和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/traces/561fa09cafed451aba6c80dbe594139a.jsonl)

<details>
<summary>展开系统实际回答原文</summary>

**2026 Fall · 补齐剩余 GE 的 3 门课**

> **结论：需进一步信息。** 我手上没有你的官方 GE 审核结果（记录里只有 ICS 31/32/33、MATH 2A/2B 五门课），下面这 3 门是按课程库标注挑出的「最可能仍空着」的类别，不是审核结论。

──────────────────────────────────

**先说清依据**

- 已修课程库里带的 GE 标注集中在 **II（科学技术）/ Va / Vb（量化与形式推理）**，所以这三类大概率已经覆盖。
- 最可能仍缺的是 **Ib（高年级写作）、III、IV、VII、VIII**。下面三门分别对应 Ib、III+VIII、IV。
- 2026 Fall 现在已是第 5 周：**免费加退课窗口 10/09 已关闭**，但 late add/drop 到 **11/06**，现在加课须走 Enrollment Exceptions 并获批（据 UCI 校历 [链接](https://catalogue.uci.edu/academiccalendar/academiccalendar.pdf)）。如果你其实在规划 **Winter 2027**，告诉我，我按那个学期重排。

──────────────────────────────────

**I&C SCI 139W · Critical Writing on Information Technology**（GE Ib 高年级写作，4 学分）

| 指标 | 数值 |
|---|---|
| 学期 / 形式 | 2026 Fall · 全线上（时间 TBA） |
| 可选讲次 | Lec B **36260**（Black, R.）+ B1–B4 任选讨论 |
| 先修 | 需已完成低年级写作要求，**我的记录无法核实** |

- 覆盖 **GE Ib**，是三个候选里最可能真正缺的一类；课程库标注 ICS 学生优先选课。
- 关键风险：先修是「低年级写作要求」，如果你是用 AP/转学分抵掉的，记录里看不到，务必先在 DegreeWorks 确认。

──────────────────────────────────

**ANTHRO 2A · Introduction to Sociocultural Anthropology**（GE III + VIII，4 学分）

| 指标 | 数值 |
|---|---|
| 学期 / 形式 | 2026 Fall · 面授 |
| 可选讲次 | Lec B **60020**（Olson, V.）TuTh 9:30–10:50 + B1–B10；Lec A 60000（Egan, J.）TuTh 14:00–15:20 |
| 先修 | 无 |

- 一门覆盖 **III + VIII** 两类，是效率最高的选择。
- 历史给分（Zotistics，[来源](https://zotistics.com/)）：ANTHRO 2A × Egan, J. 的**课程-教师组合**数据 4,418 份 A–F 成绩、平均 GPA **2.58**、A 占 **20.3%**（2014 Summer–2022 Spring）；这是历史数据，不代表本学期，也不等同于 Olson 的给分——Olson 只有跨课程的教师级数据（668 份，3.39），课程级样本缺失。

──────────────────────────────────

**PHILOS 5 · Contemporary Moral Problems**（GE IV，4 学分）

| 指标 | 数值 |
|---|---|
| 学期 / 形式 | 2026 Fall · 全线上（时间 TBA） |
| 可选讲次 | Lec A **30340**（O'Connell, R.）+ Dis 1–4（周四 11:00 起） |
| 先修 | 无 |

- 覆盖 **GE IV**，无先修门槛，4 学分 4 units。
- 给分证据缺口：Zotistics 里没有可用的 PHILOS 5 课程/教师成绩数据，所以这一门的难度和 GPA 影响**无法给出有依据的判断**，只能按「无先修的人文导论」定位。

──────────────────────────────────

**建议**

三门都是 4 学分、共 12 学分，结构上互补（Ib / III+VIII / IV），且三门都是 Lec + Dis 配对——只填 Lec 不选 Dis 在 WebReg 会被拒。真正的瓶颈不是找课，是确认你缺哪几类：把 DegreeWorks 的 GE 审计截图或「未完成类别」告诉我，我就能把这三门收窄成确定能补缺的组合；若你其实在排 Winter 2027，我直接按 2027 冬季重查开课。

**下一步：** 你的 GE 审计里还空着哪几个类别——Ib？III？IV？VII？

</details>

### C4

输入：请推荐 3 门课，周五绝对不能上课，其他时间都可以。

预期：excluded_weekdays=[Fri]；去掉已知周五班次，时间未知则保留并明确待核实。

实际：解析超时，周五硬条件丢失。ANTHRO 2A 卡片仍有四个周五讨论课；PHILOS 3 时间未知，回答却声称不可能撞周五。

整例耗时（含登录、展示、截图及刷新恢复）：85.8 秒。解析耗时：9562.8 ms。

实际校验后计划：

```json
{
  "plan": {
    "goal": "balanced",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": false,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": ""
  },
  "status": "fallback",
  "issues": [
    "model_timeout"
  ],
  "metrics": {
    "parse_elapsed_ms": 9562.8,
    "llm_calls": 1,
    "failure_reason": "model_timeout"
  }
}
```

排除：0 门；最终保留 1562 门。由于解析降级，没有按该案例的选课目标排除课程。

| 课程 | 当前主讲 | 推荐分 | 有效特征 | A 证据范围 |
| --- | --- | ---: | --- | --- |
| PHILOS 3 | BERNECKER, S. | 69.6 | RMP | 缺失 |
| ANTHRO 2A | OLSON, V. | 62.9 | A 率 + RMP | 教授跨课程汇总 |
| I&C SCI 45C | KLEFSTAD, R., ATALLAH, N., RATON, M. | 未评分 | 无可用特征 | 缺失 |

真实页面刷新恢复：答案、卡片、预筛报告、排名和默认学期标签全部一致。

[完整 SSE、回答、卡片与恢复比对 JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C4.json)；[实际调用和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/traces/f5bf0893aba8400b856ee67433d77a06.jsonl)

<details>
<summary>展开系统实际回答原文</summary>

**Fall 2026 · 周五全空方案（12 学分）**

> **结论：推荐。** 三门课我都核实过快照中的每周会议时间，均不含周五，而且把 **I&C SCI 45C** 这个你最关键的卡点也排了进去。

──────────────────────────────────

**排课表**（Registrar WebSoc，2026 Fall 实时抓取）

| 课程 · 代码 | 时间 |
|---|---|
| **I&C SCI 45C** Lec A · 36120 | TuTh 12:30-13:50（无 Dis） |
| **ANTHRO 2A** Lec B · 60020 | TuTh 09:30-10:50 + Dis 任选 |
| **PHILOS 3** Lec A · 30220 | 全线上 TBA（Dis 亦全线上） |

──────────────────────────────────

**逐门说明**

**I&C SCI 45C（4 学分，核心）**
唯一讲座在 TuTh 12:30-13:50，没有讨论课，因此不占用周五。它是你目前唯一能上的 CS 核心进阶课（**I&C SCI 46** 需要先修 45C，**I&C SCI 51** 还缺 **I&C SCI 6B**）。
不确定性：先修要求是 I&C SCI 33 且成绩 ≥ C；你的档案里 ICS 33 已修但成绩记录缺失，系统判定为“待核实”，需你在 WebReg 或 DegreeWorks 上确认。另外这节课只有 1 个讲座班，秋季已注册 255 人，容量风险偏高。

**ANTHRO 2A（4 学分，GE III + GE VIII）**
Lec B 是 TuTh 上午，讨论课可选 B1/B2（周一）、B3-B6（周三）、B7/B8（周四下午），全部避开周五；B9、B10 在周五，避开即可。先修课无需核实。
历史成绩参考（Zotistics，历史数据非本学期）：该课由 Egan 授课的历史记录样本 4,899 人、均分约 2.58、A 占 20.3%——不是“水 A”型 GE，但拿 C 以上很稳。注意这是历史分布，不代表 2026 秋季 Olson 班的给分。

**PHILOS 3（4 学分，GE II）**
讲座与 4 个讨论课全部标注 ON LINE / TBA，属于无固定会议时间的线上课，物理上不可能撞周五。适合用来调剂一门轻量 GE；代价是没有固定上课时间，需要自律。RMP 上该讲课教师有 36 条评分、均分 4.0（样本充足度中等）。

──────────────────────────────────

**两点必须知道的排课冲突事实**

本学期你的 CS 必修几乎全部被周五堵死：**MATH 3A** 三个讲座全是 MWF；**I&C SCI 6B** 两个讲座也都是 MWF；**I&C SCI 6D** 唯一讨论课在 F 14:00-15:50；**STATS 67** 四个讲座的所有讨论课都在周五。所以严格“周五绝不上课”的前提下，这学期你只能推进 45C 一门核心课，数学/统计/离散要留到冬季。

**建议**
Fall 2026 已进入学期中、add/drop 早已关闭，这份清单是按秋季官方课表给出的结构参考（数据为 2026-10-19 快照）；真正能落地的下一窗口是 Winter 2027。以 12 学分保持全职、难度分布也偏轻，符合你 3.7 的目标 GPA——把最难的一门留在 45C 上，另外两门给你的 GPA 更大缓冲。

**下一步：** 要不要我直接按 Winter 2027 查一遍 45C、MATH 3A、STATS 67 的开设与周五情况？

</details>

### C5

输入：请推荐 3 门课，最好不要早八。

预期：avoid_early_classes=true；保持软偏好，不建立早八硬截止条件。

实际：原始 JSON 的 avoid_early_classes=true 正确，但 explicit_constraints 含字符串“推荐 3 门课”；校验失败后软偏好变回 false。

整例耗时（含登录、展示、截图及刷新恢复）：79.9 秒。解析耗时：5379.5 ms。

实际校验后计划：

```json
{
  "plan": {
    "goal": "balanced",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": false,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": ""
  },
  "status": "fallback",
  "issues": [
    "model_invalid_schema"
  ],
  "metrics": {
    "parse_elapsed_ms": 5379.5,
    "llm_calls": 1,
    "failure_reason": "model_invalid_schema"
  }
}
```

排除：0 门；最终保留 1562 门。由于解析降级，没有按该案例的选课目标排除课程。

| 课程 | 当前主讲 | 推荐分 | 有效特征 | A 证据范围 |
| --- | --- | ---: | --- | --- |
| MATH 3A | YOUSSEFPOUR, H. | 64.3 | A 率 + RMP | 教授＋本课程 |
| I&C SCI 45C | KLEFSTAD, R., ATALLAH, N., RATON, M. | 未评分 | 无可用特征 | 缺失 |
| STATS 67 | BAO, J. | 未评分 | 无可用特征 | 缺失 |

真实页面刷新恢复：答案、卡片、预筛报告、排名和默认学期标签全部一致。

[完整 SSE、回答、卡片与恢复比对 JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C5.json)；[实际调用和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/traces/b4f1af4ae4564882ab5a7f8ab5753970.jsonl)

<details>
<summary>展开系统实际回答原文</summary>

**2026 Fall · CS 核心三门（都避开早八）**

> **结论：推荐。** 45C、MATH 3A、STATS 67 都是 CS 必修，而且秋季都有 10 点以后开的班次。

──────────────────────────────────

**先说明时间现状**（UCI 学术日历，2026-10-19 查）

Fall 2026 已于 9/24 开课，前两周在 WebReg 免费加退课已在 **10/9** 结束；现在到 **11/6** 只能走 Enrollment Exceptions 特批加课，而且已经缺了四周内容。如果你是提前规划下个季度，告诉我，我按 Winter 2027 的班表重排。

──────────────────────────────────

**I&C SCI 45C · 36120**（Registrar WebSoc，2026 Fall）

| 指标 | 数值 |
|---|---|
| 讲座 | Lec A · **TuTh 12:30-1:50p** · ALP 2300 |
| 教师 | Klefstad / Atallah / Raton |
| 先修 | ICS 33（最低 C） |
| 讨论课 | 无，单讲座即可 |

先修核验：你修过 ICS 33，但档案里没有记录成绩，所以“最低 C”这一条**系统无法确认**，需要你自己确认成绩单。该教师 RMP 上 319 条评分均分 2.6，属于**褒贬不一**。45C 只有一个班次，没得挑时间。

──────────────────────────────────

**MATH 3A · 44320 + 讨论课**（Registrar WebSoc + RMP + Zotistics）

| 指标 | 数值 |
|---|---|
| 讲座 | Lec B（**Youssefpour**）· MWF 1:00-1:50p · EH 1200 |
| 备选讲座 | Lec C · MWF 4:00-4:50p · 同教师 · 44330 |
| 讨论课 | B1 Tu 11:00 / B3 Tu 4:00 / B4 Th 4:00（B5、B6 为 5-6 点） |
| 先修 | MATH 2B —— **已满足** |
| 配套 | 必须 Lec + 一个 Dis |

先修是这三门里唯一**明确通过**的。教师 RMP 上 271 条评分均分 4.6，属于**好评如潮**；Zotistics 记录他 MATH 3A 的历史 A 率为 46.6%（133 条 A–F 成绩，2020 冬–2020 春，库更新至 2023 春；历史数据不外推你的成绩）。对照 Lu Z. 的 21.7%（529 条 A–F），选 Youssefpour 的 B 或 C 班更划算。注意 Lec B 已登记 299 人，可能已接近容量，满的话用 Lec C + C3/C4/C5/C6（10:00-11:50 那几个，别选 C1/C2 的 8 点）。

──────────────────────────────────

**STATS 67 · 36980 + 讨论课**（Registrar WebSoc）

| 指标 | 数值 |
|---|---|
| 讲座 | Lec B · MW 11:00-12:20p · ICS 174 |
| 备选讲座 | Lec C · MW 2:00-3:20p（36990）· Lec D · TuTh 2:00-3:20p（37000） |
| 讨论课 | B1 F 11:00 / B2 F 12:00；C1 F 3:00 / C2 F 4:00；D1 F 2:00 |
| 先修 | MATH 2B（最低 D-）—— 成绩未记录，无法核验 |
| 配套 | 必须 Lec + 一个 Dis |

避开默认的 Lec A（MW 9:30 + 周五 9:00 讨论），B/C/D 都从 11 点后开始。Bao J.、Seong A.（Lec D）两位我在 RMP 和 Zotistics 都**查不到可用评分或成绩数据**，所以不给风格判断。

──────────────────────────────────

**建议**

你这学期的核心短板是 45C 没修——它是 46、53 的唯一门槛，先把这门拿下来，后面两个季度才不会全被卡住。MATH 3A 和 STATS 67 是纯必修、可选班次多、时间也好安排，跟 45C 拼成 12 学分的技术型负荷，比硬塞 ICS 6B 更稳。补充一点：ICS 51 你目前**不满足**先修（缺 ICS 6B），而 Fall 2026 的 6B 只有 Gassko 一位（RMP 444 条评分均分 1.9，**差评如潮**），讲座是 MWF 11:00 或 3:00，讨论在下午 5 点——如果非修不可就接受它，但从体验看放到后面并不吃亏。

三门里 MATH 3A 和 STATS 67 都必须同时占一个讲座和一个讨论课，只填讲座 WebReg 会拒收。

**下一步：** 要我把 MATH 3A 的 Lec B 与 B1/B3 讨论课、STATS 67 的 Lec B 与 B1/B2 的 5 位 section code 整理成一张可直接抄进 WebReg 的清单，还是改按 Winter 2027 重新排一版？

</details>

## 下一步修复建议（本轮未实施）

先修筛选计划输出契约和解析超时，再处理降级时的硬条件告知及最终回答校验；随后按原五例重新验收。GE 审计是按需分支，不能用补齐 GE 审计替代这些意图解析问题。任何旧 Prompt／旧路径的删除仍遵循既有逐项审批范围。

## 原始产物

- [结构化验收总结](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/acceptance-summary.json)
- [浏览器与恢复检查总结](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/browser-summary.json)
- [串行真实解析复查](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/parser-recheck.json)
- [合成测试档案](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/test-profile.json)
- [C4 实际页面截图](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-acceptance-2026-10-19-a0bd7325/results/C4-live.png)

结果中的 captured 仅表示采集和页面恢复成功；本报告的业务结论为未通过。
