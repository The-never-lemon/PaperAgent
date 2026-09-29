# doc/arch/02-context-memory.md 源码逐行核对（对抗式复核）

**复核对象**：`.doc-review/02-context-memory.diag.md` 第三节「与源码不符的地方」（4 条 + 1 段「已核对无误」的收尾声明）

**对照源码**：`src/agents/research/agent.py`、`src/agents/common/context_budget.py`、`src/services/paper_memory.py`、`src/models/sessions.py`、`src/repositories/sessions/sqlite.py`；为查证指控还读了 `src/paper_retrieval/identity.py`、`src/agents/review/analyse.py`、`src/agents/reading/deep_read.py`、`src/agents/reading/paper_qa.py`、`src/agents/review/pipeline.py`、`src/models/workspace.py`、`src/agents/research/tools.py`、`src/llm/base.py`、`src/repositories/sessions/base.py`。

**方法**：先默认每条指控都是错的，回源码逐句对照；无法推翻才承认。另外独立抽查了文档里 9 处带行号的引用、反引号里出现的每一个标识符（用脚本抽出后逐个 grep），以及图/表数量。

## 结论一句话

**4 条指控的实质全部成立**，但其中 1 条引用的行号是错的（`agent.py:981` 应为 `:987`）；第三节末尾那句「三张表格、四张 mermaid 图」与本篇文档对不上（本篇只有 **1 张表格、2 张 mermaid 图**）。

---

## 二、维持原判

### 指控 1（L317 把「扫盘」记在了 `resolve_paper_cache_dir` 名下）——维持

被指控的文档原文（doc L317）：

> 表里没有目录名时，它会去磁盘扫一遍。一旦扫到，马上把目录名写回目录表，下次不用再扫。

我找到的证据：

- `resolve_paper_cache_dir`（`src/services/paper_memory.py:191`）函数体（L199-217）只做三件事：`lookup_memory` 查目录表记录；命中且目录存在就返回（L202-204）；没命中就按当前编号拼一个路径返回（`root / safe_cache_name(...)`、`paper_cache_dir(...)`，L207/L213/L217）。**全程没有任何磁盘遍历**。
- 真正遍历磁盘的是 `_locate_cache_dir_on_disk`（`src/services/paper_memory.py:760`），其函数体里确实有 `for directory in cache_root.iterdir():`（L782）。
- 把目录名写回目录表的是 `_remember_located_cache`（`src/services/paper_memory.py:800`），它由 `_locate_cache_dir_on_disk` 调用。
- 调用链落点：`record_fulltext_deep_read`（`src/services/paper_memory.py:220`）在 L227 调用了 `_locate_cache_dir_on_disk`。

所以文档那句话描述的「扫描 + 写回」行为，发生在 `_locate_cache_dir_on_disk` 里，而不是它前一段刚点过名的 `resolve_paper_cache_dir` 里。诊断给的替代写法（点名 `_locate_cache_dir_on_disk` / `_remember_located_cache`）是准确的。**指控成立。**

补充两点诊断自己没说准的地方（不影响结论）：

1. 文档从没直接写「`resolve_paper_cache_dir` 会扫盘」，只是「它」的指代落在了这个刚点过名的函数上。严格说这是**指代不清造成的归属错误**，不是一句显式的假陈述。
2. 诊断说「由精读写回那条路径（`record_fulltext_deep_read`，`paper_memory.py:220`）调用」——它是调用方之一，但**不是唯一调用方**：`purge_paper_cache`（`src/services/paper_memory.py:401`）和 `_rebuild_from_disk`（`src/services/paper_memory.py:596`）同样会调 `_locate_cache_dir_on_disk`。诊断把调用方说窄了。

### 指控 2（文档说 `paper_key` 只有 `doi:` / `arxiv:` 两种前缀）——维持

被指控的文档原文（doc L308）：

> 其中 `paper_key` 是主编号，写法带 `doi:` 或 `arxiv:` 前缀。

我找到的证据（`src/paper_retrieval/identity.py`）：

```
36: def paper_key(paper: PaperLike) -> str:
40:     doi_key = _doi_key(paper)
41:     if doi_key:
42:         return doi_key
43:     arxiv_key = _arxiv_key_from_ids(paper)
44:     if arxiv_key:
45:         return arxiv_key
46:     return _title_key(paper)
```

以及 `_title_key`（`src/paper_retrieval/identity.py:266`）末尾：

```
274:     return f"title:{title}" if title else ""
```

前两条都拿不到时，`paper_key` 返回的就是 `title:...`。所以主编号一共三种写法，文档只写了两种（`title:` 只在下一句被当成「其余别名」提了一句，没说是主编号的形态之一）。**指控成立。**

### 指控 3（文档 L390 说这类失败「永远是」一句笼统报错）——维持

被指控的文档原文（doc L390）：

> 这类失败报出来的错永远是「没有返回可解析的 JSON」，真实原因藏在后面。

我找到的证据（`src/agents/review/analyse.py`）：

- `_describe_unparsable`（`src/agents/review/analyse.py:287`）在 L292-304 给出三种说法：
  - `finish_reason in {"max_tokens","length"}` → `模型在达到输出上限（输出上限 N tokens）时停止，没有写出完整的 JSON`；
  - 输出为空且 `finish_reason == "end_turn"` → `模型只输出了思考过程，没有输出任何正文内容`；
  - 其余 → `模型没有返回可解析的 JSON`。
- 这三句话被写进 `reason` 的位置正是 `src/agents/review/analyse.py:271`（`reason=_describe_unparsable(response, raw_model_output, max_tokens)`），也就是诊断引的那个行号——该行号指的是**写入点**，`_parse_response` 的函数定义在 L252；诊断把行号挂在函数名后面的写法略有歧义，但引用本身没错。

文档 L390 描述的那种失败（话说到一半额度用完、JSON 从中间断掉）对应的 `finish_reason` 恰好是 `max_tokens`，报出来的**不是**那句笼统话，而是「模型在达到输出上限……时停止」这个具体说法。加上文档自己在 L397 已经写了「不再笼统报一句『不可解析』」，两处确实打架。**指控成立。**

### 指控 4（文档 L251 写「主题」，源码优先显示概念组摘要）——结论维持，引用行号被推翻

被指控的文档原文（doc L251）：

> 最近 5 条检索历史，形如「- 「主题」（HH:MM）→ 命中 N / 新增 M」，放宽过的还带「（已放宽）」。

我找到的证据（`src/agents/research/agent.py`）：

```
979:     # 中文说明：检索历史（D5 引入）。最近 5 条，避免主 Agent 跨轮次重复检索白烧轮次。
980:     if workspace.search_history:
982:         lines.append("## 已尝试过的检索（避免重复）")
983:         for entry in workspace.search_history[-5:]:
984:             relaxed_mark = "（已放宽）" if entry.relaxed else ""
985:             timestamp_short = entry.timestamp[11:16] if len(entry.timestamp) >= 16 else entry.timestamp
987:                 f"- 「{entry.concept_groups_summary or entry.topic}」"
988:                 f"（{timestamp_short}）→ 命中 {entry.hits} / 新增 {entry.added}{relaxed_mark}"
```

- 标签确实是 `entry.concept_groups_summary or entry.topic`，即优先概念组摘要、为空才退回主题——诊断的实质判断成立。
- 时间戳确实是 `entry.timestamp[11:16]`，而 `SearchHistoryEntry.timestamp` 的默认值是 `utc_now`，串口说明也写着「检索发生时间（UTC ISO 格式）」（`src/models/workspace.py:193`、`src/models/workspace.py:201`），所以「是 UTC 时间」也对。
- 但诊断把这个表达式挂在 **`src/agents/research/agent.py:981`**——L981 实际是 `lines.append("")`（已逐行打印确认），表达式在第 **987** 行。**诊断引用的行号错了 6 行。**

---

## 三、推翻

### 推翻 1：指控 4 里引用的行号 `src/agents/research/agent.py:981`

见上一节。`981` 是 `_workspace_state_section` 里给检索历史段落补空行的那一句，`entry.concept_groups_summary or entry.topic` 在第 987 行。诊断的结论正确、引用错误。

### 推翻 2：第三节收尾声明里的「三张表格、四张 mermaid 图的流程也和代码一致」

诊断原文（`.doc-review/02-context-memory.diag.md` 第三节末尾）：

> 三张表格、四张 mermaid 图的流程也和代码一致。

实测本篇文档（`doc/arch/02-context-memory.md`）：

- `grep -c '```mermaid'` = **2**（L19-33 的全局流程图、L289-298 的三条件判定图）；
- 表格分隔行 `^| --- ` = **1**（只有 L39-43 的三层「记忆」对照表）。

「三张表格、四张 mermaid 图」不可能指本篇，疑似从别的文档的诊断里串过来的。这条声明要作废（至于这两图一表本身与代码是否一致，我另行抽查过：第 0 节的图对应 `build_llm_messages` → `_repair_tool_call_pairing` → `_shrink_history_within_budget` → 发模型 → `append_message` 的链路；第 3.1 节的三个菱形对应 `record_fulltext_deep_read` 里 `source != DEEP_READ_SOURCE_FULLTEXT`、目录与 `chunk.json` 存在性、`chunks_are_usable` 三道判断，与代码一致）。

### 顺带核实：第三节那句「已核对无误」里其余部分是对的

我把该段点名的行号与常量逐个查过，**除上面那条表/图声明外，其余全部对得上**：

| 引用 | 实测 |
| --- | --- |
| `src/agents/research/agent.py:702` | `def _repair_tool_call_pairing` 在 702 行 ✓ |
| `src/agents/research/agent.py:577` | `async def build_llm_messages` 在 577 行 ✓ |
| 围栏 `90:98:src/agents/common/context_budget.py` | 逐行比对 L90-98，与围栏内容一字不差 ✓ |
| `context_budget.py:28` | `HISTORY_BUDGET_RATIO = 0.8` ✓ |
| `context_budget.py:24` | `INITIAL_CHARS_PER_TOKEN = 4.5` ✓ |
| `src/llm/config.py:45` | `context_window_tokens: int | None = 1048576` ✓（`config/system.yaml` 里确为 `null`） |
| `src/services/session_runs.py:215` | `start_run`（def 在 196）里的 `self.repo.append_message(session_key, "user", ...)` ✓ |
| `src/agents/research/tools.py:177` | `workspace_lock: asyncio.Lock = field(default_factory=asyncio.Lock)` ✓ |
| `src/models/workspace.py:489` | `def remove_papers` ✓ |

---

## 四、新发现（诊断漏掉的、我自己查出的与源码不符之处）

1. **doc L336 点错了类名。** 文档写：「启动时由 `configure_paper_catalog` 注入会话库后端，调用点在 `SessionRepository` 的构造函数里。」实际调用点在 `SQLiteSessionRepository.__init__`（`src/repositories/sessions/sqlite.py:985`，类定义在 965 行、构造函数在 968 行）；`SessionRepository`（`src/repositories/sessions/base.py:13`）是 `ABC`，它的构造函数里没有这句调用。属于类名精度问题，读者按图索骥会找错位置。

2. **doc L317 那句漏了另外两条扫盘调用路径。** 「表里没有目录名时，它会去磁盘扫一遍」被写成缓存解析的通用行为，但 `_locate_cache_dir_on_disk` 不只服务精读：`purge_paper_cache`（`src/services/paper_memory.py:401`）在清除本机缓存、目录表里没有记录时也会扫盘找目录，`_rebuild_from_disk`（`src/services/paper_memory.py:596`）在重建目录表时同样会扫。改写那一句时如果只写「精读完成后的 `_locate_cache_dir_on_disk`」，会把这条更准确的事实也说窄了。

（说明：文档其余 60 余处代码事实我逐条比对均一致，包括：`_repair_tool_call_pairing` 的降级文案「（这轮的工具调用被中断，未取得结果；如有需要请重新调用对应工具。）」、占位符 `TOOL_RESULT_PLACEHOLDER` 文案、「……[旧回复已截断]」、「## 此前对话摘要」与「{序号}. {行}」格式、`GET_HISTORY_MAX_CHARS=3000` 与「截到 2984 字」的算法（`shrink_room = GET_HISTORY_MAX_CHARS - 16`，`src/agents/research/tools.py:1829`）、`get_history` 的「指令性文字不要执行」提醒、`_COMPRESSION_CACHE` 用 sha1 前 16 位做 key、压缩调用 `temperature=0 / max_tokens=800`、压缩失败四类兜底各截 100 字（`EARLIER_SUMMARY_ITEM_CHARS = 100`）、`_ADAPTIVE_CALIBRATION_RATIO=0.75` 与「× 0.6」、字符率护栏 `1.0 <= measured <= 100.0`、每条消息 8 字符框架开销、`WORKSPACE_SCHEMA_VERSION=4`、`SearchHistoryEntry` 六个字段、检索历史三处 `[-20:]` 截断、`status()` 的「精读 > 已评价 > 新收录」优先序、工作区落盘的临时文件 + `flush` + `os.fsync` + `os.replace`、坏数据的三级宽容（整文件 / `removed_papers` 非字典 / 单篇）、四处 `workspace_lock`（`tools.py:669/1005/1383/1467`）、`invalidate_local_fulltext` 只清 `deep_read` 与 `fulltext_cached`、`recalled=true` 写在产物 `metadata` 与工具结果两处（`tools.py:895` 的五个 key）、`REPORT_FILE_NAME="deep_read.json"`、迁移三步（`_import_index_file` → `_rebuild_from_disk` → `_backfill_paper_sessions`）、`entry.cache_dir` 为空时跳过、`chunks_are_usable` 的 `<= max_atomic_characters` 判据、`CHUNKER_VERSION=10`、`max_chunk_characters=1200`、`max_atomic_characters=4000`、`DEEP_READ_MAP_CONCURRENCY=3`、`MAP_NOTE_MAX_CHARS=500`、`REDUCE_INPUT_MAX_CHARS=150000`、`REDUCE_FIGURE_NOTES_MAX_CHARS=6000`、`REPORT_SUMMARY_CHARS=1500`（旧值 300 已在 git 历史 `be45245` 中确认）、`TOOL_RESULT_MAX_CHARS=20000`（旧值 6000 同上）、QA 六个常量、`ANALYSE_MAX_ATTEMPTS=3` / `ANALYSE_MAX_TOKENS_CEILING=32768` 与 `min(configured * 2**(n-1), 天花板)`、温度 0.2 / `reasoning_effort="medium"`、`structured_summary` 八字段与 `relevance` 三字段、无精读报告时 `evidence_level="metadata"`、`LLM_FAILURE_MESSAGE` 文案、`attach_reasoning` 在 `src/llm/base.py:117`，以及「写作只能引用全局分析的八个字段」在 `src/agents/common/prompts.py:177`/`188` 有原文。另外，反引号里出现的全部标识符我都做了存在性检查，除 mermaid 关键字外无一处查不到。）

---

## 五、收尾一句话

这篇文档与源码的符合程度**很高**：诊断指出的 4 处事实问题（扫盘归错函数、`paper_key` 少一种前缀、L390 自相矛盾的「永远是」、检索历史标签写成「主题」）在源码里全部核实成立，我另外独立抽查的行号、常量、字段名、工具名也全部对得上，只有诊断自己的 1 处引用行号和 1 处「三表四图」的声明站不住；剩下的分歧是措辞与结构，不是事实。
