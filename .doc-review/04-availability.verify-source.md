# 04-availability.verify-source — 逐行核对源码的对抗式复核

复核对象：`.doc-review/04-availability.diag.md` 第三节「与源码不符的地方」共 9 条
被诊断文档：`doc/arch/04-availability.md`
复核视角：逐行核对源码（默认诊断是错的，必须亲自在源码里看到证据才承认）
复核时间：2026-09-29

结论速览：**9 条里 8 条维持原判，1 条（第 5 条）推翻**。诊断在「模型失败收尾状态」这条主线上是**对的**，且是要害；但它在 4、5 两条里给出的行号大面积不准，第 5 条「与源码不符」的定性本身站不住。

---

## 一、维持原判的条目

### 第 1 条（L168：「再由 run 服务补一个 `turn_end(failed)`。」）—— 维持

核对证据：

- `src/agents/research/agent.py:277` `if not response.ok:` → `:293-295` 发固定话术助手消息（`LLM_FAILURE_MESSAGE`，常量定义在 `:147`）→ `:297` **`return`**（正常返回，不是抛异常）。
- `src/services/session_runs.py:398-410`：`await invoke_message_handler_async(...)` 正常返回后，只判断 `self.broker.is_cancel_requested(run_id)`；没收到取消就直接调 `self._finalize_success(...)`（调用点在第 410 行）。
- `_finalize_success`（`src/services/session_runs.py:438`）里 `self.repo.set_status(session_key, "completed")`（`:449`），发出的 `turn_end` 里 `"status": "completed"`（`:455-466`）。
- 真正发 `turn_end(failed)` 的只有 `_finalize_failure`（`src/services/session_runs.py:526`），而它只被 `except asyncio.CancelledError`（`:412-425`）与 `except Exception`（`:426-433`）调用。

结论：诊断成立。文档说「补 `turn_end(failed)`」，源码走的是 `completed` 收尾，会话被写成 `completed`。这是本篇最要命的一处，诊断抓住了。
（诊断给的行号 `agent.py:283-296`、`session_runs.py:401-411`、`session_runs.py:438-466` 基本对得上。）

### 第 2 条（L316 小结第 1 条）—— 维持

同第 1 条，同一份证据链（`src/agents/research/agent.py:277/297`、`src/services/session_runs.py:410`、`:449`）。文档 L316 写「run 补一个 `turn_end(failed)`」，与实际发给前端的 `turn_end(status="completed")` 不符。
（诊断给的 `session_runs.py:459` 指代不准——第 459 行不是 `turn_end` 的 status 字段，只是行号细节问题。）

### 第 3 条（L226：「文案是「正在保存当前进度」」）—— 维持

源码 `src/services/session_runs.py:288-297`：

```
"event": "status",
"status": "cancel_requested",
"message": "已收到停止请求，正在保存当前进度",
```

文档用「」引号逐字引用，却漏掉前半句「已收到停止请求，」。诊断成立（`session_runs.py:294` 行号也准确）。

### 第 4 条（L240：「都做三件事」）—— 维持（但诊断自己的行号有错）

源码三个终局函数的实际动作序列（以 `_finalize_success` 为例，`src/services/session_runs.py:445-466`）：

1. `assistant_buffer.persist(...)`（`:446`）
2. `self.repo.set_status(session_key, "completed")`（`:449`）
3. `self.repo.set_run_started_at(session_key, None)`（`:450`）
4. `self.repo.set_active_run_id(session_key, None)`（`:451`）
5. 发 `turn_end`（`:455-466`）
6. `finally` 里 `self.broker.close_run_nowait(run_id)`（`:480-481`）

三条路径对称，只是 `set_status` 的值不同：`completed`（`:449`）、`cancelled`（`:494`）、`failed`（`:539`）。

文档把状态写回从「三件事」里抽走、只在下句说「只有失败才把会话标成 `failed`」，读者确实会以为成功/取消两条不写状态——诊断这条判断成立。

**但诊断自己的描述有两处不准**：
- 「第一件是写回会话状态」错。第一件是 `assistant_buffer.persist`，`set_status` 是第二件。
- 行号 `447`（实际 449）、`538`（实际 539）各有 1~2 行偏差；只有 `494` 是对的。

### 第 6 条（L214：「用保守草稿继续」）—— 维持

源码 `src/agents/review/writing.py:725-745`：

- `:726` 函数 docstring 明写「模型不可用时返回一个明确的失败占位，**不生成伪正文**」。
- `:730-733` 注释说明「改造前会生成一段『像正文的文字』……现在改为返回一个明确的失败占位」。
- `:738` 正文第一行就是 `[本节写作失败：写作模型不可用]`，结尾是「请后续人工补充或重新运行写作模型。」

「保守草稿」这个词只出现在 `warnings` 文案里（`:204`「未配置可用的写作模型，已生成保守正文草稿。」、`:223`「写作模型调用失败，已生成保守正文草稿：{raw_output}」）。文档把 warnings 的措辞当成了行为描述。诊断成立。
（诊断给的 `writing.py:203`、`:212` 应为 `:204`、`:223`；`:725-745` 准确。）

### 第 7 条（L271：口径对齐 `failed` / `cancelled` / `download_failed`）—— 维持

`eval/run_reliability_stats.py`：

- `:47` `for status in ['completed', 'failed', 'cancelled', 'interrupted']:` —— 会话状态口径是这四个，**没有** `download_failed`。
- `:167-172` 单列「下载成功率」（`download_success_rate`）。
- `:173-180` 单列「Abstract Fallback 率」。
- `:333-341` 工具失败率、`:343-349` 下载成功率的告警分支。

`download_failed` 是下载层的出口状态字面量（`src/paper_retrieval/download.py`），不是会话状态，脚本里根本没这个口径。诊断成立，行号也对。

### 第 8 条（L37 内部前后不一致）—— 维持

文档 L37 原文「下面十个决策。每个决策都说清**三件事**：要解决什么问题、还考虑过哪些做法、最后选了什么、代价是什么。」冒号后列了四项。逐条核对 L39-L145 的十张卡片，实际写的确实是四件事（要解决的问题 / 考虑过的做法 / 最终选择 / 代价），十张卡片每张都有「代价是……」那句。诊断成立。（这条是文档内部一致性问题，不涉及源码，诊断自己也标注了。）

### 第 9 条（行号区间全部对得上）—— 维持

逐条实测：

| 文档引用 | 实际源码 | 结论 |
| --- | --- | --- |
| `83:84:src/api/app.py`（L248-251） | `src/api/app.py:83` = `interrupted = sessions_repo.reset_stale_runs()`；`:84` = `purged = sessions_repo.purge_stream_events()` | 逐字一致 ✅ |
| `src/services/session_runs.py:30`（L234） | 第 30 行 = `NON_PERSISTED_EVENT_TYPES = ("delta", "reasoning_delta")` | ✅ |
| `src/repositories/sessions/sqlite.py:535`（L242） | 第 535 行 = `def _connect(self) -> sqlite3.Connection:`；`busy_timeout` 在 546、`journal_mode` 在 551 | ✅ |

全文「起始行:结束行:文件」格式的引用块只有 L248-251 这一处，另外两处是内联 `文件:行号`，共 3 处，与诊断所说一致。

---

## 二、被推翻的条目

### 第 5 条（L238：「跳过写库，但仍然通知结束。」）—— 推翻（降级为「措辞可再精确」，不成立为「与源码不符」）

诊断原文断言：会话被删时 `except SessionError` 分支只记日志、「**不再发 `turn_end` 事件**」，结束信号来自 `finally` 的 `broker.close_run_nowait`。

**「不发 turn_end」这部分我核实属实**：`src/services/session_runs.py:466-469`（success）、`:511-513`（cancelled）、`:570-572`（failure）三处 `except SessionError` 都只调 `_log_session_gone(...)`，随后 `finally` 关闭 broker（`:481`、`:524`、`:583`）。

**但这条「与源码不符」的定性站不住**，理由三條：

1. **文档从没说过那里会发 `turn_end`。** 文档上一句（L238）写的是「收尾写库失败……也要关掉 SSE 推送，**并发出结束信号**」，下一句才是「删掉会话也是同理：跳过写库，但仍然通知结束」。「同理」承接的是「关掉 SSE 推送」，而源码 `finally` 里的 `broker.close_run_nowait(run_id)` 恰好就是关掉这条 run 的事件流——文档这句与源码是**一致**的。
2. **前端确实以「连接关闭」为结束信号**，文档的「通知结束」名副其实。`front/src/api/sessions.ts:131-133` 把 `onerror` 交给上层；`front/src/views/ChatView.vue:744-800` 的 `onError` 里，`source.readyState === EventSource.CLOSED`（服务端关流）就走「退避 → `reloadCurrentThread()`」把状态刷成终态；`readyState !== CLOSED` 才走 `scheduleStaleStreamCheck`。也就是说「流被关掉」正是前端认定的「这一轮结束了」。
3. **诊断给的行号大面积错误**：它写 `453-456`、`492-495`、`539-543`（实际 466-469、511-513、570-572），写 `458`、`498`、`570`（实际的 `finally` 关闭调用在 `481`、`524`、`583`）。`570` 那一处实际是 `_finalize_failure` 的 `except SessionError`，被误当成了 `finally`。

诊断给出的改写（「不发 `turn_end`，但 `finally` 里仍会关掉事件流，前端以连接关闭作为结束信号」）作为**措辞澄清**是有价值的；但把它列为「与源码不符」并据此说文档讲错了，属于定性过重——**推翻**。

---

## 三、新发现（诊断漏掉的、与源码不符之处）

### 新发现 1（中低置信度，措辞问题）：L280「下载 URL 有白名单」在源码里找不到对应实现

文档 L280 写「下载 URL 有白名单。arXiv 有路径限制，避免工具变成任意 URL 抓取器。」后半句属实，前半句没有依据：

- `src/paper_retrieval/download.py:299-303` 的 `_is_safe_http_url()` 只做两件事——`parsed.scheme.lower() in {"http", "https"}` 且 `parsed.netloc` 非空。这是**协议白名单**，不是 URL / 主机白名单。
- 全仓检索 `localhost` / `127.0.0.1` / `is_private` / `host_allow` 在 `src/paper_retrieval/` 下**零命中**，没有主机黑名单或内网地址拦截。
- 真正的「白名单」在 arXiv 侧：`src/paper_retrieval/arxiv_access.py:44-46`（`_EXPORT_ALLOWED_PREFIXES = ("/api/query",)`、`_SITE_ALLOWED_PREFIXES = ("/abs", "/pdf", "/html")`）+ `is_allowed_arxiv_url()`（`:73-81`），那是**路径**白名单，文档 L191 已经说对了。

建议把 L280 的「下载 URL 有白名单」改成「下载地址只允许 http / https」，或直接与 L191 合并。
（诊断的「其余核对结论」把「下载五条出口」整体标为一致，没有抽查这句话。）

### 新发现 2（诊断自身行号错误，非文档错误）—— 汇总提交

这条不算文档的错误，但既然是「逐行核对」，一并记下诊断在行号上的系统性偏差，供改写时别照抄：

| 诊断给出的行号 | 实际行号 | 出处 |
| --- | --- | --- |
| `session_runs.py:453-456 / 492-495 / 539-543`（三处 `except SessionError`） | `466-469 / 511-513 / 570-572` | 第三节第 5 条 |
| `session_runs.py:458 / 498 / 570`（三处 `finally` 关流） | `481 / 524 / 583` | 第三节第 5 条 |
| `session_runs.py:447`（`set_status completed`） | `449` | 第三节第 1、4 条 |
| `session_runs.py:538`（`set_status failed`） | `539` | 第三节第 4 条 |
| `session_runs.py:459`（被当作 `turn_end` 的 status 行） | `turn_end` 体在 `455-466` | 第三节第 2 条 |
| `writing.py:203 / 212`（两处 warnings） | `204 / 223` | 第三节第 6 条 |
| `common/base.py:57-61`（被引为 `llm_profile` 相关） | 实为 `BaseAgent` 类 docstring；`llm_profile` 逻辑在 `base.py:15-39` | 「其余核对结论」 |
| `relevance.py:326-346` | 准确 ✅ | 「其余核对结论」 |

### 新发现 3（文档表述与源码注释口径不同，供判断）—— L174「`llm_profile` 在这条路径下不生效」

文档说法本身与源码行为一致（`src/agents/common/base.py:15-17`：`SUPPORTED_LLM_PROFILES = frozenset({"default_agent"})`，注释明写「后端节点只能用 default_agent 这一个内置档位，所有子 Agent 都从它取模型」；`AgentSpec.__post_init__` 在 `base.py:37-39` 会对非白名单档位直接抛 `ValueError`）。这不是错误，只是提醒改写时不要把它写成「配置被忽略」，准确说法是「这个字段当前只允许 `default_agent` 一个值」。

---

## 四、独立抽查的代码事实（超出诊断范围的旁证）

以下是我为「独立抽查至少 8 处」而额外逐条验证的文档断言，**全部与源码相符**：

1. `ok` 判定 = `finish_reason != "error"` —— `src/llm/base.py:105-114`（`@property def ok`，`return self.finish_reason != "error"` 在 `:114`）。
2. 退避：`min(500 * (2 ** (attempt - 1)), 32000)` + `random.uniform(0, base*0.25)` + 与 `Retry-After` 取大值，服务端建议上限 `_RETRY_AFTER_CAP_SECONDS = 60.0` —— `src/llm/base.py:443-450`、`:21`。
3. 可重试三类 + 尊重显式标记 —— `src/llm/base.py:563-576`（`error_should_retry` 优先，否则 `{"rate_limit","server_error","connection"}`）；`connection` 兜底置 `error_should_retry=True` 在 `:553-560`。
4. 信号量只包住发请求那一次、睡眠在信号量外 —— `src/llm/base.py:391-419`（`async with self._semaphore:` 在 `:399`，`await asyncio.sleep(...)` 在 `:418`，已在 `with` 之外）。
5. 默认 `max_retries=3` / `max_concurrency=2` —— `src/llm/base.py:273-274`。循环是 `for attempt in range(self.max_retries)`（`:396`），所以文档 L310「最多尝试 3 次」是**总尝试次数**，表述正确。
6. 精读三路并发 —— `src/agents/reading/deep_read.py:72` `DEEP_READ_MAP_CONCURRENCY = 3`，`asyncio.Semaphore(DEEP_READ_MAP_CONCURRENCY)` 在 `:583`。
7. 下载并发默认 3 + run 级 `AsyncClient` —— `src/runtime/resources.py:19`（`download_concurrency: int = 3`）、`:36`（`download_semaphore`）、`:39`（`http_client`）。
8. 摘要降级常量与提示词 —— `src/models/deep_read.py:21` `DEEP_READ_SOURCE_ABSTRACT = "abstract_fallback"`；`src/agents/common/prompts.py:473`「short_summary 必须在开头注明『基于摘要的精读』」；`fulltext_available` / `notice` 在 `src/agents/reading/deep_read.py:479-480`。
9. 守则第 7 条 —— `src/agents/common/prompts.py:375`。
10. 终答引用自检 / 工具轮次 —— `src/agents/research/agent.py:116` `FINAL_ANSWER_MAX_REPAIR = 1`、`:54` `MAX_TOOL_ROUNDS = 20`。
11. 写作限次 —— `src/agents/review/writing.py:65` `MAX_TOOL_CALLS = 5`、`:68` `MAX_REVISION_ROUNDS = 2`；`:116` 是 `while True:` 的安全循环，全文无 checkpoint 引用（文档 L207 说法成立）。
12. `_ReviewFailed` 带续跑编号 —— `src/agents/review/pipeline.py:199`、`:318-324`；`checkpoints.db` 在 `:368`；`NodeCancelledError → asyncio.CancelledError` 在 `:310-317`。
13. `WorkflowCancellation.request()` 置位并 `self._event.set()` —— `src/runtime/workflow.py:42-45`。
14. `reset_stale_runs` 把残留会话置 `interrupted`、清 `run_started_at` 与 `metadata.active_run_id` —— `src/repositories/sessions/sqlite.py:1252-1270`。
15. `start_run` 409 —— `src/services/session_runs.py:206-207`。
16. broker 先补发历史再接直播 —— `src/services/session_runs.py:150-186`（`:157-167` 先 `yield history`，`:168-176` 再转实时）。
17. `AssistantMessageBuffer` 落在 `src/services/sessions.py:25`（诊断「第五部分第 7 条」提到的路径准确）；写入表名 `session_message` 属实（`src/repositories/sessions/sqlite.py:476`）。
18. 工作区状态字面量：`WORKSPACE_FILE_NAME = "papers.json"`（`src/models/workspace.py:27`）、单篇损坏跳过（`:357-361`）、`"status": "cached"`（`src/agents/research/tools.py:1504`）、`reused_cache`（`src/paper_retrieval/download.py:71/99`）。
19. 检索侧：概念组自动放宽（丢最末一组，`src/agents/research/tools.py:613-643`）与 arXiv 直链回填（`src/paper_retrieval/service.py:72-98`）都属实，文档 L75 的说法成立。
20. `invalid_request` 是真实错误字面量（`src/llm/base.py:649`），文档 L269 的说法成立。
21. `vision_image_block` 分 OpenAI / Anthropic 两种图块 —— `src/llm/base.py:672-686`；插图笔记在 `src/utils/fulltext/figures.py`（`:419` 调用）。
22. 正文转写：`src/utils/fulltext/convert.py:44` 版本注释「8 -> 9：正文整页改由 Nougat 写成 Markdown，**不再把公式和表格截图交给精读模型**」——文档 L212「公式和表格不再截图交给模型」与**当前**版本一致。（顺带一提：`src/agents/reading/deep_read.py:279-281` 那句「让它把公式截图转成 LaTeX」是改造前的**过时注释**，文档没有跟着它写错。）

---

## 五、一句话总评

这篇文档与源码的符合程度**总体良好**：四层骨架、下载出口清单、退避与重试参数、`workspace_lock` / `NON_PERSISTED_EVENT_TYPES` / `checkpoints.db` 等具体事实，抽查 20 余处全部对得上，全文 3 处行号引用逐字准确；**唯一真正讲错的是「模型重试全失败」这条收场的终态**——文档承诺 `turn_end(failed)`，源码走的是 `completed`，新人据此排查会被带偏，改写时必须优先修掉这一处。
