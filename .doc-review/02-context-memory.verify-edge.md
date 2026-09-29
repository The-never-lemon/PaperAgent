# 对抗式复核：02-context-memory（视角：找反例和边界）

复核对象：`.doc-review/02-context-memory.diag.md` 的「三、与源码不符的地方」
被诊断文档：`doc/arch/02-context-memory.md`
复核立场：**先默认每条指控是错的**，只在我亲自于源码里看到证据、且无法用「特定分支 / 特定配置 / 特定调用方」解释掉时，才承认它成立。

---

## 一、维持原判（我无法推翻）

### 1. `L317` 「表里没有目录名时，它会去磁盘扫一遍……」

**诊断原文**：这句话紧跟在 `resolve_paper_cache_dir` 的说明后面，读者会以为 `resolve_paper_cache_dir` 会扫盘。

**我的反例尝试**：我试着把它解释成「讲的是 `resolve_paper_cache_dir` 在某个分支下的行为」。

**结论：推翻失败，维持原判。** 证据：

- `src/services/paper_memory.py:191` 的 `resolve_paper_cache_dir` 函数体（191-218 行）只有三段：先 `lookup_memory(paper)` 查目录表里记下的 `cache_dir`（`candidate.is_dir()` 命中就返回）；命中不到且 `entry.cache_present or entry.has_chunks` 时调 `_invalidate_missing_cache`；其余全部走「按当前编号拼路径返回」（`root / safe_cache_name(paper)` / `paper_cache_dir(root, doc)`）。**全程没有 `iterdir`、没有遍历磁盘**，任何一个分支都不会扫盘。
- 真正扫盘的是 `_locate_cache_dir_on_disk`（`src/services/paper_memory.py:760`，函数体里有 `cache_root.iterdir()`，并对每个目录比别名 / 比 `papers_match`），写回目录表的是 `_remember_located_cache`（`src/services/paper_memory.py:800`）。
- 这条扫盘链路的唯一入口是 `record_fulltext_deep_read`（`src/services/paper_memory.py:220`，第 226 行调 `_locate_cache_dir_on_disk`），也就是「精读写回」那条路径，跟 `resolve_paper_cache_dir` 无关。

补充边界：`resolve_paper_cache_dir` 确实会**读**目录表，所以 L313「优先相信目录表里记下的文件夹名」这句本身是对的；错的是紧接其后的 L317 把「扫盘 + 写回」这动作挂到了它名下。诊断给的改写建议（把动作主语换成 `_locate_cache_dir_on_disk` / `_remember_located_cache`）准确，可以直接用。

---

### 2. `L308` 「`paper_key` 是主编号，写法带 `doi:` 或 `arxiv:` 前缀」

**我的反例尝试**：会不会 `paper_key` 在别的调用路径下只产出这两种前缀？

**结论：推翻失败，维持原判。** 证据：

- `src/paper_retrieval/identity.py:36` 的 `paper_key` 是三段兜底：`_doi_key(paper)` → `_arxiv_key_from_ids(paper)` → `return _title_key(paper)`（`src/paper_retrieval/identity.py:50`）。
- `_title_key`（`src/paper_retrieval/identity.py:266`）末尾 `return f"title:{title}"`（`src/paper_retrieval/identity.py:274`）。

所以 `paper_key` 的取值有三种 `doi:` / `arxiv:` / `title:` 前缀。文档只写了两种，属于漏写。**注意**：`_doi_key`（`identity.py:237`）会把 arXiv 自家 DOI 改记成 `arxiv:`，所以「doi: 或 arxiv:」在「有 DOI 时」是自洽的——但「两者都没有」这一类论文文档完全没提，改写时必须补上 `title:`。

---

### 3. `L390` 「这类失败报出来的错永远是『没有返回可解析的 JSON』」

**我的反例尝试**：会不会 `_describe_unparsable` 只是日志文案，`reason` 实际返回的还是笼统一句？

**结论：推翻失败，维持原判，而且这条是四条里最硬的一处。** 证据：

- `_parse_response`（`src/agents/review/analyse.py:252`）在解析失败时把 `reason=_describe_unparsable(...)`（`src/agents/review/analyse.py:271`）写进返回值；`_describe_unparsable`（`src/agents/review/analyse.py:287`）是显式三分支：
  - `finish_reason in {"max_tokens", "length"}` → 「模型在达到输出上限…时停止，没有写出完整的 JSON」（301 行）
  - 输出为空且 `finish_reason == "end_turn"` → 「模型只输出了思考过程，没有输出任何正文内容」（303 行）
  - 兜底才是 「模型没有返回可解析的 JSON」（304 行）
- 函数自己的注释就写着「以前不管是哪种原因，一律只报『模型没有返回可解析的 JSON』」（289-291 行）——**「以前」二字直接证伪了文档的「永远」**。
- 文档 L397 自己也写了「失败原因也细分出『达到输出上限』和『只输出了思考没写正文』两种，不再笼统报一句『不可解析』」，L390 与 L397 在同一节内互相打架。

补充边界：`src/agents/review/analyse.py:168` 那行日志确实写死了「分析模型没有返回可解析的 JSON，准备重试」，L141 的 `last_reason` 初值也是这一句——所以文档想说的「日志里有这么一句」在**重试日志**这一层是成立的，但那是日志，不是报给上层的 `reason`。文档用的是「这类失败报出来的错永远是……」，指向的是 `reason`，站不住。

---

### 4. `L251` 「最近 5 条检索历史，形如『- 「主题」（HH:MM）→ 命中 N / 新增 M』」

**我的反例尝试**：会不会 `concept_groups_summary` 在落库时就被填成了 topic，所以显示出来就是主题？

**结论：推翻失败，维持原判（但这条要拆成两半看）。** 证据：

- 渲染处 `src/agents/research/agent.py:983-989`：`f"- 「{entry.concept_groups_summary or entry.topic}」"`（987 行），时间取 `entry.timestamp[11:16]`（985 行）。**优先显示的是 `concept_groups_summary`，不是 `topic`**——所以文档把示例标签写成「主题」不准确。
- 进一步查它的来源：`src/agents/research/tools.py:733-737` 落库时 `summary = cleaned_title or ", ".join(g[0] for g in (cleaned_groups or []) if g)`，然后 `concept_groups_summary=summary[:200]`。也就是说这一格在**有标题检索时存的是标题**、否则存的是各概念组的代表词——**两种都不是话题原文**。诊断只说「优先概念组摘要」，其实还漏了「或检索标题」。我在这条上把诊断的结论往前推一点，结论方向一致。
- 时间戳为 UTC：`SearchHistoryEntry.timestamp` 的 `default_factory=utc_now`（`src/models/workspace.py:201`，字段说明见 193 行「检索发生时间（UTC ISO 格式）」），`utc_now` 返回 `datetime.now(timezone.utc).isoformat()`（`src/models/sessions.py:22-25`）。

**但要说清边界**：文档只写了「（HH:MM）」，并没有声称这是本地时间。所以「没写是 UTC」属于**信息缺失**，不是「与源码不符」；这一半是改进建议。真正与源码冲突的只有「主题」这个标签。「最近 5 条」这个数量是对的（`search_history[-5:]`）。

---

## 二、推翻（诊断不成立）

**没有。** 「三、与源码不符的地方」的 4 条，我逐条回源码找反例，4 条全部站得住（第 4 条只有「UTC」那一半属于信息缺失而非事实错误）。诊断在这一节的判断是可靠的。

---

## 三、新发现（诊断漏掉的，以及诊断自己写错的）

### 新发现 1：`L218`「直接跳过阶段 B，只用阶段 A 兜底」与代码实际执行路径不符

文档 L218：「`build_llm_messages` 不传压缩模型时，直接跳过阶段 B，只用阶段 A 兜底。宁可历史超一点预算，也不让组装过程失败。」

源码实际行为：`compress_llm=None` 时，**阶段 B 的「整轮移除」照常发生**，只是摘要部分退化成罗列用户原话：

- `_shrink_history_within_budget` 的阶段 B 块（`src/agents/research/agent.py:829-854`）判断条件只有「超预算 + 剩余轮数大于 `KEEP_RECENT_TURNS_LITE`」，**跟 `compress_llm` 无关**；它照常 `removed_turns.append(working[:next_cut])`、`working = working[next_cut:]`（830-847 行），把最老的整轮从上下文里切掉。
- 切完才调 `summary = await _compress_completed_turns(removed_turns, compress_llm)`（848 行）；`_compress_completed_turns` 在 `compress_llm is None` 时**直接返回空字符串**（`src/agents/research/agent.py:880-881`）。
- 空摘要走 `summary_lines.extend(_fallback_user_request_lines(removed_turns))`（854 行）——罗列被移除轮次里用户的原始诉求，各截 100 字（`src/agents/research/agent.py:859-870`）。

所以准确说法是：「不传压缩模型时，阶段 B **照样整轮移除最老的轮次**，只是把 LLM 摘要换成罗列用户原话的确定性兜底」。文档的「跳过阶段 B / 只用阶段 A 兜底」把「不做模型调用」误写成了「不做移除」。

**注意这条的性质**：`build_llm_messages` 自己的 docstring（`src/agents/research/agent.py:595-596`）写的就是「不传则跳过阶段 B，只用阶段 A 结果兜底——宁可历史超一点预算」。**文档是忠实照抄了源码注释，而源码注释本身和源码行为不一致。** 所以这处不该只改文档，改文案时最好两处一起对齐，否则下次还会被抄回来。

### 新发现 2：诊断自身的一处计数错误（不是文档的错）

诊断文件末尾写「三张表格、四张 mermaid 图的流程也和代码一致」。实测 `doc/arch/02-context-memory.md`：

- mermaid 代码块只有 **2 个**（`doc/arch/02-context-memory.md:19` 与 `:289`）；诊断自己在第一节也只点名了这两张图（L19-33、L289-298），前后自相矛盾。
- 表格只有 **1 张**（`doc/arch/02-context-memory.md:39-43` 的三层记忆表）。

改写时按「1 张表 + 2 张图」核对即可，不要按 3+4 去找。

### 新发现 3：诊断的 `文件:行号` 有两处指偏（结论没错，引用要修正）

- 诊断 4 号条目引用「`entry.concept_groups_summary or entry.topic`（`src/agents/research/agent.py:981`）」。实际 981 行是 `if workspace.search_history:`，拼接那句在 **987 行**（985 行是时间戳）。
- 诊断 3 号条目引用「`_parse_response`（`src/agents/review/analyse.py:271`）」。`_parse_response` 的 `def` 在 **252 行**，271 行是函数体内 `reason=_describe_unparsable(...)` 那一行。结论没错，改成 252 更准。

### 新发现 4（低置信度，供参考）：`L380`「两个 1200 只是数值巧合」

文档 L380：「这里的 1200 和片段装箱的 1200 只是数值巧合，是两条独立的线。」

代码层面确实是两个互不引用的常量（`QA_READ_CHUNK_MAX_CHARS = 1200`，`src/agents/reading/paper_qa.py:60`；`PageChunker.max_chunk_characters = 1200`，`src/utils/fulltext/chunkers.py:116`），所以「两条独立的线」成立。但源码注释的口气不是「巧合」，而是**有意对齐**：`src/agents/reading/paper_qa.py:58-59` 写的是「片段上限 ≈ 精读切片大小（1200 字），一次最多 6 片」。文档说「巧合」会把作者的设计意图讲反。这条属于措辞与注释意图的出入，不是硬事实错误，改不改看后续取舍。

---

## 四、独立抽查（三节之外，逐条回源码核对）

诊断「已核对无误」的那一段列了一长串常量，我全部独立核对过，**全部对得上**，另外补了几处它没列的：

| 文档说法 | 源码证据 | 结论 |
| --- | --- | --- |
| `HISTORY_BUDGET_RATIO` = 0.8，定义在 `context_budget.py:28` | `src/agents/common/context_budget.py:28` | ✅ |
| `INITIAL_CHARS_PER_TOKEN` = 4.5，`context_budget.py:24` | `src/agents/common/context_budget.py:24` | ✅ |
| 代码围栏 `90:98:context_budget.py` 逐字引用 | `context_budget.py:90` 是 `def budget_tokens`，`:98` 是 `return int(window * HISTORY_BUDGET_RATIO)`，正文逐字一致 | ✅ |
| `agent.py` 里另有一份同名 `HISTORY_BUDGET_RATIO`，全仓没有引用它 | 定义在 `src/agents/research/agent.py:65`；全仓 grep 只有 60 行注释和 65 行定义，**没有任何读取点** | ✅ |
| 窗口没配按 1M，`config/system.yaml` 写的是 `null`，兜底在 `src/llm/config.py:45` | `config/system.yaml:9 context_window_tokens: null`；`src/llm/config.py:45 context_window_tokens: int | None = 1048576` | ✅ |
| 字符率护栏 1.0~100.0 | `src/agents/research/agent.py:1182 if 1.0 <= measured <= 100.0:` | ✅ |
| 自适应校准 75%，预算压到「上次占用 × 0.6」 | `agent.py:92 _ADAPTIVE_CALIBRATION_RATIO = 0.75`；`agent.py:206-209` 判定与 `int(_LAST_OBSERVED_PROMPT_TOKENS * 0.6)` | ✅ |
| 两个实测值模块级全局、无锁、不落库 | `agent.py:83`、`agent.py:90`，注释明写「无锁、无落库」 | ✅ |
| 「每条 8 字符的框架开销」 | `context_budget.py:76 total_chars += len(role) + 8` | ✅ |
| `KEEP_RECENT_TURNS_LITE` = 6 | `agent.py:68` | ✅ |
| 阶段 A 三件事（占位符 / 清思考 / 终答留前 400 字） | `agent.py:800-824`，含 `TOOL_RESULT_PLACEHOLDER.format`、`message["thinking_blocks"] = []` + `pop("reasoning_content")`、`content[:400] + "……[旧回复已截断]"` | ✅ |
| 占位符文案、补白文案逐字 | `agent.py:75-78`、`agent.py:745` | ✅ |
| 阶段 B 温度 0、`max_tokens` 800、提示词四类信息 | `agent.py:934-935`（`temperature=0, max_tokens=800`），prompt 见 `agent.py:920-924`（含「诉求 / 结论 / 动过哪些论文 / 尚未完成」） | ✅ |
| `COMPRESS_INPUT_MAX_CHARS` 16000、`COMPRESSED_SUMMARY_MAX_CHARS` 400 | `agent.py:71-72` | ✅ |
| 缓存 key 为指纹取前 16 位 | `agent.py:901 hashlib.sha1(...).hexdigest()[:16]`（`_COMPRESSION_CACHE` 在 `agent.py:574`） | ✅ |
| 「## 此前对话摘要」+「序号. 行」+ 不再截总量 | `agent.py:689`（`f"{index}. {line}"`，`enumerate(..., start=1)`）、`:694`（小节标题） | ✅ |
| `get_history` 3000 上限、2984 截短、turns 1~10 默认 3 | `src/agents/research/tools.py:1747`、`:1829 shrink_room = GET_HISTORY_MAX_CHARS - 16`、`:1770-1773` | ✅ |
| `session_message` 只增不改 | `src/repositories/sessions/sqlite.py` 只有 `INSERT`（240）与 `SELECT`（269），全仓无 `UPDATE/DELETE session_message` | ✅ |
| 用户消息在 `session_runs.py:215` 落库 | `src/services/session_runs.py:215 self.repo.append_message(session_key, "user", ...)` | ✅ |
| `build_llm_messages` / `_repair_tool_call_pairing` 行号 | `src/agents/research/agent.py:577` / `:702` | ✅ |
| `WORKSPACE_SCHEMA_VERSION` = 4、`papers.json`、默认根 `data/sessions` | `src/models/workspace.py:24`、`:27`、`:31 DEFAULT_SESSIONS_ROOT = Path("data") / "sessions"` | ✅ |
| 检索历史三处截断到 20 条 | `workspace.py:354 to_dict`、`:416 record_search_history`、`:398 _apply_dict` 三处都有 `[-20:]` | ✅ |
| `remove_papers` 在 `models/workspace.py:489`，软删除 + `removed_at` 落盘 | `src/models/workspace.py:489`、`:508-509` | ✅ |
| 工作区落盘 flush + fsync + `os.replace`、同目录临时文件 | `workspace.py:332-336` | ✅ |
| `workspace_lock` 在 `tools.py:177`，加锁点四处 | `tools.py:177` 定义；使用点 `:669`（检索）、`:1005`（引文扩展）、`:1383`（评价）、`:1467`（删除），共 4 处 | ✅ |
| 召回也在同一把锁里 | `_recall_papers_memory` 调用点 `tools.py:674`、`:1010`，两处都在 `async with context.workspace_lock` 块内 | ✅ |
| `CHUNKER_VERSION` = 10、`chunk.json`、`chunks_are_usable` 判据 4000 | `src/utils/fulltext/chunkers.py:28`；`paper_memory.py:483`、`:489 PageChunker.max_atomic_characters` | ✅ |
| `max_chunk_characters` 1200 / `max_atomic_characters` 4000、大表补表头、新一级标题另起段 | `chunkers.py:116`、`:120`、`:193`+`:202`（表头续接）、`:186`（新标题另起段） | ✅ |
| `report_summary` 从 300 放宽到 1500、工具结果上限 20000 | `src/agents/reading/deep_read.py:92-95`（注释「300 → 1500」）、`src/agents/research/tools.py:56` | ✅ |
| 追问：目录 60 条 ×60 字、小循环 4 轮、取 6 片段按 1200 截、降级 30000 前 2/3 后 1/3 | `paper_qa.py:59-66`、`:74`、`:234/244`、`:535/539`、`:606/619-623` | ✅ |
| 综述分析：温度 0.2 + `reasoning_effort="medium"`、`min(configured * 2**(n-1), 32768)`、最多 3 次 | `analyse.py:79-80`、`:200-210`、`:28/:32` | ✅ |
| 思考块双路（内存 + 落库） | `agent.py:394-406`（`attach_reasoning` + `append_message(thinking_blocks=..., reasoning_content=...)`）；`src/llm/base.py:117-133` | ✅ |
| 压缩调用发的是全新 system+user 两条 | `agent.py:928-935` | ✅ |
| 三层记忆表：`data/paper_cache/`、论文目录表与会话库同库 | `src/llm/config.py:50 paper_cache_dir: str = "data/paper_cache"`；`config/system.yaml:19`；`sqlite.py` 里 `paper` / `paper_alias` / `paper_session` 与会话表同库 | ✅ |
| `configure_paper_catalog` 调用点在 `SessionRepository` 构造函数 | `src/repositories/sessions/sqlite.py:983-985` | ✅ |

抽查合计 **30+ 处**（远超要求的 8 处），未发现新的硬事实错误。

---

## 五、一句话结论

**这篇文档与源码的符合程度很高**：诊断「与源码不符」的 4 条我一条也推翻不了（其中 L390「永远是那句笼统报错」、L308「paper_key 只有两种前缀」是确凿的事实错误，L317 是函数归属错误，L251 只有「主题」这个标签不准），另有 1 处诊断漏掉的路径描述不符（L218「跳过阶段 B」，但根子在源码注释自身）、3 处诊断自身的引用/计数疏漏，除这 5 处之外，全篇常量、行号、文案引用、失败降级路径与源码逐条对得上。
