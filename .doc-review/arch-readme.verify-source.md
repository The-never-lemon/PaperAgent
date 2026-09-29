# doc/arch/README.md 诊断的对抗式复核（视角：逐行核对源码）

复核对象：`.doc-review/arch-readme.diag.md` 第三节「与源码不符的地方」（共 8 条）
被核对文档：`doc/arch/README.md`
核对方式：每条回到源码查证，默认诊断是错的；另外独立抽查文档里的行号引用与标识符。

结论先说：**8 条里 5 条维持原判、1 条结论成立但证据（行号）写错、2 条推翻。**
本节诊断的**结论方向基本可靠，但引用的行号有系统性串行错误**（把 `writing.py` 的行号安到了 `outline.py` 头上）。

---

## 一、维持原判（5 条 + 1 条证据有误）

### 第 2 条 · 「累加 token 用量」踩红线 —— 维持原判

条目原文：「`L200` 累加 token 用量」；源码实际是覆盖，不是累加。

证据：

- `src/runtime/workflow.py:522` 注释原文逐字命中：「只有模型回调明确提供用量时才覆盖卡片数字，普通进度更新不会把已有用量清零。」
- `src/runtime/workflow.py:523-525` 是 `payload["input_tokens"] = _token_count(...)`、`payload["output_tokens"] = _token_count(...)`，**赋值**而非 `+=`。整段 `_emit_runtime_event`（约 `:495-526`）里没有任何累加。卡片上显示的是最近一次上报值。

文档 `L192`「进度和 token 用量还要落到工具卡上」不含"累加"字样，诊断建议保留原样，同意。

### 第 3 条 · `L105` 的 `baseAgent.md` 不是本项目文档 —— 维持原判

证据：

- `src/config/baseAgent.md:1` 标题为「Nanobot 多厂商大模型适配架构设计文档」。
- 同文件第 6 行起列的源码模块全是 `nanobot/providers/...`、`nanobot/providers/registry.py` 等。
- `grep -c "Paper-Agent" src/config/baseAgent.md` = **0**。
- `src/config/` 目录下**只有** `baseAgent.md` 一个文件，没有任何 `.py`——它确实不是本项目的源码目录。

补充一句诊断没说清的：文档 `L105` 那句本身写的是「一份 LLM 适配层的设计说明」，并没有直接说"这是本项目的代码"。但它被放在 `## 代码放在哪` 的目录树里，且 `L9` 写了「目录按当前代码核对」，暗示它是项目文件，所以诊断的结论成立。

### 第 5 条 · 漏了会话库里的三张论文表 —— 维持原判

证据（`src/repositories/sessions/sqlite.py` 的 7 张表，行号与诊断完全一致）：

| 表 | 行号 |
| --- | --- |
| `session` | `:451` |
| `session_event` | `:464` |
| `session_message` | `:476` |
| `session_artifact` | `:487` |
| `paper` | `:505` |
| `paper_alias` | `:515` |
| `paper_session` | `:521` |

`src/services/paper_memory.py:5-6` docstring：「现在身份和别名改记在会话库同一个 SQLite 里」。诊断的 7 张表结论成立。

### 第 6 条 · 「三类别名」说少了 —— 维持原判（行号逐条命中）

`src/paper_retrieval/identity.py:53 def paper_aliases()`，逐行核对诊断给的区间，**全部命中**：

- `:62` `aliases: set[str] = set()`
- `:63-65` 主编号 `paper_key`
- `:67-69` DOI（`_doi_key`）
- `:71-73` arXiv 编号（`_arxiv_key_from_ids`）
- `:75-77` 整理过的标题（`_title_key`）
- `:79-84` 原始 id 字符串 `_raw_id_values`（`:83-85` 还补了去版本号变体，`:86-87` 补 `arxiv:` 前缀变体）

共五组、且带变体，确实多于"三类"。别名落库位置也对：`paper_memory.py:5` 说改记 SQLite（即上一条的 `paper` / `paper_alias` / `paper_session`），`:44 INDEX_FILE_NAME = "index.json"` 注明「启动时如果目录表还是空的，会从这里迁进 SQLite，之后不再当主存储」。

### 第 8 条 · 漏了 `read_models.py` —— 维持原判

证据：

- `ls src/models/` → `__init__.py`、`sessions.py`、`workspace.py`、`deep_read.py`、`read_models.py`，共 5 个文件。
- `src/models/read_models.py` 确有 `MATCH_LEVEL_FIELDS`（`:31`）、`normalize_match_levels`（`:48`）、`calculate_relevance_score`（`:60`）。
- `src/models/workspace.py:11` 确实 import 了这三个名字。

### 第 1 条 · 「统一解析入口」踩红线 —— **结论维持，但证据（行号）写错了**

结论部分成立，我复核后无法推翻：

- `src/agents/review/outline.py` 自带 `def _extract_json_object`，`:165` 定义、`:185` `json.loads`、`:77` 调用。
- `src/agents/review/writing.py` 自带第二份，`:810` 定义、`:825` `json.loads`、`:178 / :226 / :341` 调用。
- `src/utils/llm_json.py:5` docstring：「都统一调用这里的 parse_llm_json，不允许各自手写解析和重试逻辑」——这句确实与代码不符。
- `src/agents/review/analyse.py:283` 调用 `parse_llm_json(text, fallback={})`，**没有传 `repair`**；外层重试 `_chat_json_with_retry` 在 `:112`，是它自建的一层。

**但诊断给的行号是错的：**

- 原文「`src/agents/review/outline.py:810 def _extract_json_object()`（`outline.py:178/226/341` 调用它）」——`outline.py` **总共只有 296 行**（`wc -l` = 296），根本不存在第 810 行。`:810`、`:178`、`:226`、`:341` 这四个行号**全部属于 `writing.py`**。诊断把两个文件的证据串在了一起。
- 原文「`deep_read.py:712`、`:827`（map / reduce）」——`:827` 不在 reduce 里。`_run_reduce` 是 `:665-721`，`:712` 在其中；`:827` 属于 `_run_abstract_fallback`（`:778-834`），是**摘要降级路径**，不是 reduce。真正的 map 阶段 `_map_chunks`（`:569-657`）压根**不调用** `parse_llm_json`——它只取模型的纯文本笔记（`:622` `chat(...)`、`:645` `response.content`）。所以"map / reduce"这个括注两个都错。

维持原判的一条理由要改写：不是因为 `outline.py:810` 存在，而是因为 `outline.py:165` 存在（诊断引错了文件）。

---

## 二、推翻（2 条）

### 第 4 条 · 「`L34`/`L127` 说了 `research_agent`，但配置文件里没有」—— **推翻**

诊断的事实基础是真的：`config/model.json` 的 `agents` 键下确实**没有** `research_agent`（`python -c` 读出键为 `['default_agent', 'Read_Agent']`）。代码侧也对：`src/agents/research/agent.py:51 RESEARCH_LLM_PROFILE = "research_agent"`；`src/services/chat_runtime.py:207-212` 的 `_load_llm_snapshot` docstring；`src/llm/config.py:170-171` 的回退注释「任何没配置或名字对不上的档位都回退到必配的 default_agent」。

**但这条不构成"文档与源码不符"**，理由是文档的原文本身就把这件事说对了：

> `L127`：「内置档位有 `default_agent`。主 Agent 还可以选 `research_agent`。**`research_agent` 没配置时自动退回 `default_agent`**。」
> `L34`：「内置 `default_agent`，主 Agent **另可选** `research_agent`」

文档从未声称 `research_agent` 存在于配置文件里；「另可选」「没配置时自动退回」两句正好描述了"配置里没有它、所以会回退"。因此诊断扣的这顶"与源码不符"的帽子戴不住。它提的补充建议（注明"默认配置里没有这一项"）是锦上添花，不是纠错。

顺带指出诊断这条**漏掉的一半**：`config/model.json` 的第二个档位叫 `Read_Agent`（`:35-40`，provider `OpenCode` / model `glm-5.3-flash`），而这是**文档全文一个字都没提的档位**——这才是这一行真正该补的内容（见第三节新发现 1）。

### 第 7 条 · 「`L70`『只给摘要阅读用』措辞不准」—— **推翻**

诊断说文档措辞不准、应改成"摘要相关性评价"。回到源码：

- `src/agents/reading/relevance.py:50 class ReadAgent(BaseAgent)`，`:51-55` docstring：「负责阅读摘要并给出论文与用户主题的三维匹配程度……这个 Agent 只管"理解论文摘要"这一件事」。
- `:57-63` `spec = AgentSpec(name="read_agent", role="read", description="根据论文标题和摘要整理阅读笔记，并给出三维匹配程度。", ...)`。

全仓 `BaseAgent` 的使用者只有这一个类（`grep -rn "BaseAgent\|AgentSpec" --include=*.py src/` 的结果里，除 `common/base.py` 定义处和 `__init__.py` 导出处外，只有 `relevance.py`）。

所以「`base.py` 的 `AgentSpec` / `BaseAgent`（现在只给摘要阅读用）」这句**与源码相符**：那个唯一的子类就是干"读摘要"这件事的。「摘要阅读」和诊断建议的"摘要相关性评价"只是详略之分，文档没有说错任何事实。诊断用来支撑的另外半句「精读走 `run_deep_read()` 函数入口，不用基类」我也核了，是对的（`deep_read.py:155`）——但这句话是**文档已经隐含表达过的**（文档从没说精读用基类），拿它来证明文档"措辞不准"是无效论证。

---

## 三、新发现（诊断漏掉的、文档与源码不符或缺失之处）

### 1. `L34` / `L127` 的档位描述漏了实际存在的 `Read_Agent`（与上一条诊断同一处，但方向相反）

- 文档 `L34`：「一组模型配置。内置 `default_agent`，主 Agent 另可选 `research_agent`」。
- 源码：`config/model.json:35-40` 的 `agents` 键下第二项就是 `Read_Agent`；`src/services/settings.py:81` 把整个 `agents` 字典作为 `agent_items` 吐给设置页（`:434` 也遍历 `config.agents`）；`src/services/settings.py:403-405` 明确支持"按名字取任意已配置档位"。也就是说 `Read_Agent` 是**出厂配置里真实存在、设置页可见、可按名解析**的档位。
- 另外 `src/agents/common/base.py:15-17`：「后端节点只能用 default_agent 这一个内置档位……`SUPPORTED_LLM_PROFILES = frozenset({"default_agent"})`」——这条"哪些档位能被 AgentSpec 声明"的约束，文档也没写。

诊断的第 4 条只盯着"配置里没有 research_agent"，反而漏了"配置里有一个文档没提的 `Read_Agent`"。

### 2. `L44` 括号里的 `ProviderSnapshot` 不是 `src/llm/base.py` 里的东西（文档没错，但极易被误读）

- 文档 `L44`：「『快照』在本套文档里指模型配置快照（`ProviderSnapshot`）」。
- 源码：`ProviderSnapshot` 定义在 **`src/llm/factory.py:16`**，由 `src/llm/__init__.py:2` 再导出。`src/llm/base.py` 里只有 `ToolCallRequest` / `GenerationSettings` / `LLMResponse` / `StreamCallbacks` / `ProviderHttpError` / `ProviderConnectionError` / `LLMProvider`，**没有** `ProviderSnapshot`。
- 因为文档索引表把 `src/llm/base.py` 列为 LLM 篇核心文件，而 `L44` 又只给了标识符不给文件，新人很容易去 `base.py` 里找一个不存在的类。建议首次出现时补成 `ProviderSnapshot`（定义在 `src/llm/factory.py`）。

注意：这条同时也说明诊断第 293-294 行的术语表（第四节 1 的表格）把 `ProviderSnapshot` 标成 `src/llm/base.py` **是诊断自己的错误**，不是文档的错误。

### 3. `L228` 的「两级闸门」与 `L192` 的用词，和项目自有的 02 篇不一致（诊断的改写建议会制造新矛盾）

- 诊断第二节 A 类建议把 `L228` 的「两级闸门」改成「两级收缩」。
- 但 `doc/arch/02-context-memory.md:75` 原文就是「**两级闸门**。阶段 A 只改字符串……」（同篇 `:426` 又叫"两级处理"，标题 `:69` 叫"零成本归档"）。代码侧 `src/agents/research/agent.py:61-62` 的注释用的是「阶段 A / 阶段 B」。
- 只改 README 的「两级闸门」会与 02 篇打架。要改就得两篇一起改（或者统一到代码注释的"阶段 A / 阶段 B"）。
- 附带：诊断第六节说「02 篇标题里就有『两级收缩』」——我 grep 过，`02-context-memory.md` **没有**"两级收缩"这四个字，它叫「超预算先做零成本归档，实在不行才调模型」（`:69`）。诊断记错了。

### 4. `L9`「Agent 代码在 `src/agents/` 下分成四个包」——核对无误（记录为已核事实）

`ls -d src/agents/*/` = `common/`、`reading/`、`research/`、`review/`，正好四个，加上 `__init__.py`。这条没问题，列在这里是为了说明我按任务要求做了独立抽查。

---

## 四、独立抽查：文档里的行号区间与标识符

**行号区间**：`grep -nE "[0-9]+:[0-9]+:" doc/arch/README.md` **零命中**——这篇 README 自身确实不含任何 `起始行:结束行:文件路径` 形式的引用，与诊断"关于行号引用"一段的说法一致，没有漂移可查。

**标识符/常量/字段名**（把文档反引号里的标识符全量抽出后逐个回源码）：全部命中，无一处找不到。

| 文档里的名字 | 源码位置 | 结论 |
| --- | --- | --- |
| `MAX_TOOL_ROUNDS = 20` | `src/agents/research/agent.py:54` | 命中（诊断说的 `:54` 对） |
| `TOOL_PARALLEL_LIMIT = 3` | `src/agents/research/agent.py:57` | 命中 |
| `WORKSPACE_SCHEMA_VERSION = 4` | `src/models/workspace.py:24` | 命中 |
| `ProviderSnapshot` | `src/llm/factory.py:16` | 命中（文档未给文件，见新发现 2） |
| `research_agent` | `src/agents/research/agent.py:51` | 命中 |
| `default_agent` | `src/llm/config.py`、`config/model.json` | 命中 |
| `deep_read.json` | `src/services/paper_memory.py:53 REPORT_FILE_NAME` | 命中 |
| `papers.json` | `src/models/workspace.py:27 WORKSPACE_FILE_NAME` | 命中 |
| `checkpoints.db` | `src/agents/review/pipeline.py:361-368` | 命中（诊断说 `:360`，实际函数体在 `:362-368`，差 1 行，不影响） |
| `source=abstract_fallback` | `src/agents/reading/deep_read.py:159` | 命中 |
| `turn_end` 必须最后发 | `src/services/session_runs.py:341-350` | 命中（`:350 if event_name == "turn_end"` 拦截早发） |
| `arxiv` / `openalex` / `semantic_scholar` | `src/paper_retrieval/service.py:429-434` | 命中 |
| `render_tool_result` | `src/agents/research/tools.py:486` | 命中 |
| `contextvars.ContextVar` | `src/agents/common/base.py`、`agent.py` 有实际使用 | 命中 |
| `asyncio.CancelledError` 唯一上抛 | `deep_read.py:174`、`paper_qa.py:131`、`pipeline.py:305`、`agent.py:1144` | 命中 |
| `XxxDeps` | `DeepReadDeps`/`PaperQaDeps`/`ReviewDeps` 三个真实 dataclass | 命名约定，成立 |
| `thread_id = "{turn_id}:{event_key}"` | `src/agents/review/pipeline.py:358` `return f"{deps.turn_id}:{deps.event_key}"` | 命中 |
| 11 个工具名（`search_papers` … `get_history`） | `src/agents/research/tools.py:523-533` 的 11 条 `registry.register(Tool(...))`；spec 在 `:200 / :268 / :299 / :324 / :336 / :362 / :382 / :394 / :417 / :433 / :457` | **数量 11、名称全对** |
| `run_deep_read` / `run_paper_qa` / `run_review` | `deep_read.py:155`、`paper_qa.py:116`、`pipeline.py:248` | 命中 |
| 外层 8 个节点 | `src/agents/review/pipeline.py:450-457` 的 8 个 `add_node` | 命中 |
| 追问最多 4 轮 | `src/agents/reading/paper_qa.py:74 QA_MAX_TOOL_ROUNDS = 4` | 命中 |
| 阶段 A「最近 6 轮不碰」 | `src/agents/research/agent.py:68 KEEP_RECENT_TURNS_LITE = 6` | 命中 |
| 阶段 A「旧终答只留前 400 字」 | `src/agents/research/agent.py:819-820` | 命中 |
| 6.2 MB → 1.95 MB | 不在源码里；出自 `简历-Paper-Agent项目经历.md:244` 的实测记录，`doc/arch/01-agent-orchestration.md:127` 同源 | 无源码可对，非"与源码不符" |

**目录树逐条**：`src/api/app.py`、`src/api/routers/{sessions,settings,workspace}.py`、`src/services/{session_runs,chat_runtime,paper_memory}.py`、`src/agents/common/{base,contracts,tool_registry,prompts,context_budget,skill_loader}.py` + `skills/`、`src/agents/research/{agent,tools}.py`、`src/agents/reading/{relevance,deep_read,paper_qa}.py`、`src/agents/review/{pipeline,analyse,outline,writing,document}.py`、`src/runtime/{workflow,resources}.py`、`src/repositories/sessions/`、`src/repositories/settings/`、`src/models/`、`src/utils/{llm_json,latex_text,readable_id,logging_utils}.py`、`src/utils/fulltext/`、`src/paper_retrieval/download.py`、`src/llm`、`front/`——**全部存在**，职责描述与文件 docstring 逐条对得上（`latex_text.py`「清洗论文元数据里的裸 LaTeX 标记」、`readable_id.py`「生成人能直接看懂创建时间的标识符」、`document.py`「综述终稿的引用整理和 Markdown 拼装」、`resources.py`「一次运行里共用的并发上限和网络客户端」等）。前端 `front/package.json:15` 是 `vue ^3.5.18`，`pyproject.toml:6` 是 `requires-python >=3.12`，`L130` 的"Vue 3 / FastAPI / Python 3.12 / uv"全对。

### 诊断自身的错误（本节顺带记录）

- 第三节第 1 条把 `writing.py` 的 `:810/:178/:226/:341` 记成了 `outline.py`（`outline.py` 只有 296 行）。
- 第三节第 1 条的"map / reduce"括注错：`:827` 属摘要降级路径。
- 第四节 1 的表格把 `ProviderSnapshot` 标到 `src/llm/base.py`，实际在 `src/llm/factory.py:16`。
- 第四节 3 说「`llm_profile`（`src/agents/common/base.py:33`）」——`:31` 才是 `llm_profile`，`:33` 是 `input_keys`。
- 第六节说「02 篇标题里就有『两级收缩』」——02 篇没有这四个字。

---

## 一句话结论

**这篇文档与源码的符合程度很高**：目录树、常量名、函数名、工具清单（11 个，数量与名称全对）、档位与数据结构描述逐条经得起查证，全篇不含一处行号区间因此也没有漂移问题；真正与源码不符的只有 `L101` 的「统一解析入口」（`outline.py:165`、`writing.py:810` 仍各留一份手写解析）和 `L200` 的「累加 token 用量」（`workflow.py:522` 注释与 `:523-525` 的赋值证明是覆盖）两处表述，外加 `L105` 把外项目文档 `baseAgent.md` 摆进了自己的目录树——诊断的 8 条指控里，2 条（第 4、7 条）不成立，第 1 条的结论成立但行号证据串了文件。
