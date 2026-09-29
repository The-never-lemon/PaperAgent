# doc/LLM-README.md 源码核对（对抗式复核）

**复核视角**：逐行核对源码，默认诊断的每一条都是错的，只有在源码里亲眼看到证据、无法推翻时才承认它成立。

**复核对象**：

- 诊断文件：`.doc-review/llm-readme.diag.md`（第三节「与源码不符的地方」，共 6 条）
- 被诊断文档：`doc/LLM-README.md`（776 行）
- 对照源码：`src/llm/base.py`（686 行）、`config.py`（364 行）、`factory.py`（120 行）、`registry.py`（141 行）、`openai_compat.py`（467 行）、`anthropic.py`（588 行）
- 旁证：`config/model.example.json`、`config/model.json`、`config/system.yaml`、`src/services/settings.py`、`src/services/chat_runtime.py`、`src/agents/reading/relevance.py`、`eval/`、`scripts/`、`test/`

**结论速览**：诊断第三节 6 条，**5 条维持、1 条推翻**；另外我独立查出 **2 处诊断漏掉的与源码不符之处**（L203、L264），另有 2 处轻微措辞不符。

---

## 一、维持原判（诊断成立，我无法推翻）

### 维持 1：L158「`base.py` 只声明接口，不写实现」

条目原文：文档 L158 说「第 4 层里，`base.py` 是两个适配器的父类。**它只声明接口，不写实现。**」

我找到的证据（全部亲自打开源码确认）：

- `base.py` 全文 **686 行**（`wc -l src/llm/base.py`），是 `src/llm/` 里最长的文件。
- 抽象方法只有两个：`base.py:284` 的 `chat()`（方法体 `raise NotImplementedError` 在 `base.py:299`）和 `base.py:301` 的 `chat_stream()`（`raise NotImplementedError` 在 `base.py:317`）。
- 其余全是**实打实的实现**：
  - 重试 + 限流 + 耗时日志：`base.py:393` `_run_llm_call()`
  - 退避计算：`base.py:419` `_retry_delay()`
  - 异常转响应：`base.py:494` `_error_response()`、`base.py:520` `_error_fields()`
  - 错误类别归一：`base.py:631` `_http_error_kind()`
  - 请求体递归合并：`base.py:580` `merge_body()`
  - 图片块生成：`base.py:672` `vision_image_block()`
  - 参数合并：`base.py:468` `_settings()`、同步桥：`base.py:367` `_run_async_compat()`
- 诊断引用的 6 个 `base.py` 行号（393 / 419 / 494 / 520 / 580 / 631 / 672）我逐个打开核对，**全部与源码一致，无一记错**。

判断：L158 的主语是 `base.py`（「它」= base.py），说它「不写实现」与源码不符，维持。

**一点补充（不足以推翻，但诊断说重了）**：文档 L461 自己写了「基类里同名的 `chat()` 是抽象方法……它只声明接口，不写实现。真正写转交的是两个适配器」——那里「它」指的是 `chat()`，是对的。所以读者在 L461 能自我纠正，L158 的伤害没有诊断说的那么致命。诊断只引了 L410、L429 证明「打架」，漏了 L461 这处自我澄清。改法仍建议按诊断给的那样拆成两句。

### 维持 2：L752 把 `include_stream_usage` 写成快照字段

条目原文：文档 L752 说「快照里有两个字段，上层要主动用起来：……`include_stream_usage`：流式请求要不要顺带取用量。它决定用量统计能不能实时更新。」

我找到的证据：

- `ProviderSnapshot` 在 `src/llm/factory.py:29-32` 只有 **4 个字段**：`provider`、`model`、`context_window_tokens`、`signature`。构造调用在 `src/llm/factory.py:81`，传入的也正是这 4 个值。
- `ProviderSnapshot` 是 `@dataclass(slots=True)`（`factory.py:15`），slots 意味着**不可能**动态挂上第五个属性。
- `include_stream_usage` 的真正归属是 `ProviderConfig`：`src/llm/config.py:31`；`factory.py:78` 是把它塞进 provider 的构造 kwargs，不是塞进快照。
- 全仓只有 **OpenAI 兼容适配器**读它：`src/llm/openai_compat.py:239`（`if stream and self.include_stream_usage:`）；`anthropic.py` 全文不含该标识符（我 grep 过）。
- 所以「它决定用量统计能不能实时更新」也没有源码依据：用量只在最后一个流式分片（或 Anthropic 的 message_delta）里回来一次。

判断：这是**事实性错误**（不是措辞问题），且会直接让读者去 `factory.py` 找一个不存在的字段。维持，且优先级最高。

### 维持 3：L371 示例只说了「去掉密钥」

条目原文：文档 L371 说「`config/model.example.json` 长这样（这里去掉了密钥）：」

我找到的证据（`config/model.example.json` 全文 22 行）：

- 第 5 行 `"api_key": ""` —— 文档没贴，这个说明书里交代了。
- 第 7 行 `"extra_headers": {}`、第 8 行 `"extra_body": {}` —— 文档没贴，也没交代。
- 第 13 行 `"label": "DeepSeek Flash"`、第 14 行 `"description": "系统唯一的内置档位……"` —— 文档没贴，也没交代。

判断：示例比说明多删了 4 个字段（密钥之外），说明只提了密钥。属于**交代不全**，维持（严重度低，读者照着贴出来的例子仍然能跑通）。诊断建议的「要么补全、要么把说明改成『删掉了密钥和几个可选字段』」两种改法都可行。

### 维持 4：L514 四个旧入口「目前还有调用方」不成立

条目原文：文档 L514 说「旧名字还留着四个：`chat_with_retry()`、`chat_stream_with_retry()`、`async_chat()`、`async_chat_stream()`。它们是兼容入口，**目前还有调用方**。」

我找到的证据（全仓 grep）：

- 定义处：`base.py:319`（`chat_with_retry`）、`base.py:326`（`chat_stream_with_retry`）、`base.py:336`（`async_chat`）、`base.py:341`（`async_chat_stream`）。
- `chat_with_retry` 有调用方：`src/agents/reading/relevance.py:94`、`src/agents/reading/relevance.py:153`、`test/test_model_connectivity.py:84`。
- `chat_stream_with_retry` 有调用方：`test/test_model_connectivity.py:113`。
- `async_chat` / `async_chat_stream`：**全仓只有定义、零调用点**（`grep -rn "async_chat_stream\|async_chat\b" --include=*.py` 只命中 `base.py:336` 和 `base.py:341`）。

判断：四个里有两个是死名字，文档一句「目前还有调用方」把四个都算进去了。维持。

### 维持 5：L176 使用方清单漏了 `eval/run_retrieval_eval.py`

条目原文：文档 L176 说「这一层还有几个直接使用方。`src/services/settings.py` 管设置页的模型目录。`src/agents/reading/relevance.py` 是脱离对话的阅读入口。`eval/lib/judge.py` 和 `scripts/probe_parallel_tool_calls.py` 是两个评测脚本。另外还有三个测试文件。」

我找到的证据：

- `eval/run_retrieval_eval.py:31`：`from src.llm.config import SystemConfig` —— 和 `eval/lib/judge.py:25` 同属 `eval/` 下的评测脚本，文档漏了它。
- 文档说的「三个测试文件」是对的：`test/test_live_model_config.py`、`test/test_llm_adapters.py`、`test/test_model_connectivity.py` 正好三个 import 本层。
- 另外确认 `src/services/settings.py:10` / `:216`（`make_provider(config, "__model_catalog__")`）、`src/agents/reading/relevance.py:342`（`make_provider(config, resolved_agent_name)`）、`scripts/probe_parallel_tool_calls.py:17` 都在。

判断：漏一个同类使用方，维持。

**一点折扣（不足以推翻）**：文档写的是「几个直接使用方」，措辞上并没自称完备枚举；诊断要求补的那句「此外还有十几个模块直接引用本层的公共函数（`attach_reasoning`、`normalize_token_usage` 等）」已经超出文档这段肉眼可见的范围（那段举的例子都是「装配入口」），属于「范围之争」，不算文档写错。所以这条我按「漏了一个 `eval/` 脚本」维持，不按「必须把所有 importer 都列全」维持。

---

## 二、推翻（诊断不成立）

### 推翻 1：L26 的 Provider 定义「与源码不符」

条目原文：诊断说「`L26` 文档说：『**Provider**：某一类模型接口的适配层。OpenAI、Anthropic 各算一种。』；源码实际：`registry.py` 的 `PROVIDERS` 有 5 条，其中 `openai_compat` 和 `anthropic_compat` 是协议名不是厂家名，`opencode_go` 是第三方网关……开头这句和表格对不上。」

为什么推翻：

1. **文档这句话是一句「定义 + 举例」，不是「枚举」。**原文是「某一类模型接口的适配层。OpenAI、Anthropic 各算一种。」它说的是「按接口家族分，OpenAI 系和 Anthropic 系各算一种」。而 `src/llm/registry.py:36-81` 里 5 条记录的 `backend` 只有两个取值——`openai_compat`（3 条）和 `anthropic`（2 条），`src/llm/factory.py:87-93` 也只有这两个分支。也就是说「按适配器家族算，确实只有两种」，与文档这句话一致，不构成「不符」。
2. **诊断给的理由自相矛盾。**它说「`openai_compat` 和 `anthropic_compat` 是协议名不是厂家名」——这恰好说明文档说的「OpenAI、Anthropic」指的是**协议家族**，而不是厂家；诊断自己也承认是两个适配器类（建议改法里写「对应两个适配器类」）。那么文档的表述在语义上是站得住的。
3. **文档紧接着就用表格把 5 条列全了**（L207-213，列名 `Provider` / `backend` / 默认地址 / 环境变量 / 关键字），读者不会停在 L26 形成「只有两个 Provider」的错误认知。诊断说「开头这句和表格对不上」，但表格第一列是「Provider（registry 键）」、第二列才是「backend（适配器）」——两者本来就不是同一个维度，对不上是正常的。
4. 这一节叫「与源码不符的地方」。L26 与任何一行源码都不冲突，把它放进这一节属于**归类错误**，最多算编辑建议。

判断：推翻。这条不是「文档与源码不符」，可以降级为措辞优化建议（比如写「OpenAI、Anthropic 两类协议各算一种，分别对应两个适配器类」），但不该记成源码错误。

---

## 三、新发现（诊断漏掉的、我自己查出的与源码不符之处）

### 新发现 1（中等）：L203 说配置里的 `backend` 「只有两种取值」，但配置里实际能写 5 种，本文档自己的例子就写了第三种

文档 L201-L203 原文：

> 系统真正认的是 provider 条目里的 `backend` 字段。`backend` 决定用哪个适配器，只有两种取值：`openai_compat` 和 `anthropic`。

**这里的「`backend`」在上一句已经明确指向「provider 条目里的字段」（即配置文件里的字段），不是 `ProviderSpec.backend`。**而配置文件里的 `backend` 走的是查表，键是 `PROVIDERS` 的全部 5 个名字：

- `src/llm/config.py:211`：`backend=value.get("backend") or ... or _infer_legacy_backend(name)`
- `src/llm/factory.py:65`：`spec = match_provider_backend(provider_config.backend)`，而 `match_provider_backend` 就是 `PROVIDERS[backend]`（`src/llm/registry.py:98-101`），所以 `backend` 可以是 `openai`、`openai_compat`、`anthropic`、`anthropic_compat`、`opencode_go` 中的任意一个。

反证在本仓库里就有三处，全是合法写法：

- 本文档 L377 自己的示例：`"backend": "anthropic_compat"`
- `config/model.example.json:4`：`"backend": "anthropic_compat"`
- `config/model.json`（本机真实配置）：`"backend": "opencode_go"`（第 11 行）、`"backend": "anthropic"`（第 18 行）

而且文档 L758 的「接一家新服务」第 1 条还教读者「把 `backend` 显式写成对应的值就行」——按 L203 的写法，读者会以为只能写 `openai_compat` 或 `anthropic`，从而把 `opencode_go`、`anthropic_compat` 当成非法值。

**建议改法**：把这句拆成两句——「`ProviderSpec.backend` 只有两种取值：`openai_compat` 和 `anthropic`，也就是两个适配器类。」「配置文件里的 `backend` 字段可以写 `PROVIDERS` 里的任意一个名字（`openai`、`openai_compat`、`anthropic`、`anthropic_compat`、`opencode_go`），系统拿它查表后统一落到上面两种适配器之一。」

### 新发现 2（中等）：L264 把 `provider` 缺省值 `auto` 说成「让系统按模型名去猜」，但这条链路在装配时根本没有人认 `auto`

文档 L264 原文：

> 档位里的 `provider` 字段不写时默认是 `auto`。这个值的意思是让系统按模型名去猜，而不是按名字查表。

源码实况：

- 默认值属实：`src/llm/config.py:239`：`provider=str(raw.get("provider") or "auto")`。
- 但**真正认 `auto` 的只有 `match_provider()`**（`src/llm/registry.py:124`：`if provider and provider != "auto":`），而这个函数**全仓零调用方**（`grep -rn "match_provider\b" --include=*.py` 只命中 `registry.py:104` 的定义）——文档 L271 自己也承认这点。
- 装配链走的是另一条路：`src/llm/config.py:173-177` 的 `resolve_provider_config()` 直接 `self.providers[agent.provider]` 查表，`"auto"` 不在 `providers` 里，于是抛 `ValueError("unknown provider: auto")`。`src/services/settings.py:554` 在保存校验时把 `provider == "auto"` 直接放行（不校验），所以用户完全可能存下一个运行时必炸的档位。

也就是说：文档让读者以为「不写 `provider` 就自动按模型名猜」，实际是「不写 `provider` → 运行时 `unknown provider: auto`」。这是在描述死代码的行为，却被写成了配置字段的语义。

**建议改法**：改成「档位里的 `provider` 字段不写时默认是 `auto`。目前装配链只按名字查 `providers` 表（`resolve_provider_config`），`auto` 不在表里会直接抛 `unknown provider: auto`。真正能处理 `auto` 的是 `match_provider()`，但它目前没有调用方，所以这个默认值实际上不能当『自动识别』用。」

### 新发现 3（轻微）：L715「原样透传」其实会改写大小写和连字符

文档 L715 原文：「`low`、`medium`、`high`、`xhigh`、`max` 原样透传。」

源码 `src/llm/anthropic.py:526`：`normalized = str(value).strip().lower().replace("-", "")`，随后 `return normalized`。所以传 `"X-High"` 得到的是 `"xhigh"`，不是原文。列出来的五个规范值恰好前后一致，所以影响很小，但「原样」二字不准确，建议改成「按小写去连字符后的形式透传」。

### 新发现 4（轻微）：L716 只说黑名单会拦 `temperature`，漏了「开了 thinking 也同样不发」

文档 L716 原文：「温度黑名单有 5 个前缀：……命中的模型不发 `temperature`。」

源码 `src/llm/anthropic.py:240`：`if settings.temperature is not None and effort is None and _anthropic_accepts_temperature(...)` —— 除了黑名单，**只要 `effort is not None`（也就是开了 thinking），`temperature` 一样不发**。文档 L715 讲了 thinking 开关，L716 讲了黑名单，但没点出两者的联动，读者可能以为非黑名单模型一定会带上 `temperature`。建议补一句。

### 新发现 5（不是错误，供参考）：诊断「必须保留」第 8 条里那串行号数量不对

诊断第 22 行写「（L184、L188、L221、L241、L307、L329、L351、L431、L467、L486、L530、L563、L606、L668、L689，共 14 处）」。这串里面有 15 个 L 编号，而文档里带行号的代码块（` ```起始行:结束行:文件路径 `）正好是 14 个（L184 是讲格式的正文，不是代码块）。所以「共 14 处」是对的，但列了 15 个编号，属于诊断自身的笔误。不影响结论。

---

## 四、我独立完成的抽查记录（不依赖诊断的结论）

### A. 文档里 14 处「起始行:结束行:文件路径」引用，逐个打开源码比对

| 文档位置 | 引用 | 源码实际 | 结论 |
| --- | --- | --- | --- |
| L188 | `26:33:src/llm/registry.py` | 26-33 正好是 `ProviderSpec` 的 8 个字段定义 | 通过 |
| L221 | `66:80:src/llm/registry.py` | 66-80 正好是 `opencode_go` 那一条（含 4 行中文注释） | 通过 |
| L241 | `84:101:src/llm/registry.py` | 84-101 正好是 `match_provider_backend()` 全函数 | 通过 |
| L307 | `120:136:src/llm/config.py` | 120-136 正好是 `AgentConfig`（含 `generation` 属性） | 通过 |
| L329 | `164:171:src/llm/config.py` | 164-171 正好是 `resolve_agent()` 全函数 | 通过 |
| L351 | `186:194:src/llm/config.py` | 186-194 正好是「必带头 + 用户头」那段与 `extra_headers={**...}` 行 | 通过 |
| L431 | `393:417:src/llm/base.py` | 393-417 正好是 `_run_llm_call()` 全函数 | 通过 |
| L467 | `437:448:src/llm/base.py` | 437-448 正好是 `_retry_delay()` 的计算段 | 通过 |
| L486 | `104:114:src/llm/base.py` | 104-114 正好是 `ok` 属性 | 通过 |
| L530 | `42:65:src/llm/factory.py` | 42-65 正好是 `make_provider()` 到 `match_provider_backend` 那一行 | 通过 |
| L563 | `29:39:src/llm/factory.py` | 29-39 正好是 4 个字段 + `aclose()` | 通过 |
| L606 | `223:245:src/llm/openai_compat.py` | 223-245 正好是 `if tools:` 到 `return kwargs` | 通过 |
| L668 | `378:393:src/llm/anthropic.py` | 378-393 正好是 `if role == "assistant":` 那段 | 通过 |
| L689 | `335:357:src/llm/anthropic.py` | 335-357 正好是 `flush_tool_results()` 全函数 | 通过 |

**14/14 全部通过，贴出来的代码也与源码逐字一致**（我抽了 registry 26:33、base 393:417、base 437:448、factory 42:65、anthropic 335:357 与 378:393 全文比对，一字不差）。这一节确实一个字都不用改。

### B. 文档里出现的函数名 / 常量名 / 字段名，逐个回源码找

抽查结果：**没有一个是源码里找不到的**。重点确认了容易记错的几个：

- `GenerationSettings`（`base.py:51`）、`StreamCallbacks`（`base.py:179`）、`ToolCallRequest`（`base.py:31`）、`LLMResponse`（`base.py:69`）、`LLMProvider`（`base.py:229`）
- `attach_reasoning()`（`base.py:117`）、`normalize_token_usage()`（`base.py:135`，确实只输出 `input_tokens`/`output_tokens`）、`merge_body()`（`base.py:580`）、`vision_image_block()`（`base.py:672`，默认 `media_type="image/png"`）、`yield_to_event_loop()`（`base.py:196`，函数体确实是 `await asyncio.sleep(0)`）
- `ProviderHttpError`（`base.py:206`）、`ProviderConnectionError`（`base.py:224`）
- `_settings()`（`base.py:468`）、`_should_retry()`（`base.py:563`）、`_http_error_kind()`（`base.py:631`）
- `LLMResponse` 的 12 个字段（`base.py:91-102`）与文档 L504 的清单**逐字对应**
- `ToolCallRequest` 的 4 个字段含 `provider_specific_fields`（`base.py:47`）
- `StreamCallbacks` 的 3 个回调名（`base.py:191-193`）
- `INTERRUPTED_TOOL_RESULT`（`anthropic.py:14`）与文档 L687 引的整句话**逐字一致**
- `_convert_messages`/`_convert_tool`/`_convert_tool_call`/`_parse_response`/`_thinking_block`（`anthropic.py:305/401/498/266/463`）
- `_accumulate_tool_call_delta`（`openai_compat.py:295`）里「缺 index 时带 id 算新调用、否则并入最后一次」（`openai_compat.py:308`）与文档 L640 一致；`stream_aggregated` 标记（`openai_compat.py:344`）与文档 L641 一致
- 温度黑名单 5 个前缀（`anthropic.py:541-547`）与文档 L716 列出的 5 个**完全一致**
- `PROVIDERS` 5 条、`openai` 的 `supports_max_completion_tokens=True` 且不裁前缀、`anthropic`/`anthropic_compat` 共用 `anthropic` 后端且都裁前缀（`registry.py:36-81`）
- `__init__.py` 导出 11 个名字、来自 3 个模块（`src/llm/__init__.py:1-3`），与文档 L162-164 的分组**逐字对应**
- 退避四个数值（500 毫秒、翻倍、封顶 32000 毫秒、0%～25% 抖动）对应 `base.py:437-441`；服务端上限 60 秒对应 `_RETRY_AFTER_CAP_SECONDS`（`base.py:25`）
- 运行控制默认值（`base.py:265-274`）：`api_base.rstrip("/")`、`timeout_s = float(max(1, ...))`、`max_retries = max(1, int(... or 3))`、`max_concurrency = max(1, int(... or 2))`、`include_stream_usage` 缺省 True —— 与文档 L506 五项**逐条一致**
- `LLMDefaults` 四项默认值 0.7 / 4000 / `"none"` / 1048576（`config.py:38-45`），`config/system.yaml` 四项确实都写 `null`（第 3-9 行）
- `read` 段六个字段与 10/60/50 三个数值（`config.py:52-62`、`config/system.yaml:18-24`）；`pdf_parser` 只认 `auto`/`pymupdf`/`pypdf`、写错回退 `auto`（`config.py:354-364`）
- `signature` 用 SHA-256、把 provider 名 + 档位 + 连接配置按 key 排序后求摘要（`factory.py:100-120`），且**全仓没有 `.signature` 消费点**（`grep` 只命中 Anthropic 的 thinking `signature` 与 `inspect.signature`，与快照字段无关）—— 文档 L581 的说法成立
- `match_provider()` 没有调用方（只有 `registry.py:104` 的定义）—— 文档 L271 成立
- `load_read_agent_llm()` 按 `ReadAgent.spec.llm_profile` 取档位、出厂值 `default_agent`（`relevance.py:342`、`relevance.py:61`）—— 文档 L174 成立
- 主 Agent 用 `research_agent` 档位、装一次快照后统一 `aclose()`（`chat_runtime.py:216`、`:158`、`:26`）—— 文档 L170/L172/L741 成立
- 「上下文窗口 × 0.8」属实（`src/agents/research/agent.py:65` / `src/agents/common/context_budget.py:28` 的 `HISTORY_BUDGET_RATIO = 0.8`）—— 文档 L579/L751 成立
- 「延伸阅读」的 5 个链接目标全部存在（`doc/arch/README.md`、`01-agent-orchestration.md`、`02-context-memory.md`、`03-tool-calling.md`、`04-availability.md`）

### C. 诊断自身引用行号的抽查

诊断在多处给了源码行号（`base.py:393/419/520/631/494/580/672`、`factory.py:29-32/81`、`config.py:31`、`openai_compat.py:239-241`、`relevance.py:94/153`、`test_model_connectivity.py:84/113`、`base.py:336/341`、`registry.py:36-81`、`eval/run_retrieval_eval.py:31`），我逐个打开核对，**全部正确**，没有发现把两处代码记混、或行号漂移的情况。诊断在这方面的可信度很高。

---

## 五、一句话结论

`doc/LLM-README.md` 与源码的符合程度**很高**：14 处行号引用一字不差，标识符、常量、字段名、默认值全部对得上，诊断点到的问题里 5 条成立（其中 L752 是硬错误）、1 条（L26）属于归类错误；此外我另找到 2 处诊断漏掉的、会误导新人的不符之处（L203 的 `backend` 取值域、L264 的 `auto` 语义），它们和 L158、L752 一样属于「说得比代码多一句」的类型。
