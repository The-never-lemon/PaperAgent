# doc/arch/03-tool-calling.md 对抗式复核（视角：找反例和边界）

> 复核对象：`.doc-review/03-tool-calling.diag.md` 的「三、与源码不符的地方」
> 被诊断文档：`doc/arch/03-tool-calling.md`
> 对照源码：`src/agents/research/tools.py`、`src/agents/common/tool_registry.py`、`src/agents/common/contracts.py`、`src/llm/openai_compat.py`、`src/llm/anthropic.py`、`src/agents/reading/paper_qa.py`（另按需查了 `agent.py`、`base.py`、`service.py`、`writing.py`、`read_models.py`、`connectors/base.py`、`chat_runtime.py`）
>
> 做法：诊断第三节共 3 条指控，逐条回源码查证，默认它是错的；另外独立抽查了 20 处代码事实（见第四节），覆盖默认值、数量、顺序、是否落库、失败降级五类。
>
> 结论概览：**3 条指控里 2 条被推翻（L369、L227），1 条维持但严重性要下调（L426/L116）**。诊断反过来漏掉了 5 处与源码对不上的地方（见第三节），其中「§8 卡片时序把两步写反」是唯一一条会被读者当场验证的硬伤。

---

## 一、维持原判（证据无法被推翻）

### 1. 诊断原文
> `L426`（同一表述也出现在 `L116`）文档说：「上层只认一种结构：`ToolCallRequest(id, name, arguments)`。」；**源码实际：`ToolCallRequest` 有四个字段，第四个是 `provider_specific_fields`**。

### 我的核验：指控成立，但严重性比诊断写的低
- 证据：`src/llm/base.py:35`–`47`。`@dataclass(slots=True) class ToolCallRequest` 的字段依次是 `id: str | None`（44 行）、`name: str`（45 行）、`arguments: Any`（46 行）、`provider_specific_fields: JsonObject = field(default_factory=dict)`（47 行）。四个字段，诊断的引证准确。
- 但诊断把它和「面试会追问戳穿」的另两处并列，**放大了严重性**。两点反证：
  1. 第 4 个字段全仓没有被读过。`grep -rn "provider_specific_fields" src/` 只命中 `src/llm/base.py:39`（docstring）、`src/llm/base.py:47`（定义）、`src/config/baseAgent.md:154`（另一份文档的字段清单），没有任何读取点。文档写「**上层**只认」`id/name/arguments`，作为「上层用到哪几个字段」的陈述是对的。
  2. 该字段**确实会被写入**：`src/llm/openai_compat.py:340`–`344` 构造流式工具调用时按位置传了 `{"stream_aggregated": True}`。所以它不是纯粹的占位字段，诊断建议的改写（「另有一个 `provider_specific_fields` 存原始供应商字段」）值得采纳。
- 维持的理由：文档用 `ToolCallRequest(id, name, arguments)` 这种「构造函数式」写法枚举字段，读者容易当成完整字段表；补一句的成本很低。按可读性修订处理即可，不必按「事实错误」对待。

---

## 二、推翻

### 1. 诊断原文（`L369`）
> 文档说：「三个来源一起查。源码里是 arXiv 和 OpenAlex。第三家是 Semantic Scholar。」；**源码实际：三个来源在同一次初始化里一起注册，不存在「前两家在源码里、第三家另算」的分法**。

### 为什么推翻
- 文档这句话的**事实内核是对的**：三家确实是 arXiv、OpenAlex、Semantic Scholar，并且真的「一起查」。证据：
  - `src/agents/research/tools.py:95`：`VALID_SEARCH_SOURCES = ("arxiv", "openalex", "semantic_scholar")`——文档那句就是把这个元组的顺序念歪了（前两项 arXiv 和 OpenAlex，第三项 Semantic Scholar）。
  - `src/agents/research/tools.py:599`–`605`：`sources=cleaned_sources or None`，不传 sources 时交给服务层。
  - `src/paper_retrieval/service.py:423`–`437`：`_build_default_connectors` 一次返回 `{"openalex", "semantic_scholar", "semantic", "arxiv"}`；`service.py:465`–`470` 的 `_select_connectors` 按 `id(connector)` 去重，`semantic` 与 `semantic_scholar` 是同一个对象，最终就是三家并发。文档说「三个来源一起查」与代码一致。
- 所以这是**中文表达崩了**（一句话自相矛盾），不是「与源码不符」。诊断 §五第 7 条其实已经把它归为「读三遍也读不懂」，同一个问题在两处给了两种定性，应统一为可读性问题。改写建议本身（「默认注册三家来源：arXiv、OpenAlex、Semantic Scholar」）可以照用。

### 2. 诊断原文（`L227`）
> 文档说：「`get_history` 的 `turns` 取 1 到 10，默认 3。」；**源码实际：工具的 JSON schema 只声明了「默认 3，最大 10」，下限 1 来自 handler 里的硬编码截断**。

### 为什么推翻
- 文档描述的是**运行期真实行为**，而且完全正确：
  - `src/agents/research/tools.py:1765`：注释「turns 限制在 1~10，默认 3。」；`tools.py:1767`：`cleaned_turns = int(turns) if turns is not None else 3`；`tools.py:1769`：解析失败也回落 3；`tools.py:1770`：`cleaned_turns = max(1, min(cleaned_turns, 10))`。
  - 无论 schema 怎么写，`turns=0` 会被抬到 1，`turns=99` 会被压到 10，不传是 3。「取 1 到 10，默认 3」不是近似，是精确。
- 文档那句话**没有在描述 schema**，它描述的是工具行为。诊断自己也承认「文档的结论没错」，却把这一条放进「与源码不符的地方」，属于分类错误。补一句「下限由代码截到 1」是锦上添花，不是纠错。

---

## 三、新发现（诊断漏掉、我查出的与源码不符之处）

### 1.（硬伤，顺序写反）§8 卡片时序的第 2、3 步与源码顺序颠倒
- 文档 `L504`–`L509`：「2. 执行 `await yield_to_event_loop()`，让 SSE 先把卡片推出去，再去执行很慢的检索或精读。3. 执行期间用同一个 `event_key` 把卡片更新成「正在执行」。详情里带 `arguments` 和 `arguments_summary`。」
- 源码 `src/agents/research/agent.py:450`–`463`：先 `tool_reporter.started(f"正在执行 {call['name']}", ..., arguments=..., arguments_summary=...)`（450–457 行），**然后**才 `await yield_to_event_loop()`（463 行）。也就是说：被「推出去」的正是「正在执行」那张卡，它是 yield 的**原因**，不是 yield 的**结果**。
- 按文档现在的顺序读，会得出「先让出、再把卡片改成正在执行」，与代码相反。另外源码在执行前（463）和完成后（490）各有一次 yield，文档只写了前一次。
- 建议：把第 2、3 步对调，改成「先把同一张卡更新成『正在执行』（带 `arguments` / `arguments_summary`），再 `await yield_to_event_loop()` 让 SSE 把它推出去」。

### 2.（张冠李戴）§7 `L457` 把 `ValueError` 记到了 `_enforce_role_alternation` 名下
- 文档 `L457`：「`_enforce_role_alternation` 合并连续的同角色消息。直接丢掉末尾那条没有 `tool_calls` 的 assistant。历史回复不能当成待回答的输入发回去。role 不在合法取值里时抛 `ValueError`。」
- 源码：抛 `ValueError` 的是 `_sanitize_messages`（`src/llm/openai_compat.py:357`–`359`：`if role not in {"system","user","assistant","tool"}: raise ValueError(...)`），`_enforce_role_alternation`（`openai_compat.py:371`–`393`）里没有任何 raise。
- 另外「合并连续的同角色消息」也不够准：`openai_compat.py:386`–`388` 只合并 `user`/`assistant` 的**文本内容**（源码注释原话「只合并文本内容，不跨 role 合并，也不改动 system/tool 的语义边界」）。
- 这条被诊断完全放过了（`L457` 只被当成否定句在 §二里提了一遍）。

### 3.（边界不可达）§4 `L382`「退避量级是 5 秒、15 秒、45 秒」里的 45 秒实际不会发生
- 源码 `src/paper_retrieval/service.py:191`（同步单源）与 `service.py:533`（异步，`_async_search_with_retry`）：循环是 `for attempt in range(3)`，`attempt == 2` 时**直接 raise**，只有 attempt 0/1 会走到 sleep。所以真实退避只有 5 秒和 15 秒两次，`min(60, 5 * 3 ** 2)` 那个 45 秒永远轮不到。
- 文档是照抄源码注释（`service.py:197`/`539` 的「(5s/15s/45s)」），不算编造；但既然是「优秀技术文档」，建议写成「退避 5 秒、15 秒两档，第三次直接失败」或者注明第三档是预算上限。
- 「最多尝试 3 次」「可重试状态码 `(429, 500, 502, 503, 504)`」两句都正确（`service.py:27`、`service.py:534`）。

### 4.（围栏残缺）§3.3 的 `486:504` 围栏没贴到 504 行，也没有省略标记
- 文档 `L320`–`L335` 围栏标的是 `486:504:src/agents/research/tools.py`，但最后一行停在源码 `499` 行（`text = text[:TOOL_RESULT_MAX_CHARS] + ...`），中间的 `logger.debug(...)`（500–503 行）和 `return text`（504 行）没有出现，也没有 `# ...` 之类的省略说明。
- 对照：文档别处的省略都做了标注（`L201`「# ... 中间 8 行同形」、`L155` 的 `# ...`），这里漏了。读者按「逐字源码」的约定去对行号会对不上。

### 5.（措辞过宽，非错误）决策 8「单源和多源两条路径合并到同一个带重试的入口」只对异步路径成立
- 异步路径确实统一：`service.py:505` 的 `_async_search_with_retry` 被单源（`service.py:304`）和多源（`service.py:572`）共用，其 docstring 也这么写。
- 但同步路径不是：同步多源的 `_search_many`（`service.py:472`–`501`）**完全不重试**，捕获异常后直接记进返回值；同步单源另有一份内联重试循环（`service.py:184`–`204`）。加起来退避代码出现三次（`198`、`539` 是同一段复制）。
- 文档没说错（主 Agent 走的是 `async_search`，见 `tools.py:601`），但「重试规则全仓只有一份」严格讲只对「判定规则」（`_is_retryable_search_error`，`service.py:39`）成立，退避循环不是一份。

---

## 四、独立抽查的代码事实（20 处，全部逐条回源码核对）

| # | 文档说法 | 核验结果与证据 |
| --- | --- | --- |
| 1 | 「11 个工具」（`L182`/`L192`/`L190`） | ✅ 正确。`tools.py:523`–`533` 正好 11 行 `registry.register`；`tools.py:518` 的 docstring 自称「至此十个工具全部注册」是源码自身的过时描述，文档不受影响（诊断已提示） |
| 2 | `MAX_TOOL_ROUNDS = 20`、`TOOL_PARALLEL_LIMIT = 3` | ✅ `agent.py:54`、`agent.py:57` |
| 3 | `TOOL_RESULT_MAX_CHARS = 20000`、`TOOL_ERROR_DISPLAY_CHARS = 200`、`GET_HISTORY_MAX_CHARS = 3000` | ✅ `tools.py:56`、`agent.py:150`、`tools.py:1747` |
| 4 | `SEARCH_LIMIT 5/15/10`、`EXPAND_LIMIT 5/15/10`、`LIST_LIMIT 20/50` | ✅ `tools.py:59`–`89`；schema 里引用的是 `SEARCH_LIMIT_MIN/MAX`（`tools.py:249`–`253`）、`EXPAND_LIMIT_MIN/MAX`（`tools.py:290`–`291`） |
| 5 | `get_history` 返回上限 3000，超长**先截短最老一行、仍超才丢整行** | ✅ `tools.py:1823`–`1840`：`shrink_room = GET_HISTORY_MAX_CHARS - 16`，`lines[0]` 先截短再 `pop(0)` |
| 6 | `evaluate_papers` 总分按固定表算、不采信模型自报 | ✅ `relevance.py:303`–`304`、`read_models.py:60`–`64`；三维度 `research_question`/`research_object_or_scene`/`method_or_technical_route`（`read_models.py:31`–`35`），与文档「研究问题、研究对象或场景、方法或技术路线」一致 |
| 7 | `download_paper` 命中本地缓存直接返回 `cached` | ✅ `tools.py:1502`–`1504`：`if entry.fulltext_cached: return {"status": "cached", "reason": "全文已在本地缓存"}` |
| 8 | `remove_papers` 进归档、不永久删除（即文档表格里的「可恢复」） | ✅ `tools.py:1476`–`1477`、spec 描述 `tools.py:366` |
| 9 | `deep_read_paper` 带 `force` 时清旧报告重读 | ✅ `tools.py:1565`、「force=true 时清掉旧报告并用本地片段重读」，透传在 `tools.py:1597` |
| 10 | `ask_paper` 由 handler 拦住「未精读」 | ✅ `tools.py:1640`–`1646`：`if entry.deep_read is None: return {"error": "这篇论文还没有精读…"}` |
| 11 | `generate_review` 没传 topic 用工作区 `research_topic`；指定的 id 全不在工作区直接报错 | ✅ `tools.py:1695`–`1709` |
| 12 | 「三个落点（精读/追问/记忆召回）都只在字段非空时才加，失败结果不加」 | ✅ `tools.py:1605`–`1608`（`isinstance(summary, str) and summary.strip()`）、`tools.py:1672`–`1676`、`tools.py:885`–`887`；`_UNTRUSTED_SUBAGENT_NOTE` 原文在 `tools.py:106`–`109`，逐字一致 |
| 13 | 「精读摘要回给主 Agent 之前先截到 1500 字」 | ✅ `deep_read.py:95` `REPORT_SUMMARY_CHARS = 1500`；截断点 `tools.py:886`，加框定在后一行 |
| 14 | `select(names)` 当前没有调用点 | ✅ `grep -rn "\.select(" src/`（排除 pycache）零命中；主 Agent 拿整表：`src/services/chat_runtime.py:134` `registry = build_research_tool_registry(context)` |
| 15 | `require` 找不到工具抛 `ValueError` | ✅ `tool_registry.py:64`–`68`；`as_llm_tools` 产出 `type`/`function`/`parameters`，`tool_registry.py:70`–`80`（列表止于 80，围栏 `20:80` 对得上） |
| 16 | 外部标识优先级 DOI > arXiv > S2 `paperId` > OpenAlex id；arXiv 只认两种形状、剥版本号、翻成 `ARXIV:` | ✅ `tools.py:1066`–`1094`、`_arxiv_external_ref` `tools.py:1098`–`1115`、`_normalize_arxiv_id` `tools.py:1119`–`1140`（正则 `\d{4}\.\d{4,5}` 与 `[A-Za-z\-]+(?:\.[A-Za-z\-]+)?/\d{7}`） |
| 17 | arXiv connector 未实现 `async_related`，基类抛 `NotImplementedError`，检索服务按空处理 | ✅ `connectors/base.py:76`–`83` raise；`service.py:399`–`400` `except NotImplementedError: return []` |
| 18 | RRF 公式 `1 / (60 + rank + 1)`（文档 `L371`） | ✅ `service.py:232`、`service.py:341`。注意 `service.py:670` 的同名注释写成 `1/(RRF_K + rank)`（少 `+1`），是源码注释自身不一致——诊断提醒「别引用那句注释」是对的，文档写的是代码实际行为 |
| 19 | §3 七步主循环顺序（查停止 → 流式调模型 → tool 分支 → assistant 落库 → 并行执行最多 3 个 → 按原顺序回填并落库 → 下一轮） | ✅ `agent.py:242`（`check_cancelled`）、`agent.py:380`–`410`（先 `messages.append(assistant_tool_message)` 再 `repo.append_message` 落库，都在执行之前）、`agent.py:413`–`430`（`asyncio.Semaphore(TOOL_PARALLEL_LIMIT)`）、`agent.py:503`–`526`（`for pr in parallel_results` 按序回填 + 逐条落库） |
| 20 | §6 追问小循环：最多 6 片/1200 字、最多 4 轮且最后一轮不带工具、缺 id 补 `qa_call_{序号}`、两个参数都可选 | ✅ `paper_qa.py:59`/`60`、`paper_qa.py:229`+`234`（`tools_for_this_round = qa_tools if qa_round < QA_MAX_TOOL_ROUNDS else []`）、`paper_qa.py:588`–`590`（仅在 id 为空时补）、`paper_qa.py:485`–`508`（schema 无 `required`）；`QA_TOC_MAX_ENTRIES`/`QA_TOC_PREVIEW_CHARS`/`QA_CONTEXT_MAX_CHARS`/`QA_ERROR_DETAIL_CHARS`/`QA_MAX_TOOL_ROUNDS` 分别落在 `paper_qa.py:63`/`66`/`606`/`71`/`74`，与诊断的行号全部一致 |

补充说明（不算发现，供改写时参考）：诊断第三节里列的那些行号围栏，我重点复核了最容易出问题的 `539:540:src/paper_retrieval/service.py`——该文件里同一段退避代码出现两次（同步 `197`–`198`、异步 `539`–`540`），文档引用的是异步那一份，**行号正确**；`486:504` 见第三节第 4 条（内容残缺，行号本身对）；`1137:1148`、`118:129`、`512:534`、`2:6`、`20:80`、`295:317`、`305:326`、`14:14` 与源码逐行对得上。

---

## 五、一句话结论

诊断提出的 3 条「与源码不符」里，**2 条站不住**（`L369` 是中文表达崩坏、`L227` 与运行期行为完全一致），只有 `ToolCallRequest` 少写一个字段成立且严重性要下调；反过来，诊断漏掉了 5 处真正对不上的地方，其中「§8 把 yield 与『正在执行』两步顺序写反」是新读者一按代码就能验证的硬伤——**这篇文档与源码的符合程度很高（抽检 20 处代码事实全部正确），剩下的问题集中在时序描述、函数归属这类「表述精度」上，而不是事实编造。**
