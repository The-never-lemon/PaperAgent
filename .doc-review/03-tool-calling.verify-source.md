# 03-tool-calling.diag.md 对抗式复核（视角：逐行核对源码）

复核对象：`.doc-review/03-tool-calling.diag.md` 第三节「与源码不符的地方」
被诊断文档：`doc/arch/03-tool-calling.md`（只读，未改动）
复核方式：对诊断第三节每一条回到源码取证；另独立抽查该文档全部「带行号区间的引用」与「函数名/常量名/字段名」。

---

## 一、逐条复核：诊断第三节的三条

### 条目 1 —— 维持原判

**诊断原文**：`L426`（同表述也见 `L116`）文档说「上层只认一种结构：`ToolCallRequest(id, name, arguments)`」；源码实际有四个字段，第四个是 `provider_specific_fields`。证据 `src/llm/base.py:44`–`47`。

**核验结果：成立。** 亲自看到的证据：

- `src/llm/base.py:31` 定义 `@dataclass(slots=True) class ToolCallRequest`；
- `src/llm/base.py:44`–`47` 四个字段逐行落地：`id: str | None` / `name: str` / `arguments: Any` / `provider_specific_fields: JsonObject = field(default_factory=dict)`。行号与诊断完全吻合。
- 这个第四个字段不是摆设，**两处适配器都在真塞值**：
  - `src/llm/openai_compat.py:283`：`ToolCallRequest(call.get("id"), fn.get("name", ""), args, call)` —— 把上游原始 call 字典当第四个参数传进去；
  - `src/llm/openai_compat.py:339`–`344`：流式路径传 `{"stream_aggregated": True}`。

**说明（重要）**：文档那句话括号里的 `(id, name, arguments)` 可以理解为「上层用到的字段」，但字面上它是一份不完整的字段清单，源码里这份结构确有四个字段。故维持原判，建议按诊断给的改法补一句，严重程度：中低。

---

### 条目 2 —— 推翻

**诊断原文**：`L369` 文档说「三个来源一起查。源码里是 arXiv 和 OpenAlex。第三家是 Semantic Scholar。」；**源码实际：三个来源在同一次初始化里一起注册，不存在「前两家在源码里、第三家另算」的分法**。

**核验结果：诊断引用的代码事实全部属实，但「与源码不符」这个定性不成立。**

先说属实的部分（我亲自查到的）：

- `src/paper_retrieval/service.py:423`–`437`，`_build_default_connectors` 一次性返回四个键：`{"openalex": openalex, "semantic_scholar": semantic, "semantic": semantic, "arxiv": arxiv}`，行号与诊断一致；
- `service.py:467`–`470`，`_select_connectors` 在无参分支按对象身份 `id(connector)` 去重，行号也一致。

再说为什么推翻：

1. 文档那句话**列举的三个来源名一个都没错**。源码侧的三来源就是 `arXiv`、`OpenAlex`、`Semantic Scholar`：`src/agents/research/tools.py:95` 写着 `VALID_SEARCH_SOURCES = ("arxiv", "openalex", "semantic_scholar")`，与 `_build_default_connectors` 的注册表完全对应。文档写这三个名字，与源码一致。
2. 诊断把它读成「前两家在源码里、第三家另算」，**这层意思是诊断自己加进去的**，文档并没有这句话，也没有任何「Semantic Scholar 不在默认注册表里」的暗示。`_select_connectors(None, None)` 走 `unique` 分支，默认就是把三家的 connector 全部选中（`search` 里 `selected` 非 1 时走 `_async_search_many`，多源并发）。
3. 因此这条真正的问题是**句子读不通**（可读性），不是**事实不符**。诊断把可读性问题放进了「与源码不符」小节，属于立项错误。

结论：作为「与源码不符」的指控，予以推翻。但「那句话读三遍读不懂」的意见本身是对的，改写建议（明确写出「默认注册三家：arXiv、OpenAlex、Semantic Scholar，三家一起并发查」）可采纳。

**顺带记录一个真源码细节（非文档错误，可见下方「附注」）**：`service.py:468` 是 `unique[id(connector)] = (name, connector)`，字典键相同则后写覆盖先写，而 `"semantic"` 排在 `"semantic_scholar"` 之后，所以默认多源检索里该 connector 对外露出的名字是 `semantic`，`sources_used`／`source_results`／`errors` 的键都长这样。

---

### 条目 3 —— 半推翻（事实观察成立，「与源码不符」的定性不成立）

**诊断原文**：`L227` 文档说「`get_history` 的 `turns` 取 1 到 10，默认 3」；**源码实际：JSON schema 只声明「默认 3，最大 10」，下限 1 来自 handler 硬编码截断**。证据 `tools.py:467`–`470`、`tools.py:1770`。（诊断同句末尾自己也写「文档的结论没错」。）

**核验结果：两处引用都准，但整条不构成「与源码不符」。**

- `src/agents/research/tools.py:467`–`470`：`parameters_schema.properties.turns` 这一段，描述原文确实是「往回数几个「用户轮」……；默认 3，最大 10」，**没有 `minimum` / `maximum` 键**；
- `src/agents/research/tools.py:1770`：`cleaned_turns = max(1, min(cleaned_turns, 10))`，**逐字命中**，行号分毫不差；
- handler 里 `turns=None → 3`（`tools.py:1766`–`1769`），再被上面那行夹到 `[1, 10]`。

所以**文档写的「取 1 到 10，默认 3」与代码实际行为完全一致**；schema 只写「默认 3，最大 10」只是文档没有交代下限的来源。诊断自己也承认「文档的结论没错」——把它列在「与源码不符的地方」之下，属于小节归属错误。

结论：定性推翻、事实观察保留。补一句「下限由代码截到 1」的建议可以有，但不是纠错。

---

### 本节统计

| 条目 | 结论 |
| --- | --- |
| 条目 1（`ToolCallRequest` 少一个字段） | **维持原判**（证据 `src/llm/base.py:44`–`47`，且该字段被两处适配器实际使用） |
| 条目 2（三个来源的说法） | **推翻**（代码事实属实，但文档从未主张诊断所读出的那层意思；文档列出的三个来源名与源码一致） |
| 条目 3（`get_history` 的 `turns`） | **半推翻**（引用两处行号都准，但文档表述与代码行为一致，不是不符） |

---

## 二、独立复核：文档里全部「带行号区间的引用」

文档只有 10 处行号围栏（用 `grep` 扫过，除这 10 处外没有别的 `起始行:结束行:路径` 形态引用）。全部亲自打开源码逐行核对：

| 围栏 | 独立核验结果 |
| --- | --- |
| `20:80:src/agents/common/tool_registry.py` | `class ToolSpec` 在第 20 行 ✓；`as_llm_tools` 的列表收尾 `]` 在第 80 行 ✓ |
| `512:534:src/agents/research/tools.py` | `def build_research_tool_registry` 在第 512 行 ✓；`return registry` 在第 534 行 ✓；「中间 8 行同形」= 525–532 正好 8 行 ✓ |
| `2:6:src/agents/research/tools.py` | 模块 docstring 起于第 2 行 ✓；「验收后就冻结……标注 [protocol]；」止于第 6 行 ✓（第 1 行是 `#` 注释，不在围栏内，正确） |
| `1137:1148:src/agents/research/agent.py` | `    try:` 在第 1137 行 ✓；末行 `return {"error": f"工具执行失败：{exc}"}` 在第 1148 行 ✓ |
| `118:129:src/agents/research/agent.py` | 失败状态注释起于 118 ✓；`FAILED_TOOL_STATUSES = frozenset({"failed", "download_failed"})` 在第 129 行 ✓ |
| `486:504:src/agents/research/tools.py` | `def render_tool_result` 在第 486 行 ✓；`return text` 在第 504 行 ✓（围栏正文的完整性问题见「新发现」N2） |
| `539:540:src/paper_retrieval/service.py` | 退避注释与 `delay = min(60, 5 * (3 ** attempt)) + random.uniform(0, 1.0)` 逐字一致，行号一致 ✓ |
| `295:317:src/llm/openai_compat.py` | `def _accumulate_tool_call_delta` 在第 295 行 ✓；末行 `slot["arguments_parts"].append(arguments)` 在第 317 行 ✓ |
| `305:326:src/llm/anthropic.py` | `def _convert_messages` 在第 305 行 ✓；docstring 第 3 条止于第 326 行 ✓ |
| `14:14:src/llm/anthropic.py` | `INTERRUPTED_TOOL_RESULT = '{"status": "interrupted", ...}'` 就在第 14 行 ✓ |

**10/10 全部对得上**，与诊断的核对结论一致。

### 名称级抽查（文档出现的函数名/常量名/字段名，逐个回源码找）

共抽查 40 余处，**全部能在源码里找到**，无一处凭空捏造。抽到的清单：

- 注册表侧：`ToolSpec`、`Tool`、`ToolRegistry`、`register`、`require`、`select`、`call`、`acall`、`as_llm_tools`、`parameters_schema`、`handler`（`src/agents/common/tool_registry.py`）；
- 工具侧：`ResearchToolContext`、`_ACTIVE_CALL_VAR`、`get_active_call`、`build_research_tool_registry`、`render_tool_result`、`TOOL_RESULT_MAX_CHARS=20000`、`MAX_RELAXATIONS=2`、`SEARCH_LIMIT_*`/`EXPAND_LIMIT_*`/`LIST_LIMIT_*`、`EVALUATE_MAX_PAPERS=10`/`EVALUATE_CONCURRENCY=3`、`GET_HISTORY_MAX_CHARS=3000`、`_infer_title_query`、`_normalize_tool_calls`（`f"call_r{round_no}_{index}"`，序号从 1 起）、`_UNTRUSTED_SUBAGENT_NOTE`、`workspace_lock`、`research_topic`、`arxiv_id`/`pdf_url`/`paperId`/`cited_by_count`；
- 主循环侧：`MAX_TOOL_ROUNDS=20`、`TOOL_PARALLEL_LIMIT=3`、`TOOL_ERROR_DISPLAY_CHARS=200`、`FAILED_TOOL_STATUSES`、`ROUND_LIMIT_INSTRUCTION`、`_assistant_tool_call_message`、`yield_to_event_loop`、`note_streaming_tool_call`、`_arguments_summary`、`event_key`；
- 子 Agent 侧：`DeepReadDeps`/`PaperQaDeps`（`src/agents/reading/paper_qa.py:86`）/`ReviewDeps`（`src/agents/review/pipeline.py:122`）、`run_deep_read`、`run_paper_qa`、`run_review`、`REPORT_SUMMARY_CHARS=1500`、`QA_READ_MAX_CHUNKS=6`/`QA_READ_CHUNK_MAX_CHARS=1200`/`QA_TOC_MAX_ENTRIES=60`/`QA_TOC_PREVIEW_CHARS=60`/`QA_MAX_TOOL_ROUNDS=4`/`QA_CONTEXT_MAX_CHARS=30000`/`QA_ERROR_DETAIL_CHARS=500`、`qa_call_{序号}`、`read_sections`、`get_extraction`/`search_section`/`get_chunk_by_embed`、`execute_writing_tool`；
- LLM 侧：`_accumulate_tool_call_delta`、`_finalize_stream_tool_calls`、`parallel_tool_calls`、`_sanitize_messages`、`_enforce_role_alternation`、`max_completion_tokens`、`max_retries`、`_convert_messages`、`pending_tool_results`、`flush_tool_results`、`input_schema`、`reasoning_blocks`、`thinking_delta`/`signature_delta`/`input_json_delta`、`INTERRUPTED_TOOL_RESULT`；
- 检索侧：`_build_default_connectors`、`_select_connectors`、`_merge_duplicates`、`RRF_K=60`、`OVER_FETCH_FACTOR=3`、`_is_retryable_search_error`、`_RETRYABLE_STATUS_CODES=(429,500,502,503,504)`、`async_related`、`_arxiv_external_ref`、`_normalize_arxiv_id`。

另附几处容易被写错、文档却写对了的细节（均已核到）：

- 「推理模型判据=去掉 `provider/` 前缀后以 `o1/o3/o4/gpt-5` 开头」——`openai_compat.py:425`–`427`（`model.lower().split("/", 1)[-1]`）；
- Anthropic 拒绝 `temperature` 的五个前缀与 `reasoning_effort` 五档「未知值按 `high`」——`anthropic.py:541`–`547`、`521`–`532`；
- `_sanitize_messages` 白名单恰好 6 个字段：`role`/`content`/`tool_calls`/`tool_call_id`/`name`/`reasoning_content`——`openai_compat.py:375`；
- 无 `tool_calls` 的末尾 assistant 被丢掉——`openai_compat.py:409`–`411`；
- `yield_to_event_loop()` 就是 `asyncio.sleep(0)`——`src/llm/base.py:196`–`203`；
- 流式预览键 `f"{name}_r{round_no}_{index}"`、退回键 `f"{call['name']}_{tool_call_seq}"`——`agent.py:1066`、`agent.py:423`；
- 隐藏的失败判据 `isinstance(result, dict) and (bool(result.get("error")) or result.get("status") in FAILED_TOOL_STATUSES)`——`agent.py:477`–`479`；
- `get_history` 超长处理是「先把最老一行截短、仍超再丢整行」——`tools.py:1824`–`1843`；
- `_UNTRUSTED_SUBAGENT_NOTE` 文本与文档引文逐字一致，且引文里的「子节点」确实是源码原话——`tools.py:106`–`109`；
- `front/src/types/chat.ts:11` 里确实写着「并在提交信息里标注 [protocol]」——`front/src/types/chat.ts`；
- 文档的 5 个延伸阅读链接全部存在：`doc/arch/README.md`、`01-agent-orchestration.md`、`02-context-memory.md`、`04-availability.md`、`doc/LLM-README.md`。

---

## 三、新发现（诊断没提、我查到的与源码不符之处）

严重程度都不高，但都属于「围栏号称逐字、实际不是」这一类。

**N1｜`20:80:src/agents/common/tool_registry.py` 围栏不是逐字源码。**
文档第 138 行明写「下面这段是逐字源码」，但围栏正文里：

- 漏了第 28 行的 `@dataclass(slots=True)`（`class Tool:` 的装饰器），且这里**没有** `# ...` 之类的省略标记；
- 漏了第 33–34 行的 `def call(self, **kwargs: Any) -> Any:` / `return self.handler(**kwargs)`，同样没有省略标记（正文直接把 `handler` 字段接到 `async def acall`）。

（`ToolSpec` 头顶的第 19 行 `@dataclass(frozen=True, slots=True)` 不在 [20,80] 区间内，不算漏。）
另：文档第 175 行的行内说法「`call` 是同步入口」是对的，源码确有该方法，所以只是围栏呈现问题。

**N2｜`486:504:src/agents/research/tools.py` 围栏的正文没覆盖到行号区间的末尾。**
围栏头标到 504，正文却停在 499 行那句 `text = text[:TOOL_RESULT_MAX_CHARS] + f"……[内容过长已截断，原始长度 {len(text)} 字符]"` 就结束了。区间内的 500–503 行（`logger.debug("工具结果已渲染", ...)`）与第 504 行的 `return text` 都没写出来，也没有省略标记。后果是读者会以为 `render_tool_result` 只做截断、不写日志、不返回。

**N3（轻微）｜文档第 224 行「总分由程序按固定表算」与源码措辞不一致。**
源码 `tools.py:340` 的原话是「总分由程序按**固定权重**计算」。文档写成「按固定表算」，读者去源码里搜「固定表」搜不到。属于措辞漂移，不是事实错误。

> 说明：除以上三处，我另外扫过的两处「疑似」都排除了——第 242–244 行说 `get_chunk_by_embed`「名字出现在给模型的工具清单里」，`src/agents/review/writing.py:658`–`660` 的提示词清单里确实列了它；第 337 行「精读摘要先截到 1500 字」也对得上 `deep_read.py:95` 的 `REPORT_SUMMARY_CHARS = 1500`。

---

## 附注：复核中发现的两处「非文档问题」

不影响文档结论，但记下来备查：

1. **诊断的路径过期**：诊断第三节末段引用「`chat_runtime.py:134`」，该文件在最近一次目录重构后已挪到 `src/services/chat_runtime.py`（`src/agents/research/` 下没有这个文件）。第 134 行的内容（`registry = build_research_tool_registry(context)`）本身对得上。
2. **默认多源里 Semantic Scholar 对外叫 `semantic`**：如条目 2 所述，`service.py:467`–`470` 的去重循环里 `"semantic"` 覆盖 `"semantic_scholar"`，因此运行时 `sources_used` 等键是 `openalex` / `semantic` / `arxiv`。文档没有写到这一层，所以不算文档错误；但如果后续要补「三个来源」那段的措辞，这里值得留个心。

---

## 一句话结论

这篇文档**与源码的符合程度很高**：10 处行号围栏我独立核对全部命中，40 余个函数名/常量名/字段名无一凭空捏造，检索、问答、LLM 适配三层的关键流程与常量也都逐条对得上；诊断第三节的三条里只有第一条（`ToolCallRequest` 少写一个字段）站得住，另两条是「把可读性问题误报成事实不符」和「诊断自己也承认文档没错」，真正需要改的只是三处围栏「号称逐字实为节选」的呈现问题。
