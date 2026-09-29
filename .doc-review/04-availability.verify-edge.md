# 04-availability.verify-edge — 边界与反例视角的对抗式复核

复核对象：`.doc-review/04-availability.diag.md` 第三节「与源码不符的地方」共 9 条
被诊断文档：`doc/arch/04-availability.md`
复核视角：**找反例与边界**。每一条都先假设诊断是错的，回到源码找「文档说的是不是某个分支 / 某个配置下的行为」；专门盯默认值、数量、顺序、是否落库、失败时的降级路径。
复核时间：2026-09-29

结论速览：9 条里 **8 条维持原判，1 条推翻**（第 5 条「与源码不符」的定性不成立）。
另自查出 4 处诊断没提到的偏差或边界（第三部分），并独立抽查了 24 处代码事实（第四部分）。

---

## 一、维持原判的条目

### 第 1 条（L168：「再由 run 服务补一个 `turn_end(failed)`。」）—— 维持

条目原文：文档说主 Agent 收到 `ok` 为假的响应后用固定话术收尾，「再由 run 服务补一个 `turn_end(failed)`」。

核对证据：

- `src/agents/research/agent.py:277` `if not response.ok:` → `:292-296` 发固定话术助手消息（`LLM_FAILURE_MESSAGE`，常量在 `:147`）→ `:297` **`return`**（正常返回，不是抛异常）。
- `src/services/session_runs.py:398-410`：handler 正常返回后只判断 `self.broker.is_cancel_requested(run_id)`；没收到取消就调 `_finalize_success`（调用点 `:410`）。
- `_finalize_success`（`:438`）里 `set_status(session_key, "completed")`（`:449`），发出的 `turn_end` 是 `"status": "completed"`（`:454-465`，状态行在 `:460`）。

边界检查（反例找不到）：我逐条找过「模型调用失败能不能变成 failed」的其他出口，全部否掉——

1. 主 Agent 强制收敛那条路同样正常返回：`agent.py:541-543` 在 `response.ok` 为假时把 `LLM_FAILURE_MESSAGE` 当终答，不抛异常。
2. 子 Agent / 工具的模型失败会被折成工具结果，不会穿透到 handler 外面。
3. 唯一会发出 `turn_end(status="failed")` 的是 `_finalize_failure`（`:563`），而它只被 `except asyncio.CancelledError`（`:412-425`）与 `except Exception`（`:426-433`）调用。
4. 补一个诊断没给的锚点：真正会走 `turn_end(failed)` 的是**配置不完整**那条分支——`chat_runtime._load_llm_snapshot` 抛 `SessionError`（`src/services/chat_runtime.py:206-228`），异常穿透 handler 后进 `_finalize_failure`；源码注释自己就写明这条分支是「error 事件 + turn_end(failed)，前端永远不会死等」（`src/services/chat_runtime.py:88-90`）。

也就是说：文档 L170 描述的那条路确实发 `failed`，L168 描述的这条却发 `completed`——诊断指出的正是这处张冠李戴，站得住。

### 第 2 条（L316 小结第 1 条「run 补一个 `turn_end(failed)`」）—— 维持

同一份证据链：`src/agents/research/agent.py:277/297` → `src/services/session_runs.py:410` → `:449`（`completed`）→ `:454-465`（`turn_end` 的 status 是 `completed`）。文档小结与正文错在同一处。

### 第 3 条（L226：「文案是「正在保存当前进度」」）—— 维持

`src/services/session_runs.py:286-299`：

```
"event": "status",
"status": "cancel_requested",
"message": "已收到停止请求，正在保存当前进度",
```

文档用「」引号定义「文案」，却只截了后半段。全仓 grep `正在保存当前进度` 只有这一处（`src/services/session_runs.py:294`），前端侧没有第二份文案，所以这确实是一次不完整的逐字引用。

边界说明：这是**引用精度**问题，不是事实反向——新人拿着「正在保存当前进度」照样能搜到这一行，所以诊断把它列为「与源码不符」略重，但改写建议（补全前半句）应当采纳。

### 第 4 条（L240：「都做三件事」）—— 维持

三条收尾路径的实际动作（以成功路径为例，`src/services/session_runs.py:447-481`）：

1. `assistant_buffer.persist(...)`（`:448`）
2. `repo.set_status(...)`（`:449`）
3. `repo.set_run_started_at(session_key, None)`（`:450`）
4. `repo.set_active_run_id(session_key, None)`（`:451`）
5. 发 `turn_end`（`:454-465`）
6. `finally` 里 `close_run_nowait(run_id)`（`:480-481`）

三条路径对称，只有 `set_status` 的值不同：`completed`（`:449`）/ `cancelled`（`:494`）/ `failed`（`:539`）。文档把状态写回从「三件事」里抽走、只在下句说「只有失败才把会话标成 `failed`」，读者确实容易以为成功 / 取消两条不写状态。诊断这条判断成立。

边界检查：这里没有「只在某个配置下才写状态」的分支——`repo.set_status` 内部先 `self.get(key)`（`src/repositories/sessions/sqlite.py:1216-1219`），三条路径都无条件调用，所以「成功 / 取消不写状态」在任何配置下都不成立。**推翻不了**。

但要纠正诊断自己的两处不准（供改写时别照抄）：

- 「第一件是写回会话状态」错——第一件是 `assistant_buffer.persist`，`set_status` 是第二件。
- 行号 `447`（实际 449）、`538`（实际 539）各有 1 行偏差；只有 `494` 是准的。

### 第 6 条（L214：「这一节用保守草稿继续」）—— 维持

`src/agents/review/writing.py:725-745`：

- `:726` docstring 明写「模型不可用时返回一个明确的失败占位，**不生成伪正文**」；
- `:730-733` 注释说明「改造前会生成一段『像正文的文字』……现在改为返回一个明确的失败占位」；
- `:738` 正文第一行就是 `[本节写作失败：写作模型不可用]`，结尾是「请后续人工补充或重新运行写作模型。」。

边界检查：触发点有三处，全都返回同一个占位——`llm is None`（`:197`）、`not response.ok`（`:216`）、模型返回空正文（`:228`）。所以不存在「某个正常配置下会写出真草稿」的分支。

唯一要找补的：**「保守」这个词来自源码自己的 warning 文案**（`:204`「未配置可用的写作模型，已生成保守正文草稿」、`:223`「写作模型调用失败，已生成保守正文草稿」）。文档不是凭空造词，而是把 warning 的措辞当成了正文形态描述——结论仍然是「保守草稿」这个说法让读者以为拿到了一段可用正文，诊断的改写（写明是失败占位）应当采纳。

### 第 7 条（L271：口径对齐 `failed` / `cancelled` / `download_failed`）—— 维持

`eval/run_reliability_stats.py`：

- `:47` `for status in ['completed', 'failed', 'cancelled', 'interrupted']:` —— 会话状态就是这四个，**没有** `download_failed`；
- `:165-172` 单列「下载成功率」；`:173-180` 单列「Abstract Fallback 率」。

边界检查：我对整个 `eval/` 目录 grep 过 `download_failed`（含 `eval/lib/`），**零命中**。`download_failed` 只是下载层的返回值字面量（`src/paper_retrieval/download.py:265`），不进入会话状态口径。

顺带补一句文档漏掉的：脚本真正统计的降级口径是 `abstract_fallback`（`eval/lib/event_stats.py:590`、`:726-730`），文档写 `download_failed` 是拿下载层的词替换了评测层的词。

### 第 8 条（L37 内部前后不一致）—— 维持

文档 L37「每个决策都说清**三件事**：要解决什么问题、还考虑过哪些做法、最后选了什么、代价是什么。」冒号后列了四项；逐张核对 L39-L145 的十张卡片，实际写的都是四件事（要解决的问题 / 考虑过的做法 / 最终选择 / 代价），只是「代价」那句没有加粗标签。这一条不涉及源码，我按文档本身核，成立。

### 第 9 条（行号区间全部对得上）—— 维持

| 文档引用 | 实测 | 结论 |
| --- | --- | --- |
| `83:84:src/api/app.py`（L248-251） | `:83` = `interrupted = sessions_repo.reset_stale_runs()`；`:84` = `purged = sessions_repo.purge_stream_events()` | 逐字一致 ✅ |
| `src/services/session_runs.py:30`（L234） | 第 30 行 = `NON_PERSISTED_EVENT_TYPES = ("delta", "reasoning_delta")` | ✅ |
| `src/repositories/sessions/sqlite.py:535`（L242） | 第 535 行 = `def _connect(self) -> sqlite3.Connection:`；`busy_timeout` 在 546、`journal_mode` 在 551 | ✅ |

---

## 二、推翻的条目

### 第 5 条（L238：「跳过写库，但仍然通知结束。」）—— 推翻

诊断原文断言：会话被删时 `except SessionError` 分支只记日志、「**不再发 `turn_end` 事件**」，结束信号来自 `finally` 的 `broker.close_run_nowait`。

**先承认它对的那一半**：三条 `except SessionError` 分支确实只调 `_log_session_gone`，跳过 `turn_end`——`src/services/session_runs.py:466-469`（success）、`:511-513`（cancelled）、`:570-572`（failure）；随后 `finally` 关流在 `:481` / `:524` / `:583`。我全程没找到能推翻这一点的反例：`repo.append_message` 与 `repo.set_status` 都会先 `self.get(key)`（`src/repositories/sessions/sqlite.py:1058`、`:1216-1219`），会话没了必抛 `SessionError`，异常在 `turn_end` 之前就跳出去了。

**但「文档讲错了」这个定性站不住**，四条理由：

1. **文档从没说过那里会发 `turn_end`。** 上一句（L238）写的是「收尾写库失败……也要关掉 SSE 推送，**并发出结束信号**」，下一句才是「删掉会话也是同理：跳过写库，但仍然通知结束」。「同理」承接的是「关掉 SSE 推送」这个动作，而源码 `finally` 里的 `close_run_nowait(run_id)` 正好就是关掉这条 run 的事件流。
2. **前端确实以「连接关闭」为结束信号。** `close_run_nowait` 往每个订阅队列塞一个 `None`（`session_runs.py:138-149`），SSE 生成器读到 `None` 就结束（`:150-186`）；前端 `onError` 里 `readyState === EventSource.CLOSED`（服务端关流）走「退避 → reloadCurrentThread」收尾（`front/src/views/ChatView.vue:740-800`）。所以「流被关掉」就是前端认定的「这一轮结束了」。
3. **源码注释自己就是这么写的。** `_finalize_success` 的 `except SessionError` 注释：「但下面仍要照常给前端发"运行结束"的信号，否则页面会永远等不到结果」（`session_runs.py:467-468`）。文档这句是照源码注释的意图写的，不是自己编的。
4. **诊断给的行号大面积不准**：`453-456 / 492-495 / 539-543` 应为 `466-469 / 511-513 / 570-572`；`458 / 498 / 570` 应为 `481 / 524 / 583`（`570` 那处其实是 `_finalize_failure` 的 `except SessionError`，被当成了 `finally`）。

结论：诊断给的改写（「不发 `turn_end`，但 `finally` 里仍会关掉事件流，前端以连接关闭作为结束信号」）作为**措辞澄清**值得采纳；但把它列为「与源码不符」属于定性过重——**推翻**。

---

## 三、新发现（诊断漏掉的偏差 / 边界）

### 新发现 1（失败降级路径，建议改写）：决策 5 表里「map 阶段的模型调用失败 → 直接返回 failed」讲宽了

- 文档 L91：`map 或 reduce 阶段的模型调用失败 | 直接返回 failed`
- 源码：map 是**逐块并发**，单块失败只把该块笔记记成空串，不中断整批（`src/agents/reading/deep_read.py:579` 函数 docstring、`:592-640`）；只有**所有**块都失败才硬失败（`:349-350` `if not any(notes): return _fail(deps, "全部正文片段精读失败，无法汇总报告")`）。reduce 解析失败才是无条件硬失败（`:374-376`、`:386-388`）。
- 边界后果：**部分** map 失败 + 部分成功 → 静默继续，报告照常出，用户看不出有几块没读。文档这句话会让新人以为「只要有一个块调模型失败，这篇精读就 failed」。
- 建议：改成「map 全部片段都失败、或 reduce 阶段失败 → 直接返回 `failed`；map 里个别片段失败只是少读一块，不会降级成摘要」。

### 新发现 2（安全叙述，建议改写）：L280「下载 URL 有白名单」在源码里没有对应实现

- 文档 L280：`下载 URL 有白名单。arXiv 有路径限制，避免工具变成任意 URL 抓取器。`
- 源码：`src/paper_retrieval/download.py:299-303` 的 `_is_safe_http_url()` 只做两件事——`parsed.scheme.lower() in {"http", "https"}` 且 `parsed.netloc` 非空。这是**协议允许**，不是「URL 白名单」；`src/paper_retrieval/` 下没有主机黑白名单或内网地址拦截。
- 真正的白名单是 arXiv 的**路径**白名单（`src/paper_retrieval/arxiv_access.py` 的 `_EXPORT_ALLOWED_PREFIXES` / `_SITE_ALLOWED_PREFIXES` + `is_allowed_arxiv_url()`），文档 L191 已经说对了。
- 建议：L280 前半句改成「下载地址只允许 http / https」，或与 L191 合并（诊断第六部分第 4 条也建议删掉这处重复）。

### 新发现 3（入口不存在，建议改写）：L176「脱离对话的阅读入口」在当前代码里没有调用方

- 文档 L176：`脱离对话的阅读入口是另一条路。src/agents/reading/relevance.py 的 load_read_agent_llm() 自己装配一次，档位按 ReadAgent.spec.llm_profile 取。`
- 源码：`load_read_agent_llm()` 只被 `build_read_agent(llm="auto")` 的默认分支调用（`src/agents/reading/relevance.py:351`）；而全仓（除 `.venv`）唯一的 `build_read_agent(...)` 调用点传的是**显式快照**（`relevance.py:295`，快照来自 `src/agents/research/tools.py:1359` 的 `context.llm`）。也就是应用里没有任何入口会走「自己装配一次」这条分支。
- 另外 `SUPPORTED_LLM_PROFILES = frozenset({"default_agent"})`（`src/agents/common/base.py:17`）且 `ReadAgent.spec.llm_profile="default_agent"`（`src/agents/reading/relevance.py:61`），所以即便走到这条分支，取到的档位也只可能是 `default_agent`。
- 建议：把这句从「另一条入口」改成「库函数留了一条自装配的口子（当前应用内没有调用方）」，免得新人去找一个不存在的入口。注意 L174「子 Agent 复用主 Agent 快照」这半句是**对的**。

### 新发现 4（边界澄清）：决策 2「瞬时并发可能略高于上限」只在跨实例时成立

- 文档 L57：`代价是同一 Provider 的瞬时并发可能略高于上限。请求总数的上限仍由名额控制。`
- 源码：名额是**每个 provider 实例**一把 `asyncio.BoundedSemaphore(max_concurrency)`（`src/llm/base.py:277`），并且只包住真正发请求那一次调用（`:399-418`，`async with` 结束后才 sleep）。所以**单个实例**的在飞请求数严格 ≤ 上限，不存在「瞬时略高于上限」。
- 会「略高于上限」的唯一情形是同一份模型配置存在多个实例：每个 run 各 `make_provider` 一次（`src/services/chat_runtime.py:216`，在 handler 里按 run 装配），设置页「测试连接」又是另一份（`src/services/settings.py:314`）。两个会话同时跑，对同一家服务商的在飞请求就会到「实例数 × 上限」。
- 建议：写成「上限是按 provider 实例算的；两个会话同时跑时，对同一家服务商的并发会是实例数 × 上限」。这条属于边界澄清，不算硬错误（`doc/LLM-README.md:99` 有同样的措辞，两篇要一起改）。

---

## 四、独立抽查的代码事实（超出第三节范围，共 24 处）

为防「诊断说一致就一致」，我把文档里能落到源码的断言重新核了一遍，除上面 4 处外**全部相符**：

1. `ok` 判定 = `finish_reason != "error"` —— `src/llm/base.py:113-114`。文档 L45、L151 准确。
2. 重试循环是**总次数**：`for attempt in range(self.max_retries)`（`src/llm/base.py:396`），默认 3（`:273`）。所以文档 L161「最多循环 max_retries 次（默认 3）」与 L310「最多尝试 3 次」都对——**诊断第五部分第 4 条提醒得很对**（该写清是「第一次 + 最多 2 次重试」），但那是「可以写得更明确」，不是文档算错了。
3. 退避公式 `min(500 * 2**(attempt-1), 32000)` + 0~25% 抖动 + 与 `Retry-After` 取大值 + 服务端建议封顶 60 秒 —— `src/llm/base.py:439-450`、`:21`。文档 L65、L167 准确。
4. 可重试三类与鉴权分类：`_should_retry` 先尊重显式 `error_should_retry`，否则 `{"rate_limit","server_error","connection"}`（`src/llm/base.py:575-577`）；`_http_error_kind` 把 429 → rate_limit、401/403 → auth、5xx → server_error、其余 → invalid_request（`:630-651`）。文档 L166 准确。
5. 「睡眠放在信号量外面」—— `async with self._semaphore:` 在 `src/llm/base.py:404`，`await asyncio.sleep(...)` 在 `:418`（已在 `with` 外）。文档 L55、L165、L310 准确。
6. 并发默认 2 —— `src/llm/base.py:274`。文档 L158 准确。
7. 精读 map 并发 3 —— `src/agents/reading/deep_read.py:72`、`:583`。文档 L55、L165「精读三路并发」准确。
8. 下载并发默认 3 + run 级共享 `AsyncClient` —— `src/runtime/resources.py:19`、`:36-40`；下载层取用见 `src/paper_retrieval/download.py:115-125`。文档 L193 准确。
9. 下载的五个出口 —— 命中磁盘缓存返回 `downloaded` + `reused_cache`（`:89-100`）；`no_url`（`:104-108`）；只允许 http / https 与 arXiv 路径限制（`:110-112`、`:140-142`）；超时 / 过大 / 非 PDF、HTML → `download_failed` 且带中文原因（`_download_failed` 定义在 `:256-265`，调用点 `:137-160`）。文档 L188-192 准确。
10. 工具层缓存短路 —— `src/agents/research/tools.py:1502-1504`「工作区已标记全文缓存过，直接返回 `cached`，不重复下载」。文档 L189 准确。
11. `delta` / `reasoning_delta` 不落库 —— 常量 `src/services/session_runs.py:30`；`emit` 里 `turn_end` 直通、按 `NON_PERSISTED_EVENT_TYPES` 决定落不落库（`:341-355`）。文档 L113、L234 准确。
12. 缓冲区落 `session_message` 表 —— `src/services/sessions.py:47-61`（`assistant_buffer.persist`）→ `src/repositories/sessions/sqlite.py:240`（`INSERT INTO session_message`）。文档 L234 准确。
13. 启动自愈两件事 —— `src/api/app.py:83-84`；实现见 `sqlite.py:1252-1270`（置 `interrupted`、清 `active_run_id`）、`:1273-1283`（删 `delta` / `reasoning_delta` 事件）。文档 L133、L253-255 准确。
14. `start_run` 409 —— `src/services/session_runs.py:206-207`。文档 L143、L261 准确。
15. 取消顺序：先发 `cancel_requested` 事件再 `task.cancel()` —— `src/services/session_runs.py:286-301`。文档 L226 的**顺序**说法准确（只有文案引用不全，见第 3 条）。
16. SQLite 参数：`busy_timeout = 10000`（`:546`）、`journal_mode = WAL`（`:551`），都在 `_connect`（`:535`）里；Python `sqlite3.connect` 默认 `timeout=5.0`，所以文档 L242「从默认 5 秒放宽到 10 秒」成立。
17. 「已停止」/「已中断」文案 —— `front/src/views/ChatView.vue:283`、`:285`。文档 L228、L319 准确。
18. 守则第 7 条 —— `src/agents/common/prompts.py:375`。文档 L195 准确。
19. 四个次数上限 —— `MAX_TOOL_CALLS = 5`、`MAX_REVISION_ROUNDS = 2`（`src/agents/review/writing.py:65`、`:68`）；`FINAL_ANSWER_MAX_REPAIR = 1`、`MAX_TOOL_ROUNDS = 20`（`src/agents/research/agent.py:116`、`:54`）。文档 L209-214 准确。
20. 残缺 tool_calls 配对修复 —— `src/agents/research/agent.py:661-712`。文档 L257 准确。
21. 子 Agent 复用主 Agent 快照 —— 评价 / 精读 / 追问三处都传 `llm=context.llm`（`src/agents/research/tools.py:1590`、`:1663`、`:1731`）。文档 L174 前半句准确（后半句见新发现 3）。
22. 检索部分失败与概念组放宽 —— `response.errors[source_name]`（`src/paper_retrieval/service.py:224`）、丢最末一组自动放宽（`src/agents/research/tools.py:613-643`）、arXiv 直链回填（`src/paper_retrieval/service.py:65-100`）。文档 L75、L182 准确。
23. `NodeCancelledError → asyncio.CancelledError` —— `src/agents/review/pipeline.py:310-317`。文档 L228 准确。
24. 综述检查点库位置 —— `src/agents/review/pipeline.py:360-368`（`<sessions_dir>/<session_key>/checkpoints.db`），写作小节内部无检查点。文档 L206-207 准确。

---

## 五、一句话总评

这篇文档与源码的符合程度**总体良好**：我独立抽查 24 处代码事实，落到源码上只有 4 处偏差（一处讲宽、一处无依据、一处入口不存在，另加一处边界澄清），四层骨架、下载出口、退避与重试参数、取消顺序、落库分工、启动自愈都对得上；**唯一真正会把新人带偏的是「模型重试全部失败」这条收场的终态**——文档承诺 `turn_end(failed)`，源码走的是 `completed`，这是改写时必须优先修掉的一处。
