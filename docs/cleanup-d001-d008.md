# D-001～D-008 已批准清理记录

用户于 2026-10-19 明确批准仅清理此前候选清单中的 D-001～D-008，要求删除前保存当前版本；其他候选继续保留。

## 清理前快照与恢复

本地备份目录：`.local-backups/cleanup-d001-d008-2026-10-19-7b558e45/`。该目录通过自身 `.gitignore` 排除，不进入源码提交。

- `workspace-before.tar.gz` 保存清理前全部已跟踪文件及未忽略的新文件，共 379 个文件，包含尚未提交的 Zotistics、固定排名和动态预筛实现。
- `manifest.json` 保存基础 Git 提交、文件列表和 SHA-256；归档已逐文件校验。
- `tracked-changes-before.patch` 和 `status-before.txt` 保存清理前的差异和状态。仅恢复 Git HEAD 会丢失之前的未提交实现，因此不能将其当作本次回滚方式。
- 环境密钥、依赖、运行数据库等被忽略内容不在代码快照中；本次清理不修改这些内容。

备份目录内的 `restore_cleanup.py` 默认仅检查；显式传入 `--apply` 才恢复本次涉及的文件和文档。它会核对清理后校验值，拒绝覆盖之后新增的修改。该工具不重置 Git、不还原其他文件。

```sh
.venv/bin/python -B .local-backups/cleanup-d001-d008-2026-10-19-7b558e45/restore_cleanup.py
.venv/bin/python -B .local-backups/cleanup-d001-d008-2026-10-19-7b558e45/restore_cleanup.py --apply
```

## 实际删除范围

| 编号 | 实际范围 | 保留边界 |
| --- | --- | --- |
| D-001 | `app/llm/adapter.py` 的 `get_default_answer_prompt()` | `get_default_agent_prompt()`、全部 Prompt 常量和旧回答接口保留 |
| D-002 | `app/modules/conflict.py` 兼容重导出文件 | `app.scheduling` 及实际冲突校验保留 |
| D-003 | `static/js/cards.js` 的 `_firstSection()`，以及 `renderCard()` 内未使用的 `sec`、`primaryCode`、`addIcon` 和仅对应的注释 | 当前卡片渲染、班次分组、原始成绩图、排名、旧卡片字段兼容保留 |
| D-004 | `cards.js` 的 `toggleCard()` 及专属注释；`schedule.js` 的 `updateCardState()`；`components.css` 中两个 `.cc-add-btn` 选择器 | 同组 `.cc-sg-toggle` / `.cc-sg-wrap` 样式、当前教学班加减与服务端状态同步保留 |
| D-005 | `static/js/sessions.js` 的 `_inferGradClass()` | 当前档案毕业信息和学期规则保留 |
| D-006 | `debug_grades.py` 临时探测脚本 | 实际 Anteater 成绩源、Zotistics 和人工验证工具保留 |
| D-007 | `solon-split-view.reference.html` 独立设计样例 | 正式 `static/index.html` 和静态资源保留；参考页可从快照恢复 |
| D-008 | 根目录 `websoc_sample.json` 样例 | 正式 WebSoc 逻辑和 `tests/fixtures/` 保留 |

执行前重新检查了普通调用、导入、字符串引用、页面入口、脚本和测试；未发现上述范围新增的仓库内依赖。没有修改任何 System Prompt 原文。

## 未批准、继续保留

D-009～D-012 以及其他候选均未清理：旧回答生成链路、意图／信息提取 Prompt、两份静态 GE 规则、主 Agent 的 GE 示例继续保留。默认关闭时使用的前 30 项检索包装、Anteater 课程总体成绩、缓存、回归脚本和旧卡片兼容路径也继续保留。没有切换功能开关或部署。

## 验证

清理前相关基线：115 项通过，4 项可选浏览器检查跳过。

清理后验证：

- 完整离线回归：`python -m pytest --no-cov -q`，1008 项通过、4 项可选浏览器检查跳过、2 项 live/manual 未运行。
- 显式浏览器回归：使用本地合成数据，4 项通过。覆盖真实临时 API 的加课、刷新、恢复和移除，以及成绩图、Continue 和动态预筛卡片交互；未调用外部课程服务。首次受沙箱本地监听／Chrome 启动限制，获准在沙箱外重跑后通过。
- 三份改动 JS 的 `node --check`、Python 编译、`pip check` 和 `git diff --check` 通过。
- 独立按快照审查：恰好 9 个代码文件和本记录相关文档发生变化，其余 369 个原文件内容完全一致、权限未变；`adapter.py` 的 AST 差异严格只有删除 getter，全部 Prompt 原文保留。
- 回滚工具在临时副本执行恢复，按清理前 SHA-256 核对；真实工作区仅执行恢复前检查。

备份目录中的 `cleanup.patch` 是仅本次清理的完整差异，不混入此前未提交实现；`cleanup-after.json` 保存恢复检查使用的清理后校验值。没有运行真实模型评测或部署。
