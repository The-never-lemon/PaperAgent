# doc/arch/01-agent-orchestration.md 复核（视角：找反例和边界）

复核对象：`.doc-review/01-agent-orchestration.diag.md`「三、与源码不符的地方」全部 5 条
复核方式：默认诊断是错的，回源码找反例；只有看到无法推翻的证据才承认
口径：**维持 4 条**（其中 3 条要下调严重度）、**部分推翻 1 条**、**新发现 4 条**
重点检查：默认值 / 数量 / 顺序 / 是否落库 / 失败降级路径

---

## 一、维持原判

### 【维持 1】`L119` 写作进度回调「用 `ContextVar` 传」

**但诊断漏掉了关键边界：这句话不是编造的，是一条“过期的修法”。**

现状证据（与诊断一致）：

- `src/agents/review/pipeline.py:673`：`progress_callback=on_section_progress,` —— 回调是当关键字参数传进 `write_section` 的。
- `src/agents/review/writing.py:81`：形参声明 `progress_callback: Any | None = None,`；`117`、`121` 一路透传给 `plan_or_write` / `review_draft`。
- 全仓 `ContextVar` 只有 `src/agents/research/tools.py:140`（`_ACTIVE_CALL_VAR`）与 `src/utils/logging_utils.py:15/16`；`grep -rn contextvar src/agents/review/` 零命中。

我补的反例调查（这是诊断没做的）：

- `git log --oneline --all -S "ContextVar"` 命中 3 个提交。其中 `5a1d98b`（综述流水线改成 LangGraph 图）**确实新增了** `_SECTION_PROGRESS_CALLBACK: ContextVar("section_progress_callback")`，并在同一次改动的说明里写了「Type is not msgpack serializable: function。改成用 ContextVar 传。」——这正是文档 `L119` 那句话的出处。
- `c175b21`（2026-09-22「综述收成一条流水线，小节写作不再嵌第二张图」）**又把这段删掉了**：diff 里 `-from contextvars import ContextVar`、`-_SECTION_PROGRESS_CALLBACK: ContextVar[...] = ...`。
- 当前 HEAD 下 `grep -rn "msgpack" src/` **零命中**，那句说明也随代码一起删了。

结论：诊断成立。但准确的说法是“文档抄的是一句已经被删掉的旧说明，修法早已改回普通参数”，而不是“文档把机制记混了”。诊断给的改法（改成「把回调当普通参数传给 `write_section`」）与现状一致。

### 【维持 2】`L405` 用量「累加」到工具卡

成立，并且我把诊断没查的前端也查了——前端同样是覆盖。

- `src/runtime/workflow.py:522`：注释「只有模型回调明确提供用量时才覆盖卡片数字，普通进度更新不会把已有用量清零」；`523`–`526` 是两段覆盖赋值 `payload["input_tokens"] = ...`。
- 我补的证据：`front/src/lib/session-stream-aggregator.ts:477`–`480`，接同一 id 事件时写的是 `item.inputTokens = Math.max(0, event.input_tokens)`，**赋值而不是累加**。整条链路上没有任何一处把同名卡片的用量相加。
- 诊断提到撞简历红线，我核对了 `memory/project_resume_redlines.md`：仍成立的红线里确有「token 用量是覆盖而非累加」。诊断这条成立。

需要标注的边界（不改变结论）：真正的“累加”发生在**上报之前**——`src/agents/review/pipeline.py:660` 的 `usage.collect(section_usage)` 先把一个节点里多次模型调用的用量攒成总数再上报一次。所以「累加」描述的是调用方，不是卡片；文档 `L405` 把「归到哪张卡」和「累加」写在同一句里，与同篇 `L718`–`L719` 的「覆盖」自相矛盾。诊断成立。

### 【维持 3】`L743` 行号「约第 543 行」

成立，但**严重度要压到最低**（诊断自己也降了一档，我认同）。

- `src/agents/common/prompts.py:543`：`_DEEP_READ_FIDELITY = skill_section("paper-deep-reading", "逐段阅读取舍")` —— 取片段那一行。
- `:550`：`if "JSON" in _DEEP_READ_FIDELITY:`；`:551`：`raise RuntimeError(`。

边界判定：文档写的是「**约**第 543 行」，543 到 550 只差 7 行，且落在同一个 if 块里；同一句对机制的描述（导入期守一道、抛 `RuntimeError`）与源码完全一致。这是行号漂移，不是事实错误。诊断的改法（改成 550）可以对，但按最低优先级处理。

### 【维持 4】`L53`「你现在站在第 1 节的位置」

事实成立，但**归类要改**：这条不是“与源码不符”，是文档内部编号不一致——源码里没有任何东西决定这句话。

- `doc/arch/01-agent-orchestration.md:15`：`## 0. 先看全局`；下一节是未编号的 `## 设计理念`；`## 1. 请求怎么接到主 Agent` 在 `L271`。
- 读者按编号找「第 1 节」会落到主 Agent 那一节，确实会对不上号。诊断建议改成「第 0 节」是合理修法。

---

## 二、部分推翻（1 条）

### 【部分推翻】`L101` / `L160`「综述有十几个步骤」

条目原文：

> `L101` / `L160` 文档说：「综述有十几个步骤。」；**源码实际：`_build_review_graph` 一共只挂 8 个节点**。……（低风险，属于粒度不一致，不与事实冲突。）建议改成：「综述有 8 个节点……」

**核对无误的部分**：`src/agents/review/pipeline.py:450`–`457` 确实是 8 个 `add_node`：prepare / analyse_subtopic / analyse_overall / build_outline / write_section / write_abstract / compile_document / finalize。`doc/arch/README.md:236` 也写「外层 8 个节点做成图」，文档自己的 `L618`–`L630` 画的也是这 8 个。

**推翻的理由**：诊断把「步骤」直接当成「节点」，这一步把适用范围放大了。

- 文档用的词是「**步骤**」不是「节点」。这两个词在本篇里是分开用的：`§6` 用「节点」指图上的方框（`L616`「节点与边的走向」），`L101` 用的是「步骤」。
- 按“执行了几步”数，一份 12 节的综述是**十几个**而不是 8 个：4 个前置节点 + `write_section` 自环跑 12 次 + 3 个收尾节点 ≈ 19 步。`_route_next_section`（`pipeline.py:478`–`483`）就是让 `write_section` 反复回到自己身上，节数由大纲决定。所以「十几个步骤」在“多节综述”这个常规分支下是成立的，正是任务里说的「文档在讲某个特定分支，诊断把范围放大了」。
- 因此把它判成「与源码不符」过重；诊断建议的替换文本「综述有 8 个节点」虽然更精确，但会把“这活有多繁琐”这个论点削掉。更好的改法是消歧：「综述的外层图有 8 个节点。一份 12 节的综述，节点要跑十几步。」

保留的部分：文档没有点明单位，读者容易和 `§6` 的 8 个方框对不上，作为措辞不精确仍然值得改。

---

## 三、新发现（诊断漏掉的与源码不符之处）

### 新 1（数量）`L574`「这和装箱上限不是同一个数」——两个数其实是同一个

文档原文（`doc/arch/01-agent-orchestration.md:574`）：「追问工具 `read_sections` 取回时另有每段 1200 字的截断。这和装箱上限不是同一个数。」

- 装箱上限：`src/utils/fulltext/chunkers.py:116` `max_chunk_characters = 1200`。
- `read_sections` 截断：`src/agents/reading/paper_qa.py:60` `QA_READ_CHUNK_MAX_CHARS = 1200`，用在 `:539`。

两个数**都是 1200**，文档却说「不是同一个数」。上一句 `L567` 刚说过「装到 1200 字就换下一段」，所以「装箱上限」在这里就是 1200，不是 4000。文档想表达的应该是“它们是两个各自独立的常量”，但字面写成了“数值不同”。
建议改成：「追问工具 `read_sections` 取回时另有每段 1200 字的截断。这是另一个独立的常量，只是数值恰好一样。」

### 新 2（默认路径）`L727`「中间带下划线」——源码的兜底恰恰是把下划线换掉

文档原文（`L727`）：「新增工具时要在这里补一条。否则前端只能看到未映射的原始阶段名，中间带下划线。」

- `src/runtime/workflow.py:565`–`568`：`_STAGE_DISPLAY.get((self.node_key, stage), RuntimeStageDisplay(stage, _humanize_stage(stage)))`。
- `src/runtime/workflow.py:592`–`595`：`_humanize_stage` 的 docstring 是「避免界面直接显示下划线」，函数体是 `stage.replace("_", " ").strip()`。

也就是说，没配映射时前端拿到的标题是**把下划线换成空格**的（`deep_read_paper` → `deep read paper`），原始 stage 串只留在 metadata 里。文档「中间带下划线」与兜底函数的设计意图正好相反。建议改成：「否则前端只能看到未映射的原始阶段名（下划线会被换成空格，也没有中文标题）。」

### 新 3（诊断自身的三处行号不准）

诊断「行号围栏核对结果」那一节声称「全部对得上」，但它在正文里顺手引的三处行号对不上（这三处文档本身没写行号，所以**不影响文档**，但会误导改写的人）：

| 诊断的写法 | 实际位置 | 差 |
| --- | --- | --- |
| 「主 Agent 确实是手写 `for round_no in range(1, MAX_TOOL_ROUNDS + 1)`（`agent.py:246`）」 | `src/agents/research/agent.py:241` | 偏 5 行（246 是循环体内的注释） |
| 「`QA_READ_MAX_CHUNKS = 6`、`QA_READ_CHUNK_MAX_CHARS = 1200`（`paper_qa.py:57`、`58`）」 | `src/agents/reading/paper_qa.py:59`、`60` | 各偏 2 行 |
| 「综述确实是 `StateGraph` + `AsyncSqliteSaver`（`pipeline.py:446`）」 | `StateGraph(ReviewState)` 在 `src/agents/review/pipeline.py:449`；446 是 `def _build_review_graph` | 偏 3 行 |

诊断自己引的 `pipeline.py:380`、`workflow.py:555`/`560`、`workflow.py:293`、`prompts.py:375`、`agent.py:54`/`57`/`102`/`116`、`tools.py:140`–`142`、`deep_read.py:72`/`84`、`figures.py:32`/`34`、`chunkers.py:116`/`120`/`50`/`28`、`convert.py:46`、`writing.py:65`/`68` 我逐个核过，**这些确实全对**。

### 新 4（旁注）源注释陈旧，文档反而是对的

`src/agents/research/tools.py:518` 的注释写「至此十个工具全部注册」，但 `registry.register(...)` 实到 `:533` 一共 **11** 条（`get_history` 是后加的）。文档 `L45`、`L481` 写的「11 个工具」是对的，不用改；陈旧的是源码注释。

---

## 四、独立抽查（诊断说“正确”的部分，我另跑了 16 处）

| # | 文档说法 | 源码 | 结果 |
| --- | --- | --- | --- |
| 1 | 11 个工具，8 个工作区 + 3 个子 Agent（`L45`/`L471`/`L481`） | `tools.py:523`–`533` 恰 11 条 register | ✓ |
| 2 | `MAX_TOOL_ROUNDS = 20` / `TOOL_PARALLEL_LIMIT = 3`（`L360`） | `agent.py:54` / `agent.py:57` | ✓ |
| 3 | `asyncio.Semaphore(3)` 限并发 | `agent.py:432` `Semaphore(TOOL_PARALLEL_LIMIT)` | ✓ |
| 4 | 每轮固定顺序：查取消→流式→无 tool_calls 走终答自检→有则落库→并发→按原序回填（`L349`–`L357`） | `agent.py:242`、`270`、`302`、`420`–`500`、`502` 起 | ✓ 顺序一致 |
| 5 | 轮次用尽注入 `ROUND_LIMIT_INSTRUCTION` 再调一次、不带工具（`L371`） | `agent.py:534` 追加该指令，终答路径不带 tools | ✓ |
| 6 | 四道引用过滤：行内链接 / 编号长相 / 占位词 / 短或纯数字（`L427`–`L435`） | `agent.py:1013`–`1037`（`_PAPER_ID_SHAPE_PATTERN`、`_CITATION_PLACEHOLDER_WORDS`、`len<2 or isdigit`） | ✓ 四条都在 |
| 7 | 对不上时最多修一次，仍对不上就在终答追加「未能……未经核实」（`L439`–`L443`） | `agent.py:308` `while unknown and repair_count < FINAL_ANSWER_MAX_REPAIR`；`356`/`552` 追加提示语 | ✓ |
| 8 | 检索历史取 `search_history[-5:]`（`L462`） | `agent.py:983` | ✓ |
| 9 | 降级打 `source=abstract_fallback`、`fulltext_available=false`、带 `notice`；标题「《…》无法下载全文，报告基于摘要生成」（`L227`–`L231`） | `deep_read.py:440`–`450`、`479`–`480`、`444` 标题串逐字一致 | ✓ |
| 10 | 摘要降级不进长期记忆，只有全文成功才写（`L522`） | `deep_read.py:435`–`441` `if source == DEEP_READ_SOURCE_FULLTEXT: record_fulltext_deep_read(...)` | ✓ |
| 11 | 下载/转换失败直接跳降级，不进 map、reduce（诊断 §五第 4 条） | map/reduce 在 `deep_read.py:354`/`361` 的 `if chunks and markdown_text is not None:` 里；降级在 else 分支 `:380`–`387` | ✓ 该批评成立 |
| 12 | 硬失败三种：map 全失败 / reduce 解析失败 / 降级解析失败（`L524`–`L528`） | `deep_read.py:352`、`387`（「精读报告解析失败，无法生成报告」） | ✓ |
| 13 | `CONVERTER_VERSION = 9`、`CHUNKER_VERSION = 10`（`L584`） | `convert.py:46`、`chunkers.py:28` | ✓ |
| 14 | 37 个片段 / 4 个孤立页码 / 约 11%（`L267`） | `chunkers.py:46`–`47` 注释原文逐字一致 | ✓ |
| 15 | 技能文档「目前有两份」+ 加载器用 `__file__` 定位（`L733`/`L741`） | `src/agents/common/skills/` 下只有 `literature-review.md`、`paper-deep-reading.md`；`skill_loader.py` `Path(__file__).resolve().parent / "skills"` | ✓ |
| 16 | 公式定界改写：`\[…\]`→`$$`、`\(…\)`→`$…$`（`L544`） | `tools/nougat_trial/render_pdf.py:92`–`96`（在写文件之前做） | ✓（不在 `convert.py`，文档也没说在） |

另外核对无误的默认值与边界：`research_agent` 档位未配置时回退 `default_agent`（`llm/config.py:170`–`171`，与 `L316` 一致）；`FINAL_ANSWER_MAX_REPAIR = 1`（`agent.py:116`）；`QA_MAX_TOOL_ROUNDS = 4` / `QA_READ_MAX_CHUNKS = 6` / `QA_TOC_PREVIEW_CHARS = 60`（`paper_qa.py:74`/`59`/`66`）；`MAP_NOTE_MAX_CHARS = 500`（`deep_read.py:84`，对应 `L209`「每段只写 500 字笔记」）；`get_chunk_by_embed` 空壳返回「暂未实现」（`writing.py:598`/`637`）；`_review_thread_id` = `f"{deps.turn_id}:{deps.event_key}"`（`pipeline.py:357`，对应 `L657`）；`snapshot.values` 空在先、`snapshot.next` 空在后（`pipeline.py:415`/`417`，与 `L661` 描述一致）；`NodeCancelledError` 转回 `CancelledError`（`pipeline.py:310`–`312`，与 `L665`–`L669` 一致）；`_bind_node` 不能用 lambda（`pipeline.py:434`–`440`）；`WorkflowNodeReporter` 9 个方法与 `L707` 所列一字不差（`workflow.py:318`–`420`）。

---

## 最后一句

**没有推翻任何一条“事实错误”：5 条里有 4 条经得起源码核对（其中 3 条要下调严重度），第 5 条（「十几个步骤」）被我降级为口径问题；另外我新查出 2 处诊断漏掉的源码不符（`L574` 的「1200 vs 1200」、`L727` 的「下划线」）。这篇文档与源码的符合程度很高——小节级事实、常量数值、降级与持久化路径基本都对得上，剩下的偏差集中在行号漂移、数值措辞和一条抄自旧注释的过期说明上，没有发现方向性错误。**
