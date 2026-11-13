# P-001～P-016 修改后：真实 LLM 本地验收

**结论：尚未通过最终完整验收，暂不上线。** 最终版本 C1 完成提交、真实模型、卡片和刷新恢复，但仍有无证据风险比较措辞；C2 中途收到 DeepSeek **402 / Insufficient Balance**，C3～C5 也被余额问题阻断。不能把后续故障降级或首轮结果当作最终业务通过。

## 修改、备份与回归

- 用户批准 P-001～P-016 后已实施，旧兼容 Prompt／函数保留，没有新增整文件删除。
- 修改前377文件快照逐文件校验，归档、未提交差异和安全回滚入口见 [实施与备份记录](prompt-alignment-p001-p016-2026-10-19.md)。
- 最终全套离线：**1090 passed，4 skipped，2 deselected**；四项可选真实Chrome离线回归另行执行 **4 passed**。不将这些结果等同于真实模型语义通过。
- 最终模型源码冻结后校验未变，正常 `.env` 未改；两个开关仅在隔离localhost进程启用。临时服务已关闭，未上线、未切生产开关、未触碰真实用户数据库。

## 验收条件

- 模型 `deepseek-v4-flash`，串行五个独立合成账号，真实浏览器登录、SSE与会话恢复，没有模型或课程API mock。
- 相同合成档案：CS大三，已修ICS31/32/33、MATH2A/2B，在修为空，无可信个人GE审计。无指定学期使用后端默认2026 Fall，不改时钟和默认标签。
- FilterPlan为一次有界调用，默认12秒；本轮未再加大时限。RMP为本地公开快照，WebSoc/Zotistics按实际工具调用；缺失不冒充实时全站无资料。
- 给分是历史统计，原始图表与样本收缩后的推荐分分开。所有非空卡片和主讲分均从raw value、n、k及有效权重独立重算，不仅检查字段存在。

## 最终版本逐例结果

| 案例 | 实际筛选计划／排除 | 推荐或部分结果（后端分） | 结论 |
| --- | --- | --- | --- |
| C1 | validated；scope=any；only_unmet=False；高A=True；整课排除0 | SOCIOL 1（83.0）；PUBHLTH 1（76.3）；ECON 13（59.3） | 部分通过，仍有输出问题；92.8s |
| C2 | validated；scope=ge；only_unmet=False；高A=True；整课排除0 | 中断前的部分候选：SOCIOL 1（83.0）；PHILOS 3（69.6）；SOCIOL 3（未评分） | 上游余额阻断，未完成；47.3s |
| C3 | 402导致model_error；未完成预筛，不能把默认值视为取消用户要求 | 无正式推荐 | 上游余额阻断，未完成；2.7s |
| C4 | 402导致model_error；未完成预筛，不能把默认值视为取消用户要求 | 无正式推荐 | 上游余额阻断，未完成；2.8s |
| C5 | 402导致model_error；未完成预筛，不能把默认值视为取消用户要求 | 无正式推荐 | 上游余额阻断，未完成；2.7s |

C1未按已完成GE类别排除课程；C2仅约束GE课程范围，没有自动启用只补剩余GE。两例没有必要按GE缺项整课排除，因此0不是未执行。C3～C5的0卡片来自上游不可用，不能冒充成功的GE／时间筛选。C2已出卡的数值不代表最终答案或页面恢复验收完成。

## 首轮完整五例与补丁

在最终补充前，另有一轮完整五例，浏览器／恢复5/5，完整业务输出仅C3通过（1/5）。该轮真实失败促成配对门禁和卡片绑定补丁；结果全部保留，不混入最终通过数。

| 案例 | 首轮结果 | 发现的问题 |
| --- | --- | --- |
| C1 | SOCIOL 1（83.0）；DRAMA 10（71.8）；ANTHRO 2A（62.9） | 意图/预筛正确，GE完成未缩小课程范围，评分与恢复正确；但ANTHRO2A卡片reason引用Lec A余34席，实际后端选中Olson Lec B(60020)，理由与所选班次未绑定一致，完整推荐输出未通过。 |
| C2 | SOCIOL 1（83.0）；ANTHRO 2A（62.9）；PHILOS 5（未评分） | GE范围及高A软偏好正确，未自动只补剩余GE；但正文把Olson跨课程59.3%与Egan本课20.3%称为同课两班差近40个百分点，统计范围不可直接等同。GE标签仍有ge_labels_unknown，却使用“目录确认”措辞；完整证据表达未通过。 |
| C3 | 暂停，无正式卡片 | 排他GE意图以requested_plan保留；缺可信GE审计则applied only_unmet_ge=false且gate.ready=false，0正式卡片、0重详情查询，回答明确暂停并澄清，未推测剩余类别。此项通过的是缺证据时的边界，不是自动完成个人GE审计。 |
| C4 | I&C SCI 6D（73.2）；I&C SCI 45C（未评分）；STATS 67（未评分） | 已知周五班次过滤成功，但过滤后secondary_pairing_unknown仍存在。正文却声称三门纯Lec、无需Dis及周五全空，门禁遗漏原始配对不确定性，完整业务输出未通过；此项在后续补丁中修复并须重验。 |
| C5 | I&C SCI 6D（73.2）；I&C SCI 45C（未评分）；STATS 67（未评分） | avoid_early_classes保持软偏好、无硬截止、候选未硬删除；但正文称已选STATS67 B/36980 MW11:00且全部11点后，实际card primary A/36970 MW09:30，未明确为替代班；完整输出绑定未通过。 |

[首轮业务审查记录](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-final-2026-10-19-f2ed3517/business-review.json)；[首轮完整浏览器采集](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-final-2026-10-19-f2ed3517/browser-summary.json)。更早主动中止的 c4491102 尝试也单独保留，不计完整验收。

## 每例完整证据

### C1

输入：GE 已经修完了，我想找容易拿 A 的课，请推荐 3 门。

预期：GPA目标、高A软偏好；不因GE完成而排除整个类别。

判断：GPA目标、GE不排他、实际主班绑定、历史数据口径与评分正确；但正文称“比CS高年级课风险低得多”和“A率明显高于常规”，本轮没有对应CS比较样本或明确常规基线；又在secondary_pairing_unknown仍存在时保证只选讲座会被WebReg拒绝，超出已核实配对证据，完整业务仍仅部分通过。

实际模型解析／校验：

```json
{
  "plan": {
    "goal": "gpa_optimization",
    "secondary_goals": [],
    "course_scope": "any",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": true,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [],
    "reason_summary": "Wants easy-A courses; completed GE noted, scope kept broad."
  },
  "status": "validated",
  "issues": [],
  "metrics": {
    "parse_elapsed_ms": 1308.2,
    "llm_calls": 1,
    "failure_reason": null
  }
}
```

首次候选池计数：`{"input_candidates": 1562, "kept_candidates": 1562, "excluded_candidates": 0, "needs_verification": 1546, "input_sections": 452, "kept_sections": 451, "excluded_sections": 1, "before": 1562, "kept": 1562, "excluded": 0}`；工具调用：`{"search_courses": 1, "get_zotistics_grades": 11, "get_sections": 4, "check_prerequisites_met": 3, "propose_recommendation": 1, "get_professor_rating": 3}`。

实际卡片特征／原始班次（C2为中断前部分结果）：

```json
[
  {
    "course_id": "SOCIOL 1",
    "score": 83.0194146624167,
    "score_verified": true,
    "instructors": [
      "CARIAN, E."
    ],
    "grade_scope": "course_instructor",
    "grade_a_rate": 0.8772169167803547,
    "grade_sample_count": 733,
    "rmp": 5,
    "effective_weights": {
      "grade_a_rate": 0.6,
      "professor_rating": 0.4
    },
    "friday_codes": [
      "69040",
      "69041",
      "69042",
      "69043",
      "69027",
      "69028",
      "69029"
    ],
    "unknown_day_codes": [
      "69000"
    ],
    "time_displays": [
      {
        "code": "69040",
        "days": "MWF",
        "time": "MWF 9:00- 9:50"
      },
      {
        "code": "69041",
        "days": "F",
        "time": "F 10:00-10:50"
      },
      {
        "code": "69042",
        "days": "F",
        "time": "F 11:00-11:50"
      },
      {
        "code": "69043",
        "days": "F",
        "time": "F 12:00-12:50p"
      },
      {
        "code": "69044",
        "days": "M",
        "time": "M 1:00- 1:50p"
      },
      {
        "code": "69045",
        "days": "M",
        "time": "M 2:00- 2:50p"
      },
      {
        "code": "69046",
        "days": "M",
        "time": "M 3:00- 3:50p"
      },
      {
        "code": "69047",
        "days": "Th",
        "time": "Th 10:00-10:50"
      },
      {
        "code": "69048",
        "days": "Th",
        "time": "Th 11:00-11:50"
      },
      {
        "code": "69049",
        "days": "Th",
        "time": "Th 12:00-12:50p"
      },
      {
        "code": "69020",
        "days": "TuTh",
        "time": "TuTh 2:00- 3:20p"
      },
      {
        "code": "69021",
        "days": "Th",
        "time": "Th 4:00- 4:50p"
      },
      {
        "code": "69022",
        "days": "Th",
        "time": "Th 5:00- 5:50p"
      },
      {
        "code": "69023",
        "days": "Th",
        "time": "Th 6:00- 6:50p"
      },
      {
        "code": "69024",
        "days": "Tu",
        "time": "Tu 5:00- 5:50p"
      },
      {
        "code": "69025",
        "days": "Tu",
        "time": "Tu 6:00- 6:50p"
      },
      {
        "code": "69026",
        "days": "Tu",
        "time": "Tu 4:00- 4:50p"
      },
      {
        "code": "69027",
        "days": "F",
        "time": "F 10:00-10:50"
      },
      {
        "code": "69028",
        "days": "F",
        "time": "F 11:00-11:50"
      },
      {
        "code": "69029",
        "days": "F",
        "time": "F 12:00-12:50p"
      },
      {
        "code": "69000",
        "days": null,
        "time": "TBA"
      }
    ]
  },
  {
    "course_id": "PUBHLTH 1",
    "score": 76.26482717571531,
    "score_verified": true,
    "instructors": [
      "UBAN, K."
    ],
    "grade_scope": "course_instructor",
    "grade_a_rate": 0.7706237424547284,
    "grade_sample_count": 994,
    "rmp": 4.5,
    "effective_weights": {
      "grade_a_rate": 0.6,
      "professor_rating": 0.4
    },
    "friday_codes": [],
    "unknown_day_codes": [
      "81010"
    ],
    "time_displays": [
      {
        "code": "81010",
        "days": null,
        "time": "TBA"
      },
      {
        "code": "81015",
        "days": "TuTh",
        "time": "TuTh 2:00- 3:20p"
      }
    ]
  },
  {
    "course_id": "ECON 13",
    "score": 59.322225470135436,
    "score_verified": true,
    "instructors": [
      "SARRAF, G."
    ],
    "grade_scope": "course_instructor",
    "grade_a_rate": 0.5304442036836403,
    "grade_sample_count": 9230,
    "rmp": 3.8,
    "effective_weights": {
      "grade_a_rate": 0.6,
      "professor_rating": 0.4
    },
    "friday_codes": [
      "62046",
      "62047"
    ],
    "unknown_day_codes": [],
    "time_displays": [
      {
        "code": "62040",
        "days": "W",
        "time": "W 7:00- 9:50p"
      },
      {
        "code": "62041",
        "days": "M",
        "time": "M 4:00- 4:50p"
      },
      {
        "code": "62042",
        "days": "M",
        "time": "M 5:00- 5:50p"
      },
      {
        "code": "62043",
        "days": "M",
        "time": "M 6:00- 6:50p"
      },
      {
        "code": "62044",
        "days": "Th",
        "time": "Th 6:00- 6:50p"
      },
      {
        "code": "62045",
        "days": "Th",
        "time": "Th 7:00- 7:50p"
      },
      {
        "code": "62046",
        "days": "F",
        "time": "F 11:00-11:50"
      },
      {
        "code": "62047",
        "days": "F",
        "time": "F 12:00-12:50p"
      }
    ]
  }
]
```

页面恢复检查：`{"answer": true, "cards": true, "prefilter": true, "ranking": true, "dom_answer": true, "dom_cards": true, "dom_scores": true, "dom_warnings": true, "default_term": true}`。

[完整SSE／卡片／恢复JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C1.json)

[实际工具和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/traces/cc6a8b2876644635bd2fb28312b96950.jsonl)
[页面截图 live](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C1-live.png)
[页面截图 restored](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C1-restored.png)

<details><summary>展开真实回答或故障原文</summary>

**Fall 2026 · 三门高 A 率入门课（SOCIOL 1 / PUBHLTH 1 / ECON 13）**

> **结论：推荐。** 三门都是历史 A 率明显高于常规的大班入门课，你的成绩单上没有先修课障碍。

──────────────────────────────────

**SOCIOL 1 · 入门社会学** (Zotistics 课程-教师历史成绩)

| 指标 | 数值 |
|---|---|
| Carian 班 A 率 | **87.7%**（733 条 A–F 记录，2 个班次，2022 Fall–2023 Winter） |
| 近似历史 GPA | 3.84（无 ±，仅 A–F） |
| 课程整体 A 率 | 62.1%（17,326 条记录，68 个班次，2014 Summer–2023 Spring） |
| RMP | 19 条评分，均分 5.0，难度 1.4（好评如潮） |
| 卡片班次 | Lec C（69040），CARIAN, E.，MWF 9:00–9:50，须配 Dis C1–C9 |

同课备选：Lec B（69020）Ferguson，TuTh 2:00–3:20，历史 A 64.2%（204 条记录，仅 1 个班次）；Lec A（69000）为线上 TBA。

──────────────────────────────────

**PUBHLTH 1 · 公共卫生原理** (Zotistics 课程-教师历史成绩)

| 指标 | 数值 |
|---|---|
| Uban 班 A 率 | **77.1%**（994 条 A–F 记录，3 个班次，2020 Fall / 2022 Fall） |
| 近似历史 GPA | 3.66（无 ±） |
| 课程整体 A 率 | 60.9%（10,206 条记录，55 个班次） |
| RMP | 33 条评分，均分 4.5，难度 2.2（好评如潮） |
| 卡片班次 | Lec A（81010），UBAN, K.，线上 TBA，无 Dis 段 |

同课备选：Lec B（81015）Farmer，TuTh 2:00–3:20，历史 A 67.6%（941 条记录，6 个班次）。

──────────────────────────────────

**ECON 13 · 全球经济** (Zotistics 课程-教师历史成绩)

| 指标 | 数值 |
|---|---|
| Sarraf 班 A 率 | **53.0%**（9,230 条 A–F 记录，32 个班次，2014 Summer–2023 Spring） |
| 近似历史 GPA | 3.35（无 ±） |
| 课程整体 A 率 | 52.1%（9,852 条记录，40 个班次） |
| RMP | 155 条评分，均分 3.8，难度 2.7（褒贬不一） |
| 卡片班次 | Lec A（62040），SARRAF, G.，W 19:00–21:50，须配 Dis A1–A7 |

──────────────────────────────────

**风险与未确定项**

**数据时点：** Zotistics 历史库只更新到 2023 Spring，是过往四个学期以上的成绩记录，不代表 2026 Fall 的实际给分。
**配对：** SOCIOL 1 与 ECON 13 都必须同时选讲座与讨论课；只输讲座代码会被 WebReg 拒绝，讨论班次的具体配对该项在预筛里仍标为待核实。
**冲突：** PUBHLTH 1 卡片班次是线上 TBA，无法判定它与 SOCIOL 1 的 MWF 9:00、ECON 13 的周三晚课是否冲突；确定已知 SOCIOL 1 C 与 ECON 13 A 不重叠。
**排序说明：** 卡片顺序来自固定排序分数（历史 A 率权重 0.6、RMP 权重 0.4，本组无缺失维度）；分数不是拿 A 概率。

**建议**

你已修完 GE、目标是拉 GPA，这三门都是 4 学分的大班通识型课程，历史 A 率 53–88%，比 CS 高年级课风险低得多；但 PUBHLTH 1 的线上班次时间未知，混排后仍有隐藏冲突的可能。若要最稳的组合，SOCIOL 1 选 Carian 的 C 班是数据上最强的一门（87.7% A），PUBHLTH 1 建议改用 Farmer 的实体 B 班以避免 TBA 的不确定性。

**下一步：** 要不要我按你的具体空闲时段，逐一核对 SOCIOL 1 的讨论班（C1–C9）与 ECON 13 的讨论班（A1–A7）是否互不冲突？

</details>

### C2

输入：推荐 3 门简单的 GE 课程。

预期：GE范围，高A偏好；不得启用only_unmet_ge或擅选固定GE类别。

判断：GE范围与高A软偏好已解析并进入预筛，未启用only_unmet_ge。模型第6轮返回402 Insufficient Balance，SSE流内报错；虽然已生成三张候选卡，最终回答与刷新验收未完成。部分卡片PHILOS3的reason仍称GE II，而ge_category=null并有ge_labels_unknown，属于未核实GE标签的确定性表述，不能当正式推荐通过。

实际模型解析／校验：

```json
{
  "plan": {
    "goal": "gpa_optimization",
    "secondary_goals": [],
    "course_scope": "ge",
    "hard_filters": {
      "only_unmet_ge": false,
      "earliest_start": null,
      "earliest_start_is_hard": false,
      "excluded_weekdays": []
    },
    "soft_preferences": {
      "high_a_rate": true,
      "professor_rating": "any",
      "avoid_early_classes": false,
      "avoid_weekdays": []
    },
    "explicit_constraints": [
      {
        "field": "course_scope",
        "quote": "推荐 3 门简单的 GE 课程。",
        "source": "user_message"
      }
    ],
    "reason_summary": "Simple GE courses; requested count is an output instruction, not a filter."
  },
  "status": "validated",
  "issues": [],
  "metrics": {
    "parse_elapsed_ms": 2847.3,
    "llm_calls": 1,
    "failure_reason": null
  }
}
```

首次候选池计数：`{"input_candidates": 1562, "kept_candidates": 1562, "excluded_candidates": 0, "needs_verification": 1562, "input_sections": 452, "kept_sections": 451, "excluded_sections": 1, "before": 1562, "kept": 1562, "excluded": 0}`；工具调用：`{"search_courses": 6, "get_sections": 5, "get_zotistics_grades": 5, "propose_recommendation": 1, "get_policy": 1}`。

实际卡片特征／原始班次（C2为中断前部分结果）：

```json
[
  {
    "course_id": "SOCIOL 1",
    "score": 83.0194146624167,
    "score_verified": true,
    "instructors": [
      "CARIAN, E."
    ],
    "grade_scope": "course_instructor",
    "grade_a_rate": 0.8772169167803547,
    "grade_sample_count": 733,
    "rmp": 5,
    "effective_weights": {
      "grade_a_rate": 0.6,
      "professor_rating": 0.4
    },
    "friday_codes": [
      "69040",
      "69041",
      "69042",
      "69043",
      "69027",
      "69028",
      "69029"
    ],
    "unknown_day_codes": [
      "69000"
    ],
    "time_displays": [
      {
        "code": "69040",
        "days": "MWF",
        "time": "MWF 9:00- 9:50"
      },
      {
        "code": "69041",
        "days": "F",
        "time": "F 10:00-10:50"
      },
      {
        "code": "69042",
        "days": "F",
        "time": "F 11:00-11:50"
      },
      {
        "code": "69043",
        "days": "F",
        "time": "F 12:00-12:50p"
      },
      {
        "code": "69044",
        "days": "M",
        "time": "M 1:00- 1:50p"
      },
      {
        "code": "69045",
        "days": "M",
        "time": "M 2:00- 2:50p"
      },
      {
        "code": "69046",
        "days": "M",
        "time": "M 3:00- 3:50p"
      },
      {
        "code": "69047",
        "days": "Th",
        "time": "Th 10:00-10:50"
      },
      {
        "code": "69048",
        "days": "Th",
        "time": "Th 11:00-11:50"
      },
      {
        "code": "69049",
        "days": "Th",
        "time": "Th 12:00-12:50p"
      },
      {
        "code": "69020",
        "days": "TuTh",
        "time": "TuTh 2:00- 3:20p"
      },
      {
        "code": "69021",
        "days": "Th",
        "time": "Th 4:00- 4:50p"
      },
      {
        "code": "69022",
        "days": "Th",
        "time": "Th 5:00- 5:50p"
      },
      {
        "code": "69023",
        "days": "Th",
        "time": "Th 6:00- 6:50p"
      },
      {
        "code": "69024",
        "days": "Tu",
        "time": "Tu 5:00- 5:50p"
      },
      {
        "code": "69025",
        "days": "Tu",
        "time": "Tu 6:00- 6:50p"
      },
      {
        "code": "69026",
        "days": "Tu",
        "time": "Tu 4:00- 4:50p"
      },
      {
        "code": "69027",
        "days": "F",
        "time": "F 10:00-10:50"
      },
      {
        "code": "69028",
        "days": "F",
        "time": "F 11:00-11:50"
      },
      {
        "code": "69029",
        "days": "F",
        "time": "F 12:00-12:50p"
      },
      {
        "code": "69000",
        "days": null,
        "time": "TBA"
      }
    ]
  },
  {
    "course_id": "PHILOS 3",
    "score": 69.56521739130434,
    "score_verified": true,
    "instructors": [
      "BERNECKER, S."
    ],
    "grade_scope": "unknown",
    "grade_a_rate": null,
    "grade_sample_count": null,
    "rmp": 4,
    "effective_weights": {
      "grade_a_rate": 0,
      "professor_rating": 1
    },
    "friday_codes": [],
    "unknown_day_codes": [
      "30220",
      "30221",
      "30222",
      "30223",
      "30224"
    ],
    "time_displays": [
      {
        "code": "30220",
        "days": null,
        "time": "TBA"
      },
      {
        "code": "30221",
        "days": null,
        "time": "TBA"
      },
      {
        "code": "30222",
        "days": null,
        "time": "TBA"
      },
      {
        "code": "30223",
        "days": null,
        "time": "TBA"
      },
      {
        "code": "30224",
        "days": null,
        "time": "TBA"
      }
    ]
  },
  {
    "course_id": "SOCIOL 3",
    "score": null,
    "score_verified": null,
    "instructors": [
      "VICTORIA TORRE, P.",
      "COPEMAN-PAPAS, A.",
      "ROBERTS, M."
    ],
    "grade_scope": "unknown",
    "grade_a_rate": null,
    "grade_sample_count": null,
    "rmp": null,
    "effective_weights": {
      "grade_a_rate": 0,
      "professor_rating": 0
    },
    "friday_codes": [],
    "unknown_day_codes": [],
    "time_displays": [
      {
        "code": "69080",
        "days": "TuTh",
        "time": "TuTh 3:30- 4:50p"
      }
    ]
  }
]
```

页面恢复检查：`null`。

[完整SSE／卡片／恢复JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C2.json)

[实际工具和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/traces/54bec0f7000e4345800cf3c89689458e.jsonl)
[页面截图 failed](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C2-failed.png)

<details><summary>展开真实回答或故障原文</summary>

SSE中途出错，没有完成最终回答。

</details>

### C3

输入：只推荐能补齐我剩余 GE 要求的课，给我 3 门建议。

预期：识别排他GE请求；没有可信审计则保留未决并澄清，无正式卡片、无重详情查询、不能猜测缺项。

判断：上游402导致解析model_error，随后回答服务也不可用；只返回确定性故障说明，无正式卡片。本轮没有实际验到GE审计门禁，不能以首轮C3通过代替当前最终版通过。

实际模型解析／校验：

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
    "model_error"
  ],
  "metrics": {
    "parse_elapsed_ms": 583.8,
    "llm_calls": 1,
    "failure_reason": "model_error"
  }
}
```

首次候选池计数：`null`；工具调用：`{}`。

实际卡片特征／原始班次（C2为中断前部分结果）：

```json
[]
```

页面恢复检查：`{"answer": true, "cards": true, "prefilter": true, "ranking": true, "dom_answer": true, "dom_cards": true, "dom_scores": true, "dom_warnings": true, "default_term": true}`。

[完整SSE／卡片／恢复JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C3.json)

[实际工具和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/traces/b43545575f4e434291a00252a01968af.jsonl)
[页面截图 live](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C3-live.png)
[页面截图 restored](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C3-restored.png)

<details><summary>展开真实回答或故障原文</summary>

目前无法连接回答服务。我不会凭空编造课程建议或班次信息。你可以稍后重试，或先询问一门具体课程；服务恢复后我会调用课程数据进行查询。

</details>

### C4

输入：请推荐 3 门课，周五绝对不能上课，其他时间都可以。

预期：Friday硬排除生效；已知周五班次移除，TBA内部保留但不冒充满足要求的正式班次组合。

判断：上游402导致解析与回答不可用；没有完成Friday筛选和新配对门禁的真实模型复验。已有离线反例覆盖修复，但不得据此宣称真实业务通过。

实际模型解析／校验：

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
    "model_error"
  ],
  "metrics": {
    "parse_elapsed_ms": 615.5,
    "llm_calls": 1,
    "failure_reason": "model_error"
  }
}
```

首次候选池计数：`null`；工具调用：`{}`。

实际卡片特征／原始班次（C2为中断前部分结果）：

```json
[]
```

页面恢复检查：`{"answer": true, "cards": true, "prefilter": true, "ranking": true, "dom_answer": true, "dom_cards": true, "dom_scores": true, "dom_warnings": true, "default_term": true}`。

[完整SSE／卡片／恢复JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C4.json)

[实际工具和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/traces/d31e3facf7d0480da1834935db0e775c.jsonl)
[页面截图 live](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C4-live.png)
[页面截图 restored](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C4-restored.png)

<details><summary>展开真实回答或故障原文</summary>

目前无法连接回答服务。我不会凭空编造课程建议或班次信息。你可以稍后重试，或先询问一门具体课程；服务恢复后我会调用课程数据进行查询。

</details>

### C5

输入：请推荐 3 门课，最好不要早八。

预期：avoid_early_classes软偏好，earliest_start不设硬阈值，不因偏好硬删除。

判断：上游402导致解析与回答不可用；没有完成软时间偏好及主班/备选班一致性的真实模型复验。没有编造推荐，故障回答刷新一致，但不满足推荐任务。

实际模型解析／校验：

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
    "model_error"
  ],
  "metrics": {
    "parse_elapsed_ms": 652.1,
    "llm_calls": 1,
    "failure_reason": "model_error"
  }
}
```

首次候选池计数：`null`；工具调用：`{}`。

实际卡片特征／原始班次（C2为中断前部分结果）：

```json
[]
```

页面恢复检查：`{"answer": true, "cards": true, "prefilter": true, "ranking": true, "dom_answer": true, "dom_cards": true, "dom_scores": true, "dom_warnings": true, "default_term": true}`。

[完整SSE／卡片／恢复JSON](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C5.json)

[实际工具和来源追踪](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/traces/49b9cac744cb406584c8fdb2ea068002.jsonl)
[页面截图 live](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C5-live.png)
[页面截图 restored](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/results/C5-restored.png)

<details><summary>展开真实回答或故障原文</summary>

目前无法连接回答服务。我不会凭空编造课程建议或班次信息。你可以稍后重试，或先询问一门具体课程；服务恢复后我会调用课程数据进行查询。

</details>

## 剩余工作与限制

- 需先恢复DeepSeek API可用余额，再从同一冻结版本补验C2～C5；本轮没有充值或更换模型。
- C1仍有无对照样本／基线的跨课程风险判断和超出配对待核实证据的WebReg保证；C2部分卡片仍有未核实GE标签的确定性理由。不能仅凭Prompt已修改就宣称每次模型输出都正确。
- 个人可信GE审计适配器仍未接通；首轮C3通过证明的是缺证据时正确暂停，不代表自动算完个人GE缺项。
- 年份范围与相关追问有反例回归；term-rules.md另外三个已知时间解析缺口仍保留。
- 原始图表与评分计算已验证的部分、离线回归、真实模型受阻的部分分别记录，最终仍不具备正式开启条件。

[结构化最终验收总结](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/acceptance-summary.json)；[原始浏览器总结](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/browser-summary.json)；[合成档案](/Users/luoweipotter/Downloads/uci-course-advisor1/data/runtime/llm-prompt-rerun-2026-10-19-0a264d86/test-profile.json)。

