# doc/arch/01-agent-orchestration.md 源码复核（视角：逐行核对源码）

复核对象：`.doc-review/01-agent-orchestration.diag.md` 的「三、与源码不符的地方」全部 5 条
复核方式：每条都回到源码查证，默认诊断是错的；只有亲自在源码里看到证据、无法推翻时才承认
报告口径：维持 5 条（其中 1 条归类错误、2 条严重度需下调）；推翻 0 条；新发现 6 条

---

## 一、维持原判

### 【维持 1】`L119` 说写作进度回调用 `ContextVar` 传 —— 成立

条目原文：

> `L119` 文档说：「这里有一条真实的出错记录。写作的小节图曾经把进度回调函数放进图状态，立刻报 `Type is not msgpack serializable: function`。后来改成用 `ContextVar` 传。」；**源码实际：进度回调是当普通函数参数传的，全仓没有任何一处用 `ContextVar` 传写作进度回调**。

我找到的证据：

- `src/agents/review/pipeline.py:673`：`progress_callback=on_section_progress,` —— 回调是从节点函数当关键字参数传进 `write_section` 的，节点函数的依赖（`deps`）由闭包给。
- `src/agents/review/writing.py:81`：`progress_callback: Any | None = None,`（形参声明）
- `src/agents/review/writing.py:117`、`121`：一路当参数透传给 `plan_or_write` / `review_draft`
- `src/agents/review/writing.py:191`、`305`：这两个函数又把 `progress_callback` 收成形参，`382`–`397` 的 `_notify_section_progress` / `_notify_section_usage` 只是 `if callable(progress_callback): progress_callback(...)`
- `grep -rn "ContextVar" src/` 的全部命中：`src/agents/research/tools.py:138/140/164/180/185`（`_ACTIVE_CALL_VAR` 及其注释）、`src/utils/logging_utils.py:8/15/16/90`（日志上下文）、`src/agents/research/agent.py:438/447`（注释）。`grep -rn "contextvars\|contextvar" src/agents/review/` **零命中**。
- 另跑 `git log -S "ContextVar" -- src/agents/review/`，无任何提交曾在该目录增删过 `ContextVar`。
- 唯一的那处 `ContextVar`（`_ACTIVE_CALL_VAR`）确实参与子 Agent 链路，但作用和文档说的不同：`src/agents/research/agent.py:453` 用 `context.set_active_call(...)` 写入、`501` 清空，`src/agents/research/tools.py:1581/1655/1722` 在 deep_read / ask_paper / generate_review 三个工具 handler 里 `context.get_active_call()` 读出「当前是哪张工具卡」。也就是说，它解决的是**用量归到哪张卡**，不是**进度回调怎么送进小节图**。

结论：文档这句把「图状态不能放函数 → 改用闭包/参数传」和「主 Agent 侧用 ContextVar 记录当前工具调用」两件事记混了。诊断成立，且诊断给出的改法（「后来改成把回调当普通参数传给 `write_section`」）与源码一致。
一点补充（不改变结论）：诊断说「全仓 `ContextVar` 只出现在两处」，严格说是两个**文件**（`logging_utils.py` 里有 15、16 两个定义），且注释里另有若干处提及；实质结论（没有一处与综述写作有关）完全成立。

### 【维持 2】`L405` 说用量「累加」到工具卡 —— 成立

条目原文：

> `L405` 文档说：「……**用量也就累加到**对应的那张工具卡上。」；**源码实际：token 用量是覆盖，不是累加**。

我找到的证据：

- `src/runtime/workflow.py:522`–`526`：注释原文「只有模型回调明确提供用量时才覆盖卡片数字，普通进度更新不会把已有用量清零。」随后是两段覆盖赋值

  ```python
  if "input_tokens" in extra:
      payload["input_tokens"] = _token_count(extra.get("input_tokens"))
  if "output_tokens" in extra:
      payload["output_tokens"] = _token_count(extra.get("output_tokens"))
  ```

  赋值语义就是「有就覆盖」，没有累加动作。
- 同一篇文档 `L718`–`L719` 自己写的第 5 条规则就是「用量只在显式传入时**覆盖**……普通进度更新不会把已有用量清零」——`L405` 与 `L718` 前后打架。
- 简历红线文件 `memory/project_resume_redlines.md` 明确写着仍成立的红线：「**token 用量是覆盖而非累加**」。诊断说这条撞红线，成立。

一个需要说清的细节（不改变结论）：真正的「累加」发生在**上报之前**——`src/agents/review/pipeline.py:660` 用 `usage.collect(section_usage)` 把一次节点内多次模型调用的用量先攒成一个总数，再上报一次。所以「累加」这个词描述的是调用方，不是工具卡；`L405` 把「归到哪张卡」和「累加」写进了同一句，读起来像是卡片在做累加，与 `workflow.py:522` 的覆盖语义相反。诊断的改法（「用量也就记到对应的那张工具卡上」，与同篇 `L89` 的「记到别人头上」对齐）是对的。

### 【维持 3】`L743` 的 prompts.py 行号（约 543）—— 成立，但严重度应下调

条目原文：

> `L743` 文档说：「`src/agents/common/prompts.py` 还会在导入期额外守一道（**约第 543 行**）。」；**源码实际：第 543 行是 `_DEEP_READ_FIDELITY = skill_section("paper-deep-reading", "逐段阅读取舍")`，真正的守卫在 550，`raise` 在 551**。

我找到的证据（`grep -n` 确认）：

- `src/agents/common/prompts.py:543`：`_DEEP_READ_FIDELITY = skill_section("paper-deep-reading", "逐段阅读取舍")` —— 这是**取片段**那一行
- `src/agents/common/prompts.py:550`：`if "JSON" in _DEEP_READ_FIDELITY:`
- `src/agents/common/prompts.py:551`：`raise RuntimeError(`

所以文档给的行号指向的是紧邻的上一句，不是守卫本身。诊断指出的偏差（543 → 550）准确。
严重度需下调的依据：文档写的是「**约**第 543 行」，是明确打了折扣的近似引用；同一句对机制的描述（导入期检查、小节名「逐段阅读取舍」、抛 `RuntimeError`）与 `prompts.py:543`–`556` 完全一致。这属于行号漂移，不是事实错误。

---

## 二、维持事实、但归类错误（不算源码不符）

### 【归类问题】`L53`「你现在站在第 1 节的位置」

条目原文：

> `L53` 文档说：「你现在站在第 1 节的位置。」；**文档实际当前是「## 0. 先看全局」……**。建议改成：「你现在站在第 0 节的位置。」

我核到的事实：

- `doc/arch/01-agent-orchestration.md:15`：`## 0. 先看全局`
- `doc/arch/01-agent-orchestration.md:53`：`你现在站在第 1 节的位置。往后每一节，大致对应图上的一个方框。`
- `doc/arch/01-agent-orchestration.md:55`：`## 设计理念：为什么是这个形状`（未编号）
- `doc/arch/01-agent-orchestration.md:271`：`## 1. 请求怎么接到主 Agent`

文档自身的编号确实对不上，诊断**观察到的事实成立**。但它被放进「三、与源码不符的地方」是放错了位置：这一节的其他条目都会写「源码实际……证据：`文件:行号`」，而这一条从头到尾没有引用任何一行源码——它纯粹是文档内部的章节编号问题，应当归入诊断自己的「六、结构建议」第 4 条（那一条讲的正是 §0 与 §1 合并后编号自然对上）。

另外，诊断建议改成「你现在站在第 0 节的位置」，也只解决了一半：紧随其后的「设计理念」整节没有编号，读者读完第 0 节后接到的第一个小节仍然不是「第 1 节」。

---

## 三、维持（表述不一致，严重度低）

### 【维持 4】`L101` / `L160`「综述有十几个步骤」

条目原文：

> `L101` / `L160` 文档说：「综述有十几个步骤。」；**源码实际：`_build_review_graph` 一共只挂 8 个节点**。

我找到的证据：

- `src/agents/review/pipeline.py:450`–`457`：8 个 `add_node`，依次是 prepare / analyse_subtopic / analyse_overall / build_outline / write_section / write_abstract / compile_document / finalize（grep `add_node(` 只有这 8 行）
- `doc/arch/README.md:236`：同项目姊妹文档写的是「**外层 8 个节点做成图**」
- 更关键的是**同一篇文档自己**：`01-agent-orchestration.md:618`–`630` 的 mermaid 图恰好画了这 8 个节点，`634` 又说「全部写完才去 `write_abstract`」。读完那张图再回头看 `L101` 的「十几个步骤」，读者会对不上号。

诊断成立。需要说明的是（也是我说它严重度低的原因）：文档里的「步骤」是全篇的模糊用词，`write_section` 节点会按小节数反复执行，一次 12 节的综述实际执行次数确实会超过 8；所以这不是硬性的假话，而是「步骤」与「节点」两个粒度混用。诊断自己标为「低风险，属于粒度不一致」，判断合理。

---

## 四、推翻的条目

**无。** 诊断「三、与源码不符的地方」5 条里，4 条（`L119`、`L405`、`L743`、`L101/L160`）有源码证据支持，1 条（`L53`）事实成立但归类错误。我没有在任何一条上找到「文档对而诊断错」或「诊断把两处代码记混了」的情况。

---

## 五、新发现（诊断漏掉的 / 诊断自己写错的）

### 5.1 诊断自身的行号错误（两处，都在它说「全部正确」的清单里）

1. **`agent.py:246` 应为 `agent.py:241`**。诊断写「主 Agent 确实是手写 `for round_no in range(1, MAX_TOOL_ROUNDS + 1)`（`agent.py:246`）」。`grep -n "for round_no in range" src/agents/research/agent.py` 只有一处命中：**241**。该条结论（文档没写错）成立，引用的行号错了。
2. **`QA_READ_MAX_CHUNKS` / `QA_READ_CHUNK_MAX_CHARS` 的行号应为 59 / 60**。诊断写 `paper_qa.py:74`、`57`、`58`、`66`。实测：`QA_READ_MAX_CHUNKS = 6` 在 **59**，`QA_READ_CHUNK_MAX_CHARS = 1200` 在 **60**（`grep -n "^QA_"` 确认）；`QA_TOC_PREVIEW_CHARS = 60` 在 66 ✓、`QA_MAX_TOOL_ROUNDS = 4` 在 74 ✓，这两个对。

### 5.2 诊断内部自相矛盾

同一节里，条目 2 说 `L405`「撞上本项目的简历红线（「token 用量是覆盖而非累加」）」，而该节末尾又写「**没有触碰任何一条简历红线**（本篇没有出现「全流程基于 LangGraph」「熔断器」……）」。后半句的括号里列举的红线不含「用量覆盖」，所以它是在说另一件事；但两句话摆在同一节里，读者会以为前一条不成立。

### 5.3 文档 `L574` 的一句表述与数值不符

`L574`：「追问工具 `read_sections` 取回时另有每段 1200 字的截断。**这和装箱上限不是同一个数。**」
实测两个值都是 1200：装箱上限 `src/utils/fulltext/chunkers.py:116`（`max_chunk_characters = 1200`），追问截断 `src/agents/reading/paper_qa.py:60`（`QA_READ_CHUNK_MAX_CHARS = 1200`）。文档想说的应该是「不是同一个常量 / 不是同一处上限」，但字面写成「不是同一个数」就自相矛盾了。建议改成「这和装箱上限不是同一个常量，两个都是 1200」。

### 5.4 文档 `L289` 把一段节选称作「逐字源码」

`L289`：「下面这段是逐字源码。围栏的格式是 `起始行:结束行:文件路径`。」随后 `196:266:src/services/session_runs.py` 的块里用了 `# ...` 省略、并把真实的多行 `return`（源码 `261`–`266`）压成一行 `return { ..., "stream_url": ... }`。逐行 diff 对不上「逐字」二字。同篇其他围栏（`140:142` 的 `_ACTIVE_CALL_VAR`、`54:57` 的两个常量、`120:146`、`459:472`）同样是节选（`tools.py:140:142` 那一处倒是真的逐字一致），但只有这一处自称「逐字」。属表述问题，不影响引用的事实。

### 5.5 源码里有一句与文档冲突的旧注释

`src/agents/research/tools.py:518` 注释写「阶段 4：已实现 generate_review；另有 expand_by_citations（**至此十个工具全部注册**）」，但函数体里 `523`–`533` 一共 `registry.register(...)` 了 **11** 次（多出的是 `GET_HISTORY_SPEC`）。文档说「工具表里一共 11 个工具」（`L45`、`L481`）、「工作区工具 8 个 + 子 Agent 工具 3 个」是**对的**；但读者照文档翻进 `tools.py`，会看到注释里的「十个」，容易怀疑自己数错。建议顺手把源码注释改掉，或文档不提这一段。

---

## 六、行号围栏与命名抽查（独立做的，不依赖诊断）

围栏（`起始行:结束行:文件`）逐个验过，**全部对得上**，与诊断结论一致：

| 围栏 | 核验 |
| --- | --- |
| `196:266:src/services/session_runs.py` | `async def start_run` 在 196；函数体（最后一个 `}`）在 266 ✓ |
| `120:146:src/services/chat_runtime.py` | `resume_thread_id = ...` 在 120；`run_conversation_agent(...)` 收尾的 `)` 在 146 ✓ |
| `54:57:src/agents/research/agent.py` | `MAX_TOOL_ROUNDS = 20` 在 54、`TOOL_PARALLEL_LIMIT = 3` 在 57 ✓ |
| `140:142:src/agents/research/tools.py` | `_ACTIVE_CALL_VAR` 三行，140–142，逐字一致 ✓ |
| `459:472:src/agents/review/pipeline.py` | `add_edge(START, ...)` 在 459；`add_edge("finalize", END)` 在 472 ✓ |

正文里的单点行号引用也逐个验过：

| 文档引用 | 核验 |
| --- | --- |
| `session_runs.py:196`（`L275`） | ✓ `def start_run` 在 196 |
| `agent.py:54`（`L360`） | ✓ `MAX_TOOL_ROUNDS = 20` |
| `tools.py:140:142`（`L395`） | ✓ |
| `agent.py:1047`（`L411`） | ✓ `def note_streaming_tool_call(` 在 1047 |
| `workflow.py:488`（`L723`） | ✓ `def _emit_runtime_event(` 在 488 |
| `workflow.py:144`（`L725`） | ✓ `_STAGE_DISPLAY` 在 144 |
| `llm/base.py:196`（`L417`） | ✓ `async def yield_to_event_loop` 在 196，函数体就是 `await asyncio.sleep(0)` |
| `prompts.py`「约第 543 行」（`L743`） | ⚠ 守卫在 550、`raise` 在 551（见【维持 3】） |

常量、字段名、函数名抽查（除上面两处行号外，**全部与源码一致**）：

- `MAX_TOOL_ROUNDS = 20` / `TOOL_PARALLEL_LIMIT = 3`（`agent.py:54` / `57`）；`ROUND_LIMIT_INSTRUCTION`（`agent.py:102`）；`FINAL_ANSWER_MAX_REPAIR = 1`（`agent.py:116`）✓
- 综述确实是 `StateGraph(ReviewState)`（`pipeline.py:449`）+ `AsyncSqliteSaver.from_conn_string`（`pipeline.py:380`）+ 会话目录下的 `checkpoints.db`（`pipeline.py:368`）；`thread_id = f"{deps.turn_id}:{deps.event_key}"`（`pipeline.py:357`）✓
- `_run_graph_safely`（`pipeline.py:288`）确实把 `NodeCancelledError`（`pipeline.py:310`）转回 `asyncio.CancelledError()`（`312`），细节与文档 `L665`–`L669` 一致 ✓
- `_bind_node`（`pipeline.py:428`）的 docstring 与文档 `L647`–`L651` 几乎同义（「不能写 lambda，否则协程对象被当成状态更新，报 `Expected dict, got coroutine`」）✓
- `write_section` 内部是 `while True`（`writing.py:116`），先 `plan_or_write`、要工具就 `run_tool` 再回来、否则 `review_draft`，只有 `completed` 才 break（`116`–`123`）✓；`MAX_TOOL_CALLS = 5`（`writing.py:65`）、`MAX_REVISION_ROUNDS = 2`（`writing.py:68`）✓
- 写作可用的三个工具名对得上：`get_extraction`（`writing.py:400`）、`search_section`（`426`）、`get_chunk_by_embed`（`598`，函数体只有 docstring，命中时返回 `message: "get_chunk_by_embed 暂未实现"`，`637`）✓
- `source=abstract_fallback`：`DEEP_READ_SOURCE_ABSTRACT = "abstract_fallback"`（`src/models/deep_read.py:21`），`deep_read.py:236` 用它，`deep_read.py:480` 返回 `notice` ✓
- 片段版本：`CONVERTER_VERSION = 9`（`convert.py:46`）、`CHUNKER_VERSION = 10`（`chunkers.py:28`）✓；`_MAX_JUNK_CHARACTERS = 20`（`chunkers.py:50`）✓；37 片段 / 4 个孤立页码 / 约 11% 出自 `chunkers.py:46`–`47` 的注释原文 ✓
- `PageChunker` 的 `max_chunk_characters = 1200`（`chunkers.py:116`）、`max_atomic_characters = 4000`（`120`）；`PageChunker.pack`（`128`）✓；超长公式切开后仍是完整 `$$`（`_split_long_math` docstring「保证每一片自身都是一对完整的 $$」）；超长表格按行切并补表头（`_split_long_table`，`566`–`581`）✓
- `\[...\]` → `$$`、`\(...\)` → `$...$` 确实在 `tools/nougat_trial/render_pdf.py:92`–`101` ✓
- 插图清单「优先图块、其次正文开头那份、都没有才反查 `![](assets/...)`」与 `src/utils/fulltext/figures.py:100`–`133` 的 `collect_paper_figures` docstring 完全一致 ✓
- 「前 2/3 + 后 1/3」在 `paper_qa.py:613`–`622`（`front_size = QA_CONTEXT_MAX_CHARS * 2 // 3`）✓；`_repair` 在 `paper_qa.py:298` ✓；`QA_MAX_TOOL_ROUNDS = 4`（`74`）、6 片（`59`）、1200 字截断（`60`）、60 字预览（`66`）✓
- `WorkflowNodeReporter` 9 个方法（`workflow.py:318`–`420`：started / progress / completed / failed / reasoning_delta / reasoning_end / message / delta / artifact），文档 `L707` 一个不差 ✓
- `_STAGE_DISPLAY` 里 `("chat", "llm_round")` 确实带 `updates_parent=True`（`workflow.py:293`）✓；`_node_event_id` = `{turn_id}:{node_key}`（`555`）、`_step_event_id` = `{turn_id}:{node_key}:{event_key}`（`560`）✓
- `event_key` 形如 `{name}_r{round}_{index}`（`agent.py:1066`），所以文档 `L201` 的 `deep_read_paper_r1_0` 成立 ✓
- 终答自检的四道过滤与 `agent.py:140`（`_BRACKET_CITATION_PATTERN = r"\[([^\[\]]+)\](?!\()"`）、`144`（`_CITATION_PLACEHOLDER_WORDS = {"todo","note","fixme"}`）、`1024`–`1034`（长度 < 2 或纯数字、`_PAPER_ID_SHAPE_PATTERN`、占位词）逐条对得上 ✓；`build_citation_lookup` 在 `src/paper_retrieval/identity.py:171` ✓
- 系统提示词第 7 条确实是「`fulltext_available` 为 false 时必须明确告诉用户」（`prompts.py:375`）✓；`RESEARCH_AGENT_SYSTEM_PROMPT` 在 `prompts.py:365` ✓
- `workspace.search_history[-5:]`（`agent.py:983`）✓
- 11 个工具（`tools.py:523`–`533`）✓；模块级入口 `run_deep_read`（`deep_read.py:155`）、`run_paper_qa`（`paper_qa.py:116`）、`run_review`（`pipeline.py:248`）✓；`DeepReadDeps`（`deep_read.py:123`）、`PaperQaDeps`（`paper_qa.py:86`）、`ReviewDeps`（`pipeline.py:122`）✓
- `BaseAgent`（`src/agents/common/base.py:58`）/ `AgentSpec`（`21`）确实只剩 `ReadAgent`（`src/agents/reading/relevance.py:50`）一个子类，供 `evaluate_papers` 用 ✓；`ReviewRequest` / `AgentRunInput` 全仓已无命中 ✓
- 文档引用的文件路径全部存在（含 `doc/arch/README.md`、`02/03/04`、`doc/LLM-README.md`、`src/agents/common/skill_loader.py`、`src/utils/fulltext/cache.py`、`tools/nougat_trial/render_pdf.py`）✓；`skill_loader.py:27` 确实用 `Path(__file__).resolve().parent / "skills"` ✓
- 文档 `L379` 的 `context.check_cancelled()`（`tools.py:189`）、`L403` 的 `context.get_active_call()`（`tools.py:184`）、`L391` 的 `asyncio.Semaphore(TOOL_PARALLEL_LIMIT)`（`agent.py:433`）✓
- 附带确认诊断「五、对新人上手不友好」第 4 条：下载失败时确实直接走摘要降级，`if chunks and markdown_text is not None:` 这道门把 map/reduce 整个跳过（`deep_read.py:318`–`331` 判失败、`331` 之后才进 map）——文档把第 7 步排在 map/reduce 之后的读法确实会误导。

---

## 最后一句

这篇文档与源码的符合程度很高：诊断列出的 5 条源码不符里没有一条能推翻（3 条成立、1 条是文档内部编号问题被放错了节、1 条属「步骤 / 节点」粒度混用），我另外把全篇的行号围栏、常量名、函数名、字段名逐项抽查一遍，除 `L743` 的行号（543 → 550）与 `L119` 的 `ContextVar` 说法外，没有查出新的与源码冲突的事实。
