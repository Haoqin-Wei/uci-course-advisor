# M0 Baseline: Demo Flows and Known Failures

记录日期：2026-07-03

## Checkpoints

- `751af53` — separate runtime data from fixtures
- `a7b56ab` — checkpoint current advisor implementation
- 运行数据保留在本地，但 `data/memory/`、auth DB、grade cache、validation log
  和 professor summaries 均不再进入 Git。

## 验证环境

- FastAPI 从项目 `.venv` 启动。
- 浏览器目标：`http://127.0.0.1:8000/`。
- Guest 身份：`demo_001`。
- LLM：当前环境已启用真实 DeepSeek client；手工聊天验证只发送了一次请求。
- Signup API 使用临时 auth DB 和假邮件发送器验证，没有写入真实 `data/auth.db`。

## 当前可演示流程

| Flow | 操作 | 当前结果 |
|---|---|---|
| 注册与登录 | `request_code → verify → me → logout → login` | 临时 DB 下状态码依次为 `200, 200, 200, 200, 200`；logout 后 `me` 为 `401`。 |
| Guest 进入 | Auth modal 点击“以游客身份继续” | 成功进入；后端身份回退为 `demo_001`。 |
| Onboarding | Profile → Open wizard → school → year → major → completed courses | 四步 UI 和数据接口可加载；本次验证在最终保存前退出，没有改写 profile。 |
| Onboarding 数据 | schools、ICS majors、COMPSCI courses | 返回 12 个 schools、8 个 ICS B.S. majors、180 门 COMPSCI courses。 |
| 发送消息 | Fall 2026 请求推荐一门下一步课程并显示卡片 | SSE 完成；工具链读取 profile、课程、section、先修和成绩，回答推荐 `I&C SCI 32`。 |
| 生成卡片 | 同一条消息 | 生成 1 张 `I&C SCI 32` 卡片，包含 section、先修、add/drop 和成绩信息。 |
| 加课 | 展开卡片，添加 Lecture A（registrar code `36090`） | Schedule 打开并显示 `1 course`，周视图显示对应上课时间。 |
| 恢复会话 | 切换到其他 session 后再返回；随后刷新页面并重新进入 Guest | 用户消息、AI 回答和课程卡片均能恢复。 |
| Continue | 运行离线 `scripts.smoke_limit_reached` | 当前失败，尚无可接受的正向基线；详见 `M0-KF-01`。 |

本次手工聊天产生了一个被 Git 忽略的 Guest runtime session，标题为
`Pick ICS32`。它只用于确认新格式 turn 能持久化 cards。

## 已知失败场景

### M0-KF-01 — limit reached / Continue smoke contract 冲突

- 命令：`python -m scripts.smoke_limit_reached`
- 结果：失败。
- 当前代码允许 fallback 最后调用一次 `propose_recommendation`，但 StubClient
  仍断言 fallback 不应收到任何 `tools`。
- 实际事件为
  `tool_call_start → tool_call_done → limit_reached → error`，不是预期的
  token/final 序列。
- M1 应建立统一的 fake LLM contract，明确 fallback 是否允许最后一次卡片工具调用。

### M0-KF-02 — smoke 脚本直接运行失败

- `python scripts/smoke_test.py` 报
  `ModuleNotFoundError: No module named 'app'`。
- `python -m scripts.smoke_test` 可以运行并完成 Spring 2025 validation smoke。
- M1 应提供一条不依赖手工设置 `PYTHONPATH` 的统一测试命令。

### M0-KF-03 — Guest 使用共享可写身份

- 任意未登录用户都会进入 `demo_001`。
- 新 Guest 可以看到已有 Guest session，并能写入同一份 profile、memory 和 session。
- 公开环境前必须改为隔离且可过期的 Guest 身份。

### M0-KF-04 — 旧 profile 无法完整预填 onboarding

- 当前 profile 有 `major: Computer Science`，但没有 `program_id`。
- Profile 中重开 wizard 后，school 和 year 可以继续，Step 3 major 没有自动选中，
  Continue 被禁用，必须重新选择专业。
- M1 应使用 legacy profile fixture 固定该行为；后续迁移或兼容 `major` 名称。

### M0-KF-05 — Schedule 状态跨 session 泄漏

- 在 `Pick ICS32` session 添加一门课后切换到 `King 51B 适合` session。
- 聊天内容切换成功，但 schedule count 仍为 `1 course`。
- `loadSession()` 没有清空或按 session 重新加载 `scheduleEvents`。

### M0-KF-06 — Schedule 无法随 session 恢复

- 新 session 的文字和 cards 在页面刷新后可以恢复。
- 同一 session 的 schedule 刷新后从 `1 course` 变为 `0 courses`。
- 当前 schedule 依赖聊天模块的进程内 session state，持久化 session repository
  没有保存或恢复 pending schedule。

### M0-KF-07 — Continue 状态不能从历史会话恢复

- `continuation_id` 是进程内、single-use 状态，没有写入持久化 turn。
- 历史回答即使文字提示用户继续，重新加载时也没有可用的 Continue 控件。
- M1 应分别覆盖即时 Continue、重复使用 continuation ID、刷新和进程重启。

### M0-KF-08 — Instructor validation 存在误报

- Spring 2025 smoke 正确识别 `COMPSCI 999` 和 `Professor Nonexistent`。
- 同时把普通短语 “I'd skip that one” 误识别为 `Professor Skip`。
- M1 characterization 应保留这个输入；M4 再修正 instructor mention 解析。

## M1 Characterization 输入

优先把以下场景变成离线测试：

1. limit reached fallback 与 Continue 事件顺序。
2. 新 session 的 cards/followups/validation 持久化和恢复。
3. schedule add/remove/clear，以及跨 session、刷新、进程重启后的状态。
4. Guest 与 authenticated user 的 session/memory 隔离。
5. legacy profile 缺少 `program_id` 时的 onboarding 预填。
6. Spring 2025 validation smoke，包括 `Professor Skip` 误报。
