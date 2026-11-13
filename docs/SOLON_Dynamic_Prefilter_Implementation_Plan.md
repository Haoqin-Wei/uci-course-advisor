# SOLON — Intent-Aware Dynamic Pre-Filtering Implementation Plan

> **交付给 Codex 的实施计划**  
> 目标：在现有课程推荐系统中加入「LLM 理解用户意图 → 生成受约束的 Filter Plan → 后端校验并预筛 → 查询详细数据 → 现有推荐流程排序/解释」机制。  
> **重要：允许提出清理旧推荐逻辑、旧版 System Prompt、冗余模块和文件的建议，但任何删除或等效破坏性修改都必须提前向用户说明并取得明确批准。**

## 0. Codex 工作方式与不可违反的约束

1. **先审计代码仓库，再设计，再实施。** 不假设任何现有路径、模块、数据库字段或 Prompt 组织方式。下文文件名和函数名仅作建议。
2. **本期只做动态预筛选（Phase 1）。** 尽量复用原有课程查询、成绩/教授数据获取、推荐排名和 LLM 结果生成逻辑。不要顺手重写全套推荐系统，不要引入不必要的框架。
3. **不得静默删除旧实现。** 旧推荐代码、旧 System Prompt 的片段、旧适配器、旧文件与配置都可以列入候选清理清单；但无论是删除文件、删函数、删 Prompt 段落、移除调用分支，还是用覆盖/重命名实现等效删除，**都必须先请求用户明确批准**。
4. 审批只针对列明的删除项；**用户同意实施本计划不等于同意任何删除**。没得到同意时，保留旧代码并继续做非破坏性改进；清理阶段可以等待。
5. 不要运行未经批准的 `rm`、`git rm`、`git clean`、`git reset --hard` 或其他可能清除文件、历史或用户数据的操作。不要擅自改动生产密钥、`.env`、生产数据库、迁移历史等。
6. 如预筛选功能会改变线上结果，先加 feature flag（默认关闭），实现可比较、可回滚；经用户验收再切换默认值。
7. 每完成一个阶段，提供简明的变更说明、测试结果和风险。对于必须由用户决策的地方，明确列出选项，不要假装已获授权。

---

## 1. 业务背景与设计原则

### 1.1 要解决的问题

固定预筛选常见错误：学生已经完成 GE III，系统就自动删除所有满足 GE III 的课程。但另一门尚未修过的 GE III 课程可能历史给 A 比例很高、教授评价很好，且学生正在寻求 GPA 友好课程，因此**仍有推荐价值**。

必须严格区分：

- **Eligibility（修读/成绩资格）**：能否修读、是否开课、先修是否满足、重复修读及成绩/学分如何计算等。
- **Degree Contribution（毕业贡献）**：对学生尚未完成的 GE/学位要求是否有增量帮助。
- **Personal Value（用户价值）**：是否符合 GPA、教学质量、时间、兴趣、工作量等偏好。

**已完成某一 GE 类别 ≠ 同类别其他课程没有价值 ≠ 同一门课程可以无限重复修读并提高 GPA。** 学校的正式规则与具体学生记录必须由确定性规则和可靠数据处理，不能由 LLM 猜测。

### 1.2 目标工作流

```text
学生自然语言需求 +（可用且经授权的）学生学业状态
                         │
                         ▼
               LLM Intent Parser
         → 输出结构化 Filter Plan
                         │
                         ▼
                Plan Validator
       → Schema / 允许规则 / 用户显式条件验证
                         │
                         ▼
           Lightweight Candidate Retrieval
     → 学期、课程、section、GE、课表等基础元数据
                         │
                         ▼
               Pre-Filter Engine
       → keep / exclude / needs_verification
                         │
                         ▼
       对保留候选批量加载详细教授/成绩数据
                         │
                         ▼
        复用现有 Ranking + LLM Recommendation
                         │
                         ▼
        推荐卡片 + 解释 + 必要的不确定性提醒
```

在可能的情况下，把意图解析和现有首次 LLM 调用合并，避免不必要的额外调用；不得因此牺牲输出可靠性。

---

## 2. Phase 0 — 仓库审计（先执行，不要直接删除）

Codex 首先检查仓库并报告：

- 当前用户请求从 API/UI 进入到推荐输出的完整调用链，标注实际文件路径、关键函数和调用关系。
- 课程检索方式和返回限制：是否按课程编号截断、是否先限制 30 门、是否存在漏召回风险。
- 课程与 section / instructor / term 的数据粒度；是否有开课时间、GE、先修、学生已完成课程、历史成绩、教授评分。
- 现有 System Prompt、Prompt 模板、工具调用指令的所在位置；哪些是正在使用的，哪些可能已过时。
- 现有推荐打分是 Python 计算、LLM 排序，还是混合实现；是否已有 feature flag / tests / logging。
- 依赖、调用方、前后端契约与部署入口；明确哪些内容目前无法确认。

先提交一个简短的 **Architecture Audit + Implementation Proposal**，包含建议复用与新增的模块、预计改动文件，以及潜在废弃内容的**候选清单**。未经审查不得以“旧版”“冗余”为由直接删除。

---

## 3. Phase 1A — 设计受约束的 Filter Plan

### 3.1 结构化输出

以下 JSON 是**设计示例**，请根据实际数据结构调整；必须在后端通过 Pydantic 或项目现有 Schema 验证。

```json
{
  "goal": "gpa_optimization",
  "hard_filters": {
    "only_unmet_ge": false,
    "earliest_start": null,
    "earliest_start_is_hard": false,
    "excluded_weekdays": []
  },
  "soft_preferences": {
    "high_a_rate": true,
    "professor_rating": "high",
    "avoid_early_classes": false
  },
  "explicit_constraints": [],
  "reason_summary": "User prioritizes GPA-friendly courses, not unmet GE progress."
}
```

建议首版允许的 `goal`：`gpa_optimization`、`graduation_progress`、`balanced`、`interest_exploration`。这些是帮助识别意图的策略标签，**不应限制用户只能有一种目标**。

字段设计要求：

1. **白名单 Schema：** LLM 只能输出明确支持的字段/枚举，不允许它直接生成可执行 SQL、Python 表达式或任意业务规则。
2. **强制区分硬条件与软偏好：** “绝对不要早八”可以作为硬性过滤；“最好不要早八”只能作为排名偏好，默认不排除。
3. **默认不激进：** `only_unmet_ge` 默认为 `false`。只有用户**明确说“只要能满足尚未完成的 GE/毕业要求”**，且资料足以判断时，才可启用这种排他式筛选。仅仅说“想快点毕业”不等于授权排除所有其他课程。
4. **可验证依据：** 硬约束应记录用户原文依据（或等价的结构化 provenance），必要时与原文及上下文核对；模型不能凭空把偏好升级为硬约束。
5. **不可靠时安全降级：** 如意图不明确或模型输出无效，回到保守默认策略；必要时向用户提出一个有针对性的澄清问题，不能偷偷删候选。
6. 学生的 GE 完成状态只使用授权获得且来源可靠的数据；不能从课程类别本身推断学生已经满足学位要求。

### 3.2 建议接口（名称仅作示例）

```python
class FilterPlan(BaseModel):
    goal: Literal[
        "gpa_optimization", "graduation_progress",
        "balanced", "interest_exploration"
    ] = "balanced"
    only_unmet_ge: bool = False
    earliest_start: str | None = None
    earliest_start_is_hard: bool = False
    # 其余字段依据现有数据能力逐步增加


def parse_filter_plan(user_message, conversational_context) -> FilterPlan: ...

def validate_filter_plan(plan, user_message, profile) -> FilterPlan: ...
```

`parse_filter_plan` 可以使用已有 LLM API / structured output 机制；不要凭空创建第二套模型调用框架。

---

## 4. Phase 1B — 后端验证与动态预筛选

### 4.1 执行顺序

1. 获取候选的**轻量元数据**：课程编号、学期、sections、GE 类别、课表时间及可用的先修/学籍规则字段。
2. 应用**确定性的系统规则**：本学期确认未开课、用户明确硬性时间要求等；其他学校学籍/先修限制仅在有可靠依据时判断。
3. 应用 **Filter Plan 允许的可选规则**：仅当用户明确要求只满足剩余 GE 时，按「实际未完成 GE 贡献」过滤；否则保留同类别候选。
4. 将不确定信息标记为 `needs_verification` 或随候选保留警告；不能把 `unknown` 当作 `false`。
5. 仅对保留候选批量查询成本较高的教授、历史给分和学生评论数据，再交给现有排名/解释流程。
6. 保留详细的排除原因，便于审计和调试。

### 4.2 推荐返回契约

```json
{
  "kept": ["COURSE_A", "COURSE_B"],
  "excluded": [
    {"id": "COURSE_C", "reasons": ["not_offered_this_term"]}
  ],
  "needs_verification": [
    {"id": "COURSE_D", "reasons": ["prerequisite_status_unknown"]}
  ],
  "applied_plan": {"goal": "gpa_optimization", "only_unmet_ge": false}
}
```

这是展示数据形状的例子。实现时保留原课程/section/instructor ID，尽量不破坏现有前端或 LLM 工具接口。`needs_verification` 可以是一类独立状态，也可以是 `kept` 上的警告，但它**不等于用户一定可以修读**。

### 4.3 防止误删的关键规则

- **已完成 GE 类别不能自动作为排除理由。**
- 一门课可能涵盖多个 GE 类别：计算其对**尚未完成要求的实际贡献**，不要简单比较单一标签。
- 不要把“另一门属于同一 GE 类别的课”和“重复修读同一门课”混淆；后者可能影响成绩与学分计算。
- 如果毕业审计/成绩单缺失，不得把推断的毕业状态用于硬排除。
- 如果部分 section 不满足时间要求，不要直接删除仍有合格 section 的整门课程。
- 如果课程限制或先修状态不能确认，标注并在最终建议中说明，不要捏造结论。
- **不得先按课程号取前 30 门，再做筛选/评分**，从而遗漏更好的候选；如需要 Top K 截断，应在有意义的检索/排序后进行，并记录限制。

### 4.4 实现思路伪代码

```python
plan = parse_filter_plan(user_message, conversation_context)
plan = validate_filter_plan(plan, user_message, student_profile)

light_candidates = retrieve_lightweight_candidates(term=target_term)
filter_result = apply_dynamic_prefilter(
    light_candidates, plan, student_profile
)

detailed_candidates = fetch_details_in_bulk(filter_result.kept)
recommendations = existing_recommendation_pipeline(
    detailed_candidates, user_message, plan
)
```

请根据仓库实际接口改写。若当前 LLM 推荐接口不接收 `plan`，以最小改动添加，并确保原有行为受 feature flag 控制。

---

## 5. Phase 1C — 与现有推荐逻辑最小集成

- 新增推荐管线时优先复用现有 retrieval / ranking / explanation 组件，避免一次重构过多。
- 初版**不要开发新的动态权重排序模型**；这属于之后的 Phase 2。先让剩余候选交给已有的推荐系统。
- 如果现有推荐已使用结构化权重或排序引擎，应复用它；不要创建功能重复的模块。
- 设计明确的日志/指标：解析耗时、过滤前后数量、排除理由、未知状态数量、详细数据查询量、LLM 总耗时与输出质量。
- 记录必要的非敏感调试信息；避免在日志中泄露完整成绩单、身份信息或密钥。
- 初版用 `DYNAMIC_PREFILTER_ENABLED=false`（或现有 feature-flag 约定）保持老流程默认可用；在测试验收后再决定是否启用。

---

## 6. 核心验收用例（必须覆盖）

| Case | 用户情境 | 预期结果 |
|---|---|---|
| 1 | 用户已完成 GE III，但明确想选容易拿 A 的课 | 属于 GE III 的**另一门合格课程**可保留；不能仅因 GE 完成而排除 |
| 2 | 用户明确说“**只要**能满足我尚未完成 GE 的课”且档案可靠 | 应过滤对未完成 GE 无贡献的课程；支持多 GE 类别 |
| 3 | 用户说“尽快毕业，同时 GPA 尽量好” | 不能机械开启 `only_unmet_ge=true`；应保留适用候选并在排序阶段兼顾目标 |
| 4 | “最好不要早八” | 作为软偏好，不直接删除早课 |
| 5 | “绝对不能有 10 点以前的课” | 对确实违反的 section 进行硬过滤，不误删该课程的其他合格 sections |
| 6 | 学生 GE 记录缺失/不确定 | 不得基于猜测过滤已完成 GE；提示缺失信息或保守保留 |
| 7 | 课程本学期确认不开设 | 从该学期的可选候选中排除，记录 `not_offered_this_term` |
| 8 | 课程先修资格未知 | 标记 `needs_verification`，不能当作确定合格或不合格 |
| 9 | 同一门课程重复修读 | 与相同 GE 类别的不同课区别处理；不得承诺重复课程可提升 GPA |
| 10 | 同一输入、档案与配置 | Filter Plan 校验和过滤结果可复现；边界情况有单元测试 |
| 11 | 旧功能 flag 关闭 | 现有行为、API 结构和前端展示不发生意外回归 |
| 12 | 候选超过 30 门 | 不因课程号排序的提前截断而遗漏高质量候选 |

测试要尽量使用 synthetic fixtures，不依赖实时外部接口；复杂学业规则不能被 mock 成不存在的“确定事实”。

### 质量评价

至少输出：
- **误删率 / 候选召回情况**（优先指标）
- **硬性约束正确性**
- **过滤前后候选数**
- **新增延迟与 LLM 调用成本**
- **旧版 vs 新版推荐差异**

---

## 7. 旧代码、旧版 System Prompt 和冗余文件的清理策略（强制审批）

这次改造可以**提出**删除或合并旧的推荐流程、过时 Prompt 规则及未被调用的文件，以降低长期维护成本。但必须把“识别废弃项”与“实际删除”严格分开。

### 7.1 哪些内容可以成为候选清理项？

- 与新 Filter Plan 重复、冲突或失效的固定预筛选条件。
- 旧版 System Prompt 中已被新结构化意图解析替代的过时筛选指令。
- 迁移后确实没有运行时入口/调用方的旧推荐函数、模块、文件。
- 重复的课程检索适配器、只用于旧流程的冗余配置或测试替身。

**注意：** 不能因为旧代码“看起来没用”就认定可以删；需要检查 imports、反射/字符串引用、运行时注册、部署脚本、文档、测试和接口依赖。Prompt 也可能影响工具调用及前端输出格式，需核查相关依赖。

### 7.2 删除审批门槛：必须先提交清单，并暂停相关删除

在执行任何删除之前，Codex 必须在聊天中向用户提交如下清单（路径和行号要从实际仓库获取）：

| 编号 | 文件路径 / Prompt 段落 / 函数 | 拟删除的具体内容与范围 | 删除理由 | 替代方案 | 依赖与风险 | 是否可回滚 |
|---|---|---|---|---|---|---|
| D-001 | `实际路径` | `准确的函数名/行号/Prompt 段落` | `为什么过时` | `由哪个新模块替代` | `调用方/行为变化` | `Git 可恢复` |

另外附上：

1. **拟删除内容的摘要或相关 diff**；System Prompt 修改要展示待删除的**原文片段**及删除后关键行为差异。
2. 证明已检查引用关系和测试覆盖的说明。
3. 每一项删除对推荐结果、API、前端、部署及回滚的影响。
4. 若用户不批准，如何安全地保留旧实现而仍完成动态预筛选。

然后明确提出：

> **请确认是否批准删除 D-001 / D-002 / ……？**  
> **在你明确批准具体编号前，我不会删除对应文件、代码、System Prompt 内容或进行等效破坏性改写。**

### 7.3 用户批准规则

- 只有用户在收到上述清单后**明确批准的编号/范围**才可以删除。
- 用户没回应、说“先看看”、仅同意实施新架构、或只批准部分编号，**都不构成其余删除项的批准**。
- 若实际删除范围与此前清单不同，必须重新申请批准。
- 删除前优先保证新代码已通过测试、旧路径已明确被替代，并且有可恢复的 Git 提交/差异记录。
- 未获批准的旧实现可以保留、标记 deprecated，或留待单独清理阶段；不能私自清理。
- 删除后应再次运行测试，附上最终 diff 和已删除清单。

> **额外保护：** 改写 Prompt 并去掉旧行为、覆盖旧文件、删除旧功能入口、删除一大段逻辑使其不再运行，都视为需要审批的“等效删除”，不能通过不执行 `rm` 来规避确认。

---

## 8. 推荐的实施里程碑

### Milestone A — 审计和提案

- [ ] 列出当前真实推荐调用链和 System Prompt 来源。
- [ ] 确认当前学期/课程/section/GE/教授数据结构。
- [ ] 提交最小改动方案与潜在废弃项清单（此时不删除）。

### Milestone B — 可运行的动态预筛选（旧行为仍可用）

- [ ] 定义受约束的 Filter Plan Schema。
- [ ] 接入意图解析与 Validator。
- [ ] 复用轻量课程检索并实现后端 Pre-Filter Engine。
- [ ] 支持 keep / exclude / unknown 的审计结果。
- [ ] 通过 feature flag 接入现有推荐流程。

### Milestone C — 验证与优化

- [ ] 完成第 6 节测试用例。
- [ ] 检查 GE 已满足但 GPA 友好的课程不会被误删。
- [ ] 对比延迟、候选覆盖率、排序输入量和推荐质量。
- [ ] 提供可能的性能问题及优化建议。

### Milestone D — 用户批准后才进行旧逻辑清理

- [ ] Codex 展示逐项删除清单、diff、引用分析和风险。
- [ ] **等待用户明确批准具体删除项。**
- [ ] 仅删除已获批准的内容，记录可回滚方式。
- [ ] 重跑全部相关测试并总结改变。

---

## 9. 本期不做的事情（Out of Scope）

- 不训练字节式实时大规模推荐模型，不引入 Monolith / TensorFlow / PyTorch。
- 不新建不必要的向量数据库或大型微服务架构。
- 不在本期要求重做基于 LLM 的权重生成和排名机制；后续可单独实现 Dynamic Ranking。
- 不把学校的学业/注册规则交给 LLM 自由生成。
- 不在未征得用户批准时删除旧推荐逻辑、Prompt 或相关文件。

---

## 10. 最终交付要求

Codex 完成后应提供：

1. **现有架构审计结果**：实际文件、函数、数据源、旧流程问题。
2. **变更清单**：新增/修改文件及原因；说明哪些旧代码仍保留。
3. **新流程说明**：自然语言 → Filter Plan → Validator → 预筛 → 原推荐系统。
4. **测试与效果**：主要测试结果、回归情况、候选覆盖与响应性能。
5. **待删除/已批准删除/未批准保留** 三份明确区分的清单；未批准项绝不可删除。
6. **回滚路径** 和 feature flag 的启停方法。
7. 下一阶段建议（可选）：动态权重评分、用户反馈学习或多路召回，但**不要未经确认自行实施**。

### Codex 启动指令

**请先完成 Phase 0 的仓库审计，并用中文向我说明具体集成位置、打算新增/修改的文件，以及可考虑清理的旧推荐逻辑与旧 System Prompt。然后进行不涉及未批准删除的最小实现。凡是拟删除任何旧代码、Prompt 段落或文件，必须先提交第 7 节规定的逐项清单并等待我明确批准；不得把我发给你的这份计划视为删除授权。**
