# S-001～S-003：真实来源、GE 学年证据与配对接入

用户于 2026-10-20 批准实施。仅本地修改，保留旧兼容链路；未部署、未改生产开关、未调用收费模型。用户此前要求的本地 `.env` 两个开关继续开启，本轮没有修改 `.env`。

同日后续更新：下文记录 S-001～S-003 完成时的规则与结果。用户随后确认“列有 Dis 必选、无 Dis 无需选、有 co-class 查关联课表”，其最新实现和 DRAMA 10 / ECON 13 验收见 [Dis 判断新规则](discussion-pairing-policy-2026-10-20.md)。因此下文 PHILOS 3 整体未知等旧状态不再代表当前行为；未知的多主班具体关系仍需证据。

## 备份与回滚

修改前保存 **392 个文件**，逐文件核对 SHA256：

` .local-backups/s001-s003-2026-10-20-01a9a109/ `

`manifest.json` 的 SHA256 为 `854e153972c360fbf728cccda1842c848d637a3c5390f784cb8c6c6cd996269c`。原先四个已批准清理的缺失文件按修改前状态记录，本轮没有新增整文件删除。

同目录 `after-manifest.json` 记录本轮变更；`restore_s_changes.py` 默认只检查，使用 `--apply` 才回滚。回滚前要求当前文件仍匹配本轮结束哈希，防止覆盖后续修改；本轮新增文件移到备份目录的隔离区而非直接删除。运行前停止正在使用这些源码的本地开发进程。公共验收报告和缓存位于忽略的 `data/runtime/`，不覆盖真实用户记录。

## S-001：来源身份与条件状态分开

- `prefilter_metadata` 是内部处理标记，不显示成来源。
- `registrar_websoc` 与 `uci_websoc` 统一显示一次 UCI WebSoc，抓取时间单列；不把官方快照称为实时座位。
- 未识别来源显示中性标签；显式的未核实证据仍保留其警告含义。
- 已确认 GE 显示可点击的 UCI GE 目录学年。
- 警告按课程、学期、班次范围和 code 去重；课程警告不再复制到每个教授。
- 实际 Course Plan 隐藏了原始卡片代码区，因此另在可见正文区展示配套要求与当前选择状态。添加组件后读取服务端课表验证结果，更新摘要与计划状态。

## S-002：解析与官方认证分离

真实 `courses.csv` 的标签采用完整描述和 `|` 分隔。新的入口兼容数组、JSON、管道分隔及单标签，统一输出标准类别并保留原文及未知值。全量 **6687** 行解析未发现无法识别的现存标签；这不代表这些记录都已完成官方认证。

旧 CSV 即使是可解析的空数组，也没有对应目录学年，不能确认该课程在目标学年属于或不属于 GE。`section_ge.csv` 来自同一课程标签，不能算第二份独立证据。

`app/data/ge_evidence.py` 从官方整份 GE 名单取得年度证据，一次 GET 为同学年各课程复用。证据保留学年、官方 URL、时间、原始 HTML、SHA256 和解析器版本；落盘缓存恢复时重新解析和校验。成功缓存 24 小时，失败短缓存 5 分钟；有响应大小与请求超时限制。

目标学期决定目录学年：2026 Fall、2027 Winter/Spring 属于 2026–27；其他年份不能继承该认证。年度 GE 独立于开课历史代理，既不改 `query_terms`，也不改默认学期。找不到课程或页面失败时保留未知，不因为名单缺项就确认非 GE。

课程类别与个人毕业贡献分开。只有 Filter Plan 授权相应条件时用于过滤；缺个人可信审计不声称补齐了个人要求，也不将 GPA 请求固定改为只补 GE 缺项。

## S-003：官方配对证据生产与传递

公开 POST 固定读取目标课程的 `ClassType=ALL`、`ShowComments=on`、`CancelledCourses=Include` 全量结果。保留院系/课程注释、作用域、原始行序、课程边界及请求信息；不能先过滤班次再重新分组。真实页面包含 17 列，解析器按表头定位容量、人数、限制和状态，兼容原 15 列格式。

首个支持的规则是明确要求配套、并明确指示选择讲座下方组件的当期声明。必须同时具备官方来源、正确课程/学期/院系身份、完整请求、完整页面和可解析的原序班次。字母、颜色、零单位及共存不构成证明；混合组件语义不清或有例外时保持未知。

2026 Fall SOCIOL 1 的实际结果：

| 主班 | 官方关系 | 状态 |
|---|---|---|
| A 69000 | 没有紧随的附属组件 | 未知，不能推导无需配对；实际快照中 A 为 FULL |
| B 69020 | Dis 69021–69029，选一 | required，9 个已确认组合 |
| C 69040 | Dis 69041–69049，选一 | required，9 个已确认组合 |
| PHILOS 3 A 30220 | 存在数字讨论班 1–6，但无可用的明确配对声明 | 保持未知 |

v2 按主班分开保存配套要求、允许关系/覆盖范围和当前选择；v1 继续兼容。部分证明只支持明确正例，未覆盖组合不作为否定。需要 Dis 的规则已确认，不代表用户已经选了 Dis；新 v2 不自动替用户选择第一个讨论班。

证据完整传递经过 WebSoc → db → 预筛/班次缓存 → 出卡 → 课表验证。已有本地班次不能阻止按需补证；全目录检索仍只用本地轻量元数据，触及详细查询的候选才补官方证据，避免逐门爬全校。时间筛选检查完整组合，随后再按可用状态/限制确定主班并查询成绩、评分；不先给已无可用完整组合的主班查成绩。

显式刷新同时获取实时班次和完整配对规则，共用三秒等待预算，规则请求每次网络超时最多 2.5 秒。等待后不追加网络查询。新证据仅合并规则，不覆盖实时班次的时间；会话保留新版本、来源和 generation，后续恢复优先新证据。失败或超时保存未知，不能退回旧卡的 verified；下一次显式重试可以恢复。Continue/工具缓存不会复用刷新前结果。

## 真实数据验收

官源快照位于 `tests/fixtures/ge_evidence/` 与 `tests/fixtures/websoc/pairing/`。分别保存未修改的完整公开 GET/POST 响应及其 URL、请求、捕获时间、编码、字节数和 SHA256；没有账户或注册操作。传输回放保留原始捕获时间，不把测试执行时间当作新抓取时间。

完整数据链路测试不手工注入 `verified`：生产 fetch 读取真实表单和课程 HTML，产生证据后经过 db、预筛、卡片、实际临时 FastAPI add/get 路由与保存恢复。故障、结构变化、错学期和修改班次等反例单独标注为测试变体，不伪称为原始官源结果。

可复现的公开缓存验收：

| 固定 Filter Plan | 实际推荐 | GE | 配对与排除 |
|---|---|---|---|
| GE，无硬时间条件 | C 69040，CARIAN，83.0/100 | III、VII；2026–27 官方目录 | 需配一个允许的 Dis，尚未选择；可用班次再按状态缩小 |
| GE，周五不能上课 | B 69020，FERGUSON，59.7/100 | 同上 | 排除 C 主班和其他周五组件，共保留 7 条明确的周五排除记录；可选的开放讨论班仍须实际选择 |

评分使用既有、已校验的 Zotistics 教授＋课程缓存及本地 RMP 快照，没有重新请求模型或伪造评分。C 的历史 A 率仍为 643/733=87.7%，图表仍使用包含 P/NP 的分母 740，站点 A 百分比 86.9%，推荐分与原始比例保持分开。

结果位于 `data/runtime/s001-s003-2026-10-20/real-data-acceptance.json`。同目录 `verify_sources.py` 可重放；Zotistics 缓存过期后会显示缺失，不伪造成功。两组实际 Course Plan 经隔离 Chrome 检查桌面与 390px 手机布局、来源、GE 学年、配对提示和原始图表，无页面错误或横向溢出。截图为 `case-1/2-desktop/mobile.png`，结果为 `browser-acceptance.json`。

最终全量回归 **1302 passed，2 deselected，0 failed，28.69 秒**，包括规定的学期回归和 5 项既有实际 Chrome 检查，没有跳过浏览器测试。原始结果为 `data/runtime/s001-s003-2026-10-20/pytest-final.xml`；另有上述两组真实缓存数据的实际 Course Plan 桌面/手机验收。旧兼容 Prompt 常量与函数 AST、`app/config.py`、`.env.example`、`app/llm/adapter.py` 相对本轮备份一致，详见同目录 `compatibility-check.json`。真实 LLM 尚未重新调用，用户将自行进行对话验收。

```sh
NODE_PATH=/Users/luoweipotter/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
SOLON_BROWSER_CHECK=1 .venv/bin/python -m pytest tests -m 'not live' --no-cov -q \
  --junitxml=data/runtime/s001-s003-2026-10-20/pytest-final.xml
.venv/bin/python data/runtime/s001-s003-2026-10-20/verify_sources.py
NODE_PATH=/Users/luoweipotter/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
node data/runtime/s001-s003-2026-10-20/verify_ui.cjs
```

这些结果证明真实数据解析、规则和本地界面链路；不保证所有院系都有明确公开配对信息，不代表真实 LLM 对话或线上验收已通过。
