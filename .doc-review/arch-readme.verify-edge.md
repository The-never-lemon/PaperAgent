# doc/arch/README.md 诊断复核（视角：找反例和边界）

复核对象：`.doc-review/arch-readme.diag.md` 第三节「与源码不符的地方」，共 8 条。
复核做法：每条都**默认它是错的**，回源码找反例；重点查默认值、数量、顺序、是否落库、失败降级。
结论：**维持 4 条（1、3、5、8，其中 5/8 性质是"漏写"），半维持 1 条（6），推翻 3 条（2、4、7）**。

---

## 一、维持原判（回源码找不到反例，只能承认）

### 第 1 条：`L101`「统一解析入口」——维持

文档原文：`llm_json.py            模型输出 JSON 的统一解析入口`

反例检索（想找"其实全仓都走它"的分支或开关）：找不到。大纲和逐节写作是无条件走自己的解析，没有任何配置能让它们改走 `parse_llm_json`。

证据：

- `src/agents/review/outline.py:165 def _extract_json_object()`，唯一调用点 `outline.py:77`。
- `src/agents/review/writing.py:810 def _extract_json_object()`，调用点 `writing.py:178`、`:226`、`:341`。
- `src/agents/review/analyse.py:283` 调 `parse_llm_json(text, fallback={})`，**没传** `repair`；它那层重试是 `_chat_json_with_retry`（`analyse.py:112` 起）自己实现的。
- 确实走统一入口的是：`deep_read.py:712`、`:827`（map/reduce）、`paper_qa.py:326`、`analyse.py:283`、`figures.py:371`。
- `src/utils/llm_json.py:4-5` 的 docstring 自称"所有需要模型产出结构化数据的 Agent……都统一调用"——文档 L101 大概率是照抄了这句，而这句本身也与代码不符。

维持。诊断给出的替换表述（"精读、问答、图片解读、综述分析共用这一份解析；大纲和逐节写作仍各自解析"）与源码一致。

### 第 3 条：`L105` 的 `baseAgent.md` 不是本项目文档——维持

证据：

- `src/config/baseAgent.md:1` 标题为「Nanobot 多厂商大模型适配架构设计文档」；第 6 行起列的源码模块全是 `nanobot/providers/*.py`。
- `grep -c "Paper-Agent" src/config/baseAgent.md` = **0**。
- `ls src/config/` 只有 `baseAgent.md` 一个文件；真正的配置在同级仓库根的 `config/`（`config/model.json`、`config/system.yaml`、`config/model.example.json`）。

文档 L9 自己写着"目录按当前代码核对"，L105 却把一份外项目的设计说明放进"读分篇时先按这个目录找文件"的目录树里，两处对不上。维持。

### 第 5 条：`L96-L97` 漏了会话库里的三张论文表——维持（性质是漏写，不是写错）

证据：`src/repositories/sessions/sqlite.py:451/464/476/487` 建 `session / session_event / session_message / session_artifact` 四张；同一个 `_initialize_schema`（`sqlite.py:445` 起）里还有 `:505 paper`、`:515 paper_alias`、`:521 paper_session` 三张，用于长期记忆的论文身份、别名与会话引用。

文档那一行是对"这个目录存什么"的列举，漏掉三张表会让新人找不到长期记忆的身份表在哪（`src/services/paper_memory.py:5` 的 docstring 也写"现在身份和别名改记在会话库同一个 SQLite 里"）。维持，但定性为**列举不全**，不是**说错**。

### 第 8 条：`L98` 只写"会话、工作区、精读报告"——维持（同为漏写）

证据：`ls src/models/` → `sessions.py`、`workspace.py`、`deep_read.py`、`read_models.py`、`__init__.py`。`read_models.py` 里有 `ReadNote`（`read_models.py:12`）和相关性等级与打分（`MATCH_LEVEL_FIELDS`、`calculate_relevance_score`、`normalize_match_levels`），`src/models/workspace.py:11` 真的 import 了它。

维持。补一句：诊断建议的替换文案只提了"摘要相关性评分"，漏了同文件的 `ReadNote`，略欠完整。

### 第 6 条：`L120`「三类别名」——半维持

**维持的一半**：确实不止三类。`src/paper_retrieval/identity.py:53 def paper_aliases()` 收的是五组——
`paper_key`（主编号，`identity.py:36`，它本身是 DOI / arXiv / 标题之一）、`_doi_key`（`:237`）、`_arxiv_key_from_ids`（`:249`）、`_title_key`（`:266`）、`_raw_id_values`（`:277`，工作区里存的原始 `paperId`、本地上传编号，外加去掉版本号的变体和 `arxiv:` 前缀变体）。
`papers_match`（`identity.py:91`）自己的注释写的是"硬编号（DOI、arXiv、原始 paperId）"——第四类"原始编号"是文档没写的。

**推翻的一半**：诊断说"别名索引已经不在文件里了"，但**文档从没说过别名存在文件里**。文档那句"报告存成 `deep_read.json`"讲的是精读报告文件，不是别名索引。这半条是自己立了个靶子再打。

---

## 二、推翻

### 第 2 条：`L200`「累加 token 用量」——推翻

诊断原文：工具卡上的 token 用量是**覆盖**不是累加（引 `workflow.py:522`）；卡片显示的是**最近一次**上报的用量，不是历次之和。

反例：

- `src/agents/reading/deep_read.py:228-229` 立 `total_input/total_output`，在 map 各片段（`:632-633`）、reduce（`:684-685`）、公式转写（`:289-290`）、repair 重试（`:707-708`、`:823-824`）上**逐次 `+=`**；最终只在 `:457-463` 上报一次，上报值就是**这份精读全部模型调用之和**。
- `src/agents/reading/paper_qa.py:219` 的注释原文就是"token 用量累加器：主调用 + 取文轮次 + 可能的 repair 重试都累加到这里"；`:237-238` 累加，`:348-349` 上报总和。
- 综述同理：`src/agents/review/pipeline.py:212 class _UsageCollector`，`collect()`（`:225-231`）把本节点每次调用累加，`_usage_update()`（`:234-238`）再并进全局累计。

诊断引的 `workflow.py:520-527` 那段注释说的是**事件更新语义**——"只有模型回调明确提供用量时才覆盖卡片数字，普通进度更新不会把已有用量清零"。它管的是"没带用量的普通进度更新不要清零"，**不是**"上报值不是累计值"。把事件层的"覆盖更新"读成取值层的"不累加"，就是把适用范围放大了。

所以文档"每个工具……累加 token 用量"说的是取值层，与源码一致（项目自己的面试话术 `简历-Paper-Agent项目经历.md:159` 也是这么写的：子 Agent "把整段流程用量累加后挂到所属工具卡片"）。

**唯一可挑的是措辞歧义**：若把"累加"读成"同一张卡片被多次上报、数字往上叠"，那是错的——那是覆盖更新（`front/src/lib/session-stream-aggregator.ts:477-485` 是 `=` 赋值）。建议文档改成"把自己的 token 用量（本工具内部各次调用之和）写到工具卡上"更稳。但**这不构成"与源码不符"**，诊断的"覆盖不是累加"作为指控不成立。

推翻。

### 第 4 条：`L34`/`L127` 的 `research_agent`——推翻（作为"与源码不符"不成立）

诊断原文自己承认"**代码这一侧是对得上的**"，只是"配置这一侧会误导新人"——既然承认代码对得上，就不该放进"与源码不符的地方"这一节。

代码侧复核：`src/agents/research/agent.py:51 RESEARCH_LLM_PROFILE = "research_agent"`；`agent.py:50` 的注释原文就是"用户没有单独配置这个档位时会自动回退到 default_agent"；`src/services/chat_runtime.py:209-211`；回退逻辑 `src/llm/config.py:165-171`；`src/agents/common/base.py:15-17` 还专门注明"主对话使用的 research_agent 不在这里"（`SUPPORTED_LLM_PROFILES = frozenset({"default_agent"})`）。**文档 L127 与这些注释逐句吻合**。

而且它不是"找不到就报错的必配项"，是可选档位，缺省走回退——文档写"还可以选""没配置时自动退回"已经把这一点说准了，文档也没声称它已写在配置文件里。

推翻（作为"建议补一句默认配置里没有"的完备性意见可以保留，但不属于与源码不符）。

### 第 7 条：`L70`「现在只给摘要阅读用」——推翻

诊断原文：措辞不准，应改成"只给摘要相关性评价用"。

复核：`BaseAgent` / `AgentSpec` 全仓只有一个使用者——`src/agents/reading/relevance.py:17` 导入、`:50 class ReadAgent(BaseAgent)`。这个 Agent 干的就是"读标题和摘要，给出与主题的三维匹配程度"（`relevance.py:51-53` 的 docstring、`:57-63` 的 `spec.description`）。**文档说"只给摘要阅读用"与源码描述一致**，"摘要阅读"不是错误说法，只是比"摘要相关性评价"粗一点。诊断自己也把它归为"措辞不准"而非事实错误，放进"与源码不符"这一节不合适。精读不走这套基类这一点，文档的说法也不冲突。

推翻（属措辞偏好）。

---

## 三、新发现（诊断漏掉的、我与源码核对后发现的问题）

1. **诊断自身的行号出处错位（第 1 条）**：诊断写"`src/agents/review/outline.py:810 def _extract_json_object()`（`outline.py:178/226/341` 调用它）"。实际 `810` 是 **writing.py** 的定义行，`178/226/341` 也是 **writing.py** 的调用行；outline.py 的定义在 **`outline.py:165`**、唯一调用在 **`outline.py:77`**。结论（两边都手写解析）对，但两个文件的行号被互换了，改写者照它去核对会找不到。

2. **文档 L25 的工具卡状态标签与源码/前端对不上**：文档写"它有「准备调用 → 正在执行 → 完成」几种状态"。前端实际标签（`front/src/components/session/RuntimeEventTree.vue:61-80` 的 `statusLabel`）是 `pending 等待中 / running 处理中 / completed 完成 / failed 失败 / cancelled 已停止 / cancel_requested 正在停止 / skipped`，没有"准备调用""正在执行"这两个词（`ToolCallTrace.vue:7` 的注释只说"状态三态"，不是这三个词）。这一条诊断完全没提。

3. **`config/model.json` 里的 `Read_Agent` 是死配置**：`config/model.json:35` 有 `Read_Agent` 档位，而这一项**全仓无任何代码引用**（`grep -rn "Read_Agent" src/ front/src/` 只命中它自己；`src/agents/reading/relevance.py:57-63` 的 spec 用的是 `llm_profile="default_agent"`）。要提醒"照文档去配置文件里找会找不到"，这条比 `research_agent` 更值得写，诊断漏了。

4. **文档 L126 与 L220 是同一件事说了两遍**（"摘要降级的报告不写进长期记忆"）。不属于与源码不符，但按诊断的语言减法建议，这两处可以合并。

以下两条是我核过、**文档没错**、但属于"容易讲错、改写时别改反"的边界：

5. **L236"小节内部改成普通循环"是对的，别被旧红线带跑**：全仓现在只有一处 `StateGraph`（`src/agents/review/pipeline.py:449`），`src/agents/review/writing.py` 的单节写作确实是 `while True` 普通循环（`writing.py:116`）。注意 `简历-Paper-Agent项目经历.md:214` 里"writingAgent 的单节小循环是 StateGraph"这条已经过期，不要拿它去改文档。
6. **L119"读取端对旧文件保持宽容"与源码注释一致**：`src/models/workspace.py:19-22` 原文是"读取端对旧版本数据保持宽容（缺失字段给默认值、未知字段直接忽略）"。诊断把它算作"生造词"（第二节 A 类），但它其实是照抄代码注释，不算生造。

---

## 四、独立抽查（不依赖诊断结论，我自己核过、与源码一致的 15 处）

| 文档说法 | 源码位置 | 结论 |
| --- | --- | --- |
| 主 Agent 注册 11 个工具 | `src/agents/research/tools.py:523-533` 逐条 `registry.register`；`ToolSpec` 定义 11 个（`tools.py:201/269/300/325/337/363/383/395/418/434/458`），名字与文档 L171-174 逐一对上 | 一致 |
| `MAX_TOOL_ROUNDS = 20`、`TOOL_PARALLEL_LIMIT = 3` | `src/agents/research/agent.py:54`、`:57` | 一致 |
| `WORKSPACE_SCHEMA_VERSION = 4` | `src/models/workspace.py:24` | 一致 |
| 工作区路径 `data/sessions/{会话编号}/workspace/papers.json` | `src/models/workspace.py:269-278`、`:231` | 一致 |
| 检查点库在会话目录下 `checkpoints.db` | `src/agents/review/pipeline.py:360-368` | 一致 |
| 综述外层 8 个节点 | `pipeline.py:450-457` 的 8 个 `add_node` | 一致 |
| 追问最多 4 轮 | `src/agents/reading/paper_qa.py:74 QA_MAX_TOOL_ROUNDS = 4` | 一致 |
| 阶段 A 旧终答只留前 400 字 | `src/agents/research/agent.py:814-820`（阶段 B 摘要 `COMPRESSED_SUMMARY_MAX_CHARS = 400` 在 `agent.py:72`，最近 6 轮保护 `KEEP_RECENT_TURNS_LITE = 6` 在 `agent.py:68`） | 一致 |
| 第二个请求收到 409 | `src/services/session_runs.py:207` | 一致 |
| `turn_end` 必须最后发 | `src/services/session_runs.py:341-350` | 一致 |
| 检索三个来源 arxiv / openalex / semantic_scholar | `src/paper_retrieval/connectors/{arxiv,openalex,semantic_scholar}.py` 的 `source_name` | 一致 |
| 摘要降级不入长期记忆（`source=abstract_fallback`） | `src/models/deep_read.py:21`；`src/services/paper_memory.py:223/327/559/594/863` 一律要求 `DEEP_READ_SOURCE_FULLTEXT` | 一致 |
| 对话历史"只增不改" | `src/repositories/sessions/sqlite.py` 无 `UPDATE/DELETE session_message`，只有 `append_message`（`:225`） | 一致 |
| 进程崩溃后的启动自愈 | `src/api/app.py:79-88` 的 `reset_stale_runs` | 一致 |
| 续写编号 `thread_id` 由回合编号 + 卡片编号拼成 | `src/agents/review/pipeline.py:349-359`、`:276` 的注释"形如回合编号:卡片编号" | 一致 |

---

## 一句话结论

**这篇文档与源码的符合程度很高**：诊断第三节的 8 条指控里只有 4 条站得住（其中 2 条还只是"列举不全"），3 条是把源码语义放大后的误判——尤其"累加 token 用量"那条，工具卡上报的确实是本工具各次调用的累计值；另有 2 处诊断自己引错了行号或立错了靶子。我另外只多查出 2 处文档与源码的小出入（工具卡状态标签用词、`Read_Agent` 死配置未提），真正必须改的硬伤只有 `src/config/baseAgent.md` 的说明和"统一解析入口"的表述这两处。
