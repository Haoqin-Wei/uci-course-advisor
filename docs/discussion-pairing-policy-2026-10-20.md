# 2026-10-20：按完整课表识别 Dis 要求

用户本轮确认：课表列出 Dis 就必选；没有 Dis 就无需选；有 co-class 等额外说明时，检查关联课程当期课表，若列出 Dis 则必选。这是产品采用的选课判断规则；官方页面提供班次与说明事实，不能把这条产品规则写成 UCI 对所有课程的统一规定。

本轮仅本地实施，保留旧兼容链路。本地开发两个开关继续开启；没有修改 `.env`、共享默认开关、排名权重或生产配置，没有部署、调用收费模型或执行真实注册操作。

## 判断与接入

1. 使用后端已经解析的目标学期，读取官方 WebSoc 同课程 `ClassType=ALL`、`ShowComments=on`、`CancelledCourses=Include` 的完整结果。核对来源、课程、学期、页面结束与所有源行解析；先判断原始全量，再筛时间和名额。筛后的列表没有 Dis、网络失败、截断或错学期都不能证明无需 Dis。
2. 列出 Dis 时记为 required；一个主班时，同课程全部 Dis 是选一关系，数字班号也支持。多个主班的具体关系继续依官方说明确认，不能只凭字母或行共存猜配对。明确课程例外须单独处理。
3. 没列出 Dis，且没有未解决的附属组件或课程例外时，记为 not_required，显示“无需选 Dis”。Lab 等其他组件不被一并清除；TBA、在线、先修、名额和个人 GE 抵扣仍分别判断。
4. 从有明确作用域的官方课程说明提取 co-class 课程或五位班号，以固定 WebSoc 入口补查同学期完整课程。每次至多三项、去重、不递归；失败、引用不清、嵌套未解决或预算不足保持待核实。关联课程列有 Dis 时，保存其真实课程、学期、班号和来源哈希，标记 Dis 必选；没有跨课程配对证明时不假造可注册组合，也不把外部 Dis 改名为本课班次。
5. producer → db → 预筛 → 推荐卡片 → 实际课表添加/恢复共用 v2 证据。`policy_id=user_confirmed_discussion_presence`、`policy_version=1`、`user_confirmed=true` 标记本次规则；旧证据按需补查，失败不能沿用旧的正面结论。未知结果也保留版本标记以免每次读取都重试。成功/失败短缓存不删除旧磁盘文件。

必选要求、允许的班次关系、用户是否选齐是三个状态。ECON 13 的主班刚推荐出来时应显示“Dis 必选，尚未选齐”，而非“配对要求待核实”；DRAMA 10 则显示“无需选 Dis”。关系尚未知的多主班或跨课程组合仍显示具体的关系待核实。

修改入口：`app/data/websoc_pairing.py`、`websoc_workflow.py`、`db.py`；`app/recommendations/pairing.py`、`presentation.py`；`app/agent/prefilter.py`、当前 Agent Prompt/运行上下文；课表证据刷新及 `static/js/cards.js`。另修复出卡列表全部跳过时 `contract_enabled` 未初始化导致的异常，空列表现在正常返回确认结果。

## 真实来源验收

两份未经修改的官方公开响应已保存为 `tests/fixtures/websoc/pairing/drama_10_2026_fall.*` 与 `econ_13_2026_fall.*`；包括请求、原始响应、编码、捕获时间、字节数和 SHA256。公开请求不是选课注册，也未使用账户资料。

| 实际 2026 Fall 快照 | 按新规则的结果 | 实际添加/筛选预期 |
|---|---|---|
| DRAMA 10，A 03005，只有 Lec，TBA / ON LINE | 无需选 Dis | 仅加主班，配对满足；TBA 仍不能证明异步或满足不许周五的硬条件 |
| ECON 13，A 62040，Dis 62041–62047 | Dis 必选，七个明确的选一组合 | 仅加主班未选齐；加入 A7 62047 后配对满足 |
| ECON 13，加“绝对不能周五上课” | 周五 62046/62047 排除 | 其余 Dis 在原始快照均 FULL，无法产生可选的完整组合；不查详细给分后再推荐主班 |
| SOCIOL 1，A 69000 | Dis 必选，具体关系未知 | 不伪造 A 的允许组合；B/C 仍分别保留九个有官方关系证明的组合 |
| PHILOS 3，唯一主班 30220，数字 Dis 1–6 | Dis 必选，六个明确的选一组合 | 不再因讨论班不是字母编号而整体未知 |

完整链路回放使用生产解析/证据代码，经 db、预筛、卡片和临时 FastAPI 的实际 add/get 保存恢复；没有手工注入 verified。co-class 额外说明、网络失败、错学期、部分页、Lab、条件例外等使用明确标注的测试变体；这些测试不声称是真实 co-class 课程样本。验收区分规则/数据/界面测试和真实 LLM 对话，后者仍由用户自行验证。

## 备份与回滚

实施前已逐文件保存并校验 415 个文件：`.local-backups/discussion-policy-2026-10-20-ba3f27ed/`。`manifest.json` SHA256 为 `2ecb710ae9c10419ec4f2e27d6186c655112d18c2c2ae778b7ef71357e2cff26`。此前四个已批准删除的文件维持原状态，本轮不新增整文件删除。

完成后的变更清单与受保护回滚入口保存在同目录：先核对本轮结束哈希，拒绝覆盖后续改动；新增文件回滚时移入隔离区保留。

## 验收结果

全量离线回归 **1360 passed，2 deselected，0 failed，0 skipped**（29.04 秒），包含本仓库规定的八组学期回归和五项隔离 Chrome 检查。回放时钟进一步按各原始响应的捕获时间核对后，真实来源/保存恢复定向 **17 passed**；不借用前一个响应或表单的时间。原始结果位于 `data/runtime/discussion-policy-2026-10-20/pytest-final.xml`。

只读本地 Zotistics/RMP 缓存、真实官方响应回放所得四个案例保存在同目录 `real-data-acceptance.json`，`verify_sources.py` 可复现：

| 用户条件 | 正式结果 |
|---|---|
| DRAMA 10，无硬时间条件 | 71.8/100；无需 Dis |
| DRAMA 10，绝对不能周五上课 | 保留来源事实供核实，不正式推荐为符合条件；TBA 无法确认无周五课 |
| ECON 13，无硬时间条件 | 59.3/100；需选 Dis；当前唯一开放选项 A7 62047 |
| ECON 13，绝对不能周五上课 | 不正式出卡；唯一开放的 Dis 在周五，其他 Dis 已满 |

两份实际 Course Plan 经过桌面与 390px 手机 Chrome 渲染，来源、年度 GE、Dis 摘要、原始历史柱状图及布局检查通过，浏览器错误为零、无横向溢出。截图 `case-1/2-desktop/mobile.png`、结果 `browser-acceptance.json`、脚本 `verify_ui.cjs` 位于同目录。课程 A 率与排名分母保持原实现；本轮未调整评分权重。

`compatibility-check.json` 确认旧兼容五个 Prompt 常量、七个函数 AST、`app/config.py` 和 `.env.example` 与本轮备份一致。当前 Agent 提示同步新规则；新增的运行时规则提示只在任一功能开关启用时加入，双关路径保留旧续答行为。

```sh
NODE_PATH=/Users/luoweipotter/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
SOLON_BROWSER_CHECK=1 .venv/bin/python -m pytest tests -m 'not live' --no-cov -q \
  --junitxml=data/runtime/discussion-policy-2026-10-20/pytest-final.xml
.venv/bin/python data/runtime/discussion-policy-2026-10-20/verify_sources.py
NODE_PATH=/Users/luoweipotter/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
node data/runtime/discussion-policy-2026-10-20/verify_ui.cjs
```

这些结果覆盖真实数据、代码和界面链路；不是重新执行真实付费 LLM 的对话验收。已有历史消息不批量重写；刷新页面并重新查询可得到新证据，旧课表配对记录在再次读取/刷新时按新版本补查。
