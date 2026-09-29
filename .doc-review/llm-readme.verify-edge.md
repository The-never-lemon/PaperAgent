# LLM-README 诊断的对抗式复核（视角：找反例和边界）

复核对象：`.doc-review/llm-readme.diag.md` 第三节「与源码不符的地方」全部 6 条。
被诊断的文档：`doc/LLM-README.md`。
对照源码：`src/llm/` 下 6 个文件，另抽查 `src/services/chat_runtime.py`、`.gitignore`、`config/`。

复核口径：每条指控默认按「诊断错」处理，只有我在源码里亲眼看到证据、且确认文档那句话在任何读法下都站不住，才判「维持原判」。

结论概览：6 条里 4 条维持，2 条推翻。

---

## 一、维持原判（4 条）

### 1. L158「`base.py` 只声明接口，不写实现」

**我尝试的反驳**：这句话后面紧跟着「`openai_compat.py` 和 `anthropic.py` 是它的两个子类，各自写 `chat()` 和 `chat_stream()` 的实现」，可以读成「base.py 只声明 chat/chat_stream 这两个接口」。如果把「它」限定成「这两个方法」，文档就没说错。

**为什么仍然维持**：句子的主语是「`base.py`」，字面读法是整个文件。而 `base.py` 共 686 行（`wc -l` 实测），除了 `chat()` / `chat_stream()` 两个只有 `raise NotImplementedError` 的抽象方法（`src/llm/base.py:313`、`src/llm/base.py:333`），其余全是实现：

- `_run_llm_call`（`src/llm/base.py:393`）——重试、限流、耗时日志
- `_retry_delay`（`src/llm/base.py:419`）——退避算法
- `_settings`（`src/llm/base.py:468`）、`_error_response`（`src/llm/base.py:494`）、`_error_fields`（`src/llm/base.py:520`）、`_should_retry`（`src/llm/base.py:563`）
- `merge_body`（`src/llm/base.py:580`）、`_http_error_kind`（`src/llm/base.py:631`）、`vision_image_block`（`src/llm/base.py:672`）
- `aclose`（`src/llm/base.py:351`）、`_run_async_compat`（`src/llm/base.py:367`）

而且文档自己在同一节的前一张表里就写了第 4 层是「接口和重试」（`doc/LLM-README.md:156`），后面又写「所有 Provider 共用这里的接口、重试和公共函数」（`doc/LLM-README.md:410`）。所以这是文档内部打架，不只是措辞问题。诊断给出的改法（把范围收到 `chat()` / `chat_stream()` 上）是对的。

小修正：诊断说「680 多行」——实测 686 行，说法成立。

### 2. L752「`include_stream_usage` 是快照字段」

**我尝试的反驳**：会不会是 `ProviderSnapshot` 有别的构造路径，或者某个适配器把 `include_stream_usage` 塞进了快照？

**为什么仍然维持**：`ProviderSnapshot` 的字段定义只有四个（`src/llm/factory.py:29-32`：`provider`、`model`、`context_window_tokens`、`signature`），唯一一处构造也是原样传这四个（`src/llm/factory.py:81`）。全仓 grep `include_stream_usage` 共 12 处命中，没有一处落在快照上：

- 定义与解析：`src/llm/config.py:31`、`src/llm/config.py:199`、`src/llm/config.py:221`（都属于 `ProviderConfig`）
- 透传与默认值：`src/llm/factory.py:78`、`src/llm/base.py:243`、`src/llm/base.py:274`（默认 True）
- 真正消费：只有 `src/llm/openai_compat.py:239`（`src/llm/openai_compat.py:241` 发 `stream_options`）。`src/llm/anthropic.py` 全文不含这个标识符。

所以文档让读者去 `ProviderSnapshot` 上找这个字段，是找不到的；诊断说的「只有 OpenAI 兼容适配器会读它」也成立。这条是全文最伤人的一类错误，维持。

### 3. L371 示例配置漏字段

**我尝试的反驳**：文档写的是「这里去掉了密钥」，只要说明里交代过删减，是不是就不算错？

**为什么仍然维持（但降级为「轻微」）**：`config/model.example.json` 共 22 行，实际字段是：`providers.deepseek` 下有 `backend`、`api_key`（`config/model.example.json:5`）、`api_base`、`extra_headers`（`config/model.example.json:7`）、`extra_body`（`config/model.example.json:8`）；`agents.default_agent` 下有 `label`（`config/model.example.json:13`）、`description`（`config/model.example.json:14`）。文档贴出的示例把这 5 个键全删了，说明里只交代了「去掉密钥」。所以说明与实际删减对不上，读者会以为这个文件只剩那么几个字段。

但要指出诊断的定性偏重：这不是行为层面的错误，示例里的每一项取值都与真实文件一致（`backend: anthropic_compat`、`api_base: https://api.deepseek.com/anthropic`、`temperature: 0.3`、`max_tokens: 32768`、`reasoning_effort: none` 全部逐字核对通过），只是删减范围没说清。按「与源码不符」算成立，严重度最低。

### 4. L514「四个旧名字都还有调用方」

**我尝试的反驳**：会不会有动态调用（`getattr`、字符串拼方法名）或者测试之外的入口，让 `async_chat` / `async_chat_stream` 也能算「有调用方」？

**为什么仍然维持**：全仓 grep（排除 `.venv`）四个名字的命中如下：

- `chat_with_retry`：`src/agents/reading/relevance.py:94`、`src/agents/reading/relevance.py:153`、`test/test_model_connectivity.py:84` —— 有调用方
- `chat_stream_with_retry`：`test/test_model_connectivity.py:113` —— 有调用方
- `async_chat`、`async_chat_stream`：只有定义本身（`src/llm/base.py:336`、`src/llm/base.py:341`），没有任何调用点，也没有 `getattr(..., "async_chat")` 这类动态调用

所以「它们是兼容入口，目前还有调用方」对其中两个名字不成立。诊断给出的改法（点名哪两个有、哪两个没有）是对的。维持。

---

## 二、推翻（2 条）

### 5. L176「直接使用方枚举漏了 `eval/run_retrieval_eval.py`」

**诊断原文**：文档这段「读起来像一个完整枚举」，至少漏了 `eval/run_retrieval_eval.py`（证据：`eval/run_retrieval_eval.py:31` 的 `from src.llm.config import SystemConfig`），并建议改成「只用 `make_provider()` 装配的地方有这几处：…… `eval/run_retrieval_eval.py` ……」。

**为什么推翻**：

1. **文档列的那份名单是完整的，不是漏的。** 全仓 `make_provider` 的调用点只有：`eval/lib/judge.py:93`、`scripts/probe_parallel_tool_calls.py:56/93/123`、`src/agents/reading/relevance.py:343`、`src/services/chat_runtime.py:216`、`src/services/settings.py:216/314`、`test/test_live_model_config.py:27`、`test/test_llm_adapters.py`（5 处）、`test/test_model_connectivity.py:69`。去掉文档在 L170 已单独讲过的 `chat_runtime`，剩下的正好是文档列的「settings.py + relevance.py + 两个评测脚本 + 三个测试文件」，一个不多一个不少。
2. **诊断点名的那个文件并不调用 `make_provider`。** `eval/run_retrieval_eval.py` 只有 `src/llm/config.py` 的 `SystemConfig` 一个 import（`eval/run_retrieval_eval.py:31`），它调的是 `eval/lib/judge.py` 里的 `build_judge_deps` / `run_relevance_judge`（`eval/run_retrieval_eval.py:35`）。诊断自己给的替换文案把它写进「只用 `make_provider()` 装配的地方」，与前一句自相矛盾。
3. **按诊断的标准数下去，这个文件根本不特殊。** 只 import `src/llm.config` 的模块还有 `src/paper_retrieval/service.py:11`、`src/repositories/settings/json.py:8`、`src/services/paper_memory.py:24`、`src/services/workspace_upload.py:28`、`src/agents/research/tools.py:27`、`src/agents/review/pipeline.py:41` 等。单独补一个 `run_retrieval_eval.py` 没有边界依据。
4. **文档用的词是「还有几个」，不是「全部」。** 「这一层还有几个直接使用方」（`doc/LLM-README.md:176`）是补充口径，不是完备枚举声明。

**残留的有效部分（不计入本条的成立）**：如果读者真的把它读成完备清单，那么直接引用本层公共函数的模块确实没被列出来——`src/agents/research/agent.py:25`（`StreamCallbacks`、`attach_reasoning`、`normalize_token_usage`、`yield_to_event_loop`）、`src/utils/fulltext/figures.py:26`（`vision_image_block`）、`src/agents/common/base.py:8`、`src/agents/reading/deep_read.py:23`、`src/agents/review/analyse.py:14` 等十余处。但这是「清单要不要扩容」的建议，不是事实错误；诊断把它归进「与源码不符」并给出一个自相矛盾的改法，属于把适用范围放大了。

### 6. L26「OpenAI、Anthropic 各算一种 Provider」

**诊断原文**：`registry.py` 的 `PROVIDERS` 有 5 条，其中 `openai_compat`、`anthropic_compat` 是协议名不是厂家名，`opencode_go` 是第三方网关；开头这句和 L207-213 的表格对不上。

**为什么推翻**：

1. **这句话说的是适配层，不是表项。** 原句是「**Provider**：某一类模型接口的适配层。OpenAI、Anthropic 各算一种。」（`doc/LLM-README.md:26`）。按「适配层」这个定义去数，实际就是两种：`factory.py` 的分派分支只有 `openai_compat` 和 `anthropic` 两个（`src/llm/factory.py:87-93`），协议实现类也只有 `OpenAICompatProvider`、`AnthropicProvider` 两个（`src/llm/factory.py:8`、`src/llm/factory.py:11`）。这句没有否定 5 条表项。
2. **文档下一节就把 5 条列全了，读者不会被带偏。** `doc/LLM-README.md:182` 明写「一共 5 条」，`doc/LLM-README.md:207-213` 的表格逐条列出 5 条。开场给两个例子、正文给完整表，是常见的由浅入深写法。
3. **「OpenAI」这个词在源码里也不是错的。** `PROVIDERS` 里确实有一条键就叫 `openai`（`src/llm/registry.py:37`），`OpenAICompatProvider` 也确实对应 OpenAI 兼容协议。把它读成「厂商名混进协议名」是过度解读。
4. **顺带纠一个诊断的算术**：诊断说「`openai_compat` 和 `anthropic_compat` 是协议名不是厂家名」——`PROVIDERS` 的键里确实没有厂家名之说，但 `openai` 这一条的 `name`、`backend` 都是真名实姓，不是文档编的。

**残留的有效部分**：文档「Provider」一词确实有两种用法（L26 指适配层，L199 指表项），补一句英文或加一句「表里 5 条，落在两个适配器上」会更清楚。但这是措辞建议，不是「与源码不符」。

---

## 三、新发现（诊断漏掉的）

### 1. `merge_body()` 全仓没有调用方，文档却把它和「有调用方」的公共设施并列

文档在 `base.py` 一节把它列为公共设施：「`merge_body()`：递归合并请求体。避免用户的 `extra_body` 直接盖掉适配器写好的整块嵌套结构。」（`doc/LLM-README.md:424`）

源码实际：`merge_body` 定义在 `src/llm/base.py:580`，全仓只有它自己递归调用自己（`src/llm/base.py:598`），没有任何外部调用点。两个适配器都是把 `extra_body` 原样塞给 SDK（`src/llm/openai_compat.py:242-244`），根本没有走合并逻辑。

同一个文档对「没有调用方」的东西是会给读者点明的——`match_provider()` 写了「这个函数目前没有调用方」（`doc/LLM-README.md:271`），`signature` 写了「目前没有消费方」（`doc/LLM-README.md:581`）。`merge_body` 是唯一没被点明的一个，而且它旁边并列的 `attach_reasoning`、`normalize_token_usage`、`vision_image_block` 都确实有仓外调用方。建议补一句「目前没有调用方」。

### 2. L119 把 `connection` 类别的归属写错了

文档原文：「错误类别由 `_http_error_kind()` 归一。429 归 `rate_limit`。401 和 403 归 `auth`。500 到 599 归 `server_error`。剩下的归 `invalid_request`。没有状态码的归 `connection`。」（`doc/LLM-README.md:119`）

源码实际：`_http_error_kind()`（`src/llm/base.py:631`）只有 4 个返回分支——`rate_limit`（`src/llm/base.py:643`）、`auth`（`src/llm/base.py:645`）、`server_error`（`src/llm/base.py:647`）、`invalid_request`（`src/llm/base.py:649`），它**永远不会返回 `connection`**。`connection` 是在没有 HTTP 状态码的分支里由 `_error_fields()` 直接写死的（`src/llm/base.py:552-555`）。

也就是说，那句话把五个类别的来源都记在了 `_http_error_kind()` 头上，实际是「四个类别看状态码，第五个类别看有没有状态码，判定位置也不在同一个函数里」。

### 3. 文档没区分两个同名的 `backend` 字段，而 L203 的字面读法会被文档自己的示例配置打脸

文档在 registry 一节写：「`backend` 决定用哪个适配器，只有两种取值：`openai_compat` 和 `anthropic`。」（`doc/LLM-README.md:203`）

源码实际有两个同名但取值域不同的 `backend`：

- `ProviderSpec.backend`（`src/llm/registry.py:27`）：只有 `openai_compat` / `anthropic` 两个值，文档这句说的是它，没错。
- `ProviderConfig.backend`（`src/llm/config.py:16`，缺省 `openai_compat`）：写配置文件的那个字段，取值是 `PROVIDERS` 的键，可以是 5 条中任意一条。解析时会经过 `match_provider_backend()`（`src/llm/config.py:179`），随后又被改写成 `spec.name`（`src/llm/config.py:190`）——所以配置里写 `openai` / `anthropic_compat` / `opencode_go` 都能走通，只是最终落到两个适配器之一。

文档自己的示例配置写的就是 `"backend": "anthropic_compat"`（`doc/LLM-README.md:377`）。读者若按 L203 的字面读法认为配置里的 `backend` 只能填两个值，会在复制示例时卡住。建议在 `ProviderConfig` 那张表（`doc/LLM-README.md:281`）旁边补一句「这一层的 `backend` 填的是 `PROVIDERS` 的键，不止两个值；真正只有两个取值的是规格里的 `backend`」。

### 4. （轻微）「档位和默认值合起来」的执行位置

文档决策 7 写：「`resolve_provider_config()` 先把档位和默认值合起来，再返回补齐后的副本。」（`doc/LLM-README.md:133`）

源码实际：档位字段与 `system.yaml` 默认值的合并发生在解析阶段 `_agent_from_dict()`（`src/llm/config.py:237-245`，`raw.get("temperature", defaults.temperature)` 那一套）。`resolve_provider_config()` 干的是另一件事：查 Provider 表、补环境变量、补 `default_api_base`、合并必带请求头（`src/llm/config.py:173-200`）。两处都在「补默认值」，但不是同一个函数。

---

## 四、我另外抽查并通过的代码事实（诊断说「核对无误」的部分，独立复验）

诊断说这一节「14 处行号引用全部逐行核对通过」，我独立复验了全部 14 处代码块，逐行一致，没有一处漂移：

| 文档位置 | 引用 | 实测 |
| --- | --- | --- |
| `doc/LLM-README.md:188` | `26:33:src/llm/registry.py` | 一致（`ProviderSpec` 8 个字段） |
| `doc/LLM-README.md:221` | `66:80:src/llm/registry.py` | 一致（`opencode_go` 条目） |
| `doc/LLM-README.md:241` | `84:101:src/llm/registry.py` | 一致（`match_provider_backend`） |
| `doc/LLM-README.md:307` | `120:136:src/llm/config.py` | 一致（`AgentConfig`） |
| `doc/LLM-README.md:329` | `164:171:src/llm/config.py` | 一致（`resolve_agent`） |
| `doc/LLM-README.md:351` | `186:194:src/llm/config.py` | 一致（请求头合并） |
| `doc/LLM-README.md:431` | `393:417:src/llm/base.py` | 一致（`_run_llm_call`） |
| `doc/LLM-README.md:467` | `437:448:src/llm/base.py` | 一致（退避算法） |
| `doc/LLM-README.md:486` | `104:114:src/llm/base.py` | 一致（`ok` 属性） |
| `doc/LLM-README.md:530` | `42:65:src/llm/factory.py` | 一致（`make_provider`） |
| `doc/LLM-README.md:563` | `29:39:src/llm/factory.py` | 一致（快照字段） |
| `doc/LLM-README.md:606` | `223:245:src/llm/openai_compat.py` | 一致（请求参数） |
| `doc/LLM-README.md:668` | `378:393:src/llm/anthropic.py` | 一致（思考块补齐） |
| `doc/LLM-README.md:689` | `335:357:src/llm/anthropic.py` | 一致（工具结果合并） |

另外独立复验通过的数值与枚举（诊断列为「正确」，我逐条重查）：

- `PROVIDERS` 5 条的名称 / `backend` / 默认地址 / 环境变量 / 关键字 / 前缀裁剪，与文档 L207-213 表逐格一致（`src/llm/registry.py:36-81`）
- `openai` 的 `supports_max_completion_tokens=True` 且不裁前缀（`src/llm/registry.py:42-43`）；`anthropic` 与 `anthropic_compat` 都是 `strip_model_prefix=True` 且共用 `anthropic` 后端（`src/llm/registry.py:51-65`）
- `ProviderConfig` 恰好 10 个字段（`src/llm/config.py:16-31`）；`AgentConfig` 恰好 8 个字段（`src/llm/config.py:124-132`）
- `LLMDefaults` 四项 0.7 / 4000 / `"none"` / 1048576（`src/llm/config.py:38-45`）
- `system.yaml` 四项全写 `null`（`config/system.yaml:3-9`），解析时 `llm.get(key, 默认值)` 拿到的是 `None` 而不是类里的 0.7（`src/llm/config.py:98-101`），再传到档位（`src/llm/config.py:243-245`），因此出厂状态确实不发 temperature——文档 L303 的说法成立
- `default_agent` 缺失即抛错（`src/llm/config.py:231-232`）；`provider` 缺省 `auto`、`model_name` 认 `modelName`/`model`（`src/llm/config.py:239-240`）
- `max_retries` `max(1, … or 3)`、`max_concurrency` `max(1, … or 2)`、`timeout_s` `max(1, …)`、`include_stream_usage` 缺省 True、`api_base` 去尾斜杠（`src/llm/base.py:266-275`）
- 退避数值：500 毫秒起步、`2**(attempt-1)` 翻倍、封顶 32000、抖动 `uniform(0, 25%)`（`src/llm/base.py:437-441`）、服务端上限 `_RETRY_AFTER_CAP_SECONDS = 60.0`（`src/llm/base.py:25`）
- 错误类别映射四个分支（`src/llm/base.py:643-649`）；`_should_retry` 优先听 `error_should_retry`（`src/llm/base.py:575-577`）
- `max_retries` 是总次数不是重试次数（`src/llm/base.py:399` 的 `range(self.max_retries)`）；并发用 `asyncio.BoundedSemaphore`（`src/llm/base.py:276`）
- `match_provider()` 全仓只有定义、无调用方（`src/llm/registry.py:104`）
- 温度黑名单 5 个前缀（`src/llm/anthropic.py:541-547`）；`max_tokens` 兜底 4096（`src/llm/anthropic.py:232`）；自适应思考 + `output_config.effort`（`src/llm/anthropic.py:244-246`）；`_normalize_effort` 的 `none/off/disabled` 关闭、未知值给 `high`（`src/llm/anthropic.py:527-532`）
- 思考块只留 `type`/`thinking`/`signature`（`src/llm/anthropic.py:470-474`）；缺结果补的占位内容是 `{"status": "interrupted", "reason": "用户中途停止了本次运行，这个工具没有执行"}`（`src/llm/anthropic.py:14`），与文档 L687 逐字一致
- `system` 多条用换行拼成一段顶层字段（`src/llm/anthropic.py:361-363`、`src/llm/anthropic.py:398`）
- 消息清洗白名单 6 个键、非法 role 抛 `ValueError`、空思考键删除、连续同角色合并、末尾孤立 assistant 丢弃（`src/llm/openai_compat.py:370-411`）
- 推理模型前缀 `o1`/`o3`/`o4`/`gpt-5`（`src/llm/openai_compat.py:427`）；`list_models` 返回 `id`/`label`/`owned_by`/`context_window`（`src/llm/openai_compat.py:180-187`）
- `__init__.py` 导出 11 个名字、来自 3 个模块（`src/llm/__init__.py:1-16`），与文档 L160-164 的分组一致
- 三份配置文件的定位（`doc/LLM-README.md:367-369`）：「`config/model.json` 不入库」属实——`.gitignore:85` 就是 `model.json`，「带密钥」也与 `config/model.example.json:5` 留空 `api_key`、真实文件另填的写法一致
- 主 Agent 装配一次、传给子 Agent、运行结束统一 `aclose()`（`src/services/chat_runtime.py:96`、`src/services/chat_runtime.py:114`、`src/services/chat_runtime.py:158`）；档位名 `research_agent`（`src/agents/research/agent.py:51`）；窗口 × 0.8 的预算比例（`src/agents/research/agent.py:65`）；独立入口的出厂档位 `default_agent`（`src/agents/reading/relevance.py:61`、`src/agents/reading/relevance.py:342`）

---

## 五、一句话结论

这篇文档与源码的符合程度很高：14 处行号引用无一漂移、三张对照表和各处默认值/数量都对得上，真正会误导读者的硬错误只有两条（L158 对 `base.py` 的范围说反、L752 把配置字段说成快照字段），其余是删减说明不精确一类的小瑕疵。
