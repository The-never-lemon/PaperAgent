<div align="center">

# Paper-Agent · 智能论文检索与综述写作工作台

**输入一个研究主题 → 收获一份可全程追踪的领域综述**

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Frontend-Vue_3-42B883?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![uv](https://img.shields.io/badge/Package%20Manager-uv-DE5FE9?logo=astral&logoColor=white)](https://docs.astral.sh/uv/)

</div>

---

## 🆕 2.0 版本更新

Paper-Agent 2.0 是相对旧版 1.x 的一次**全新重写**。它保留了旧版「检索 → 阅读 → 分析 → 写作」的多智能体核心思路，但在工程实现上做了全面升级：

- **前端**改为 Vue 3 + TypeScript + Vite，交互更现代、响应更快；
- **包管理**统一使用 `uv`，一条命令即可完成 Python 依赖安装；
- **论文来源**从单一 arXiv 扩展到 arXiv、OpenAlex、Semantic Scholar 三源检索；
- **会话持久化**改用 SQLite + 文件系统，浏览器刷新后历史线程不丢失；
- **实时进度**基于 SSE 推送到工作台，从检索到写作每一步都可观察。


---

## 👀 界面概览

<p align="center">
  <img src="https://github.com/user-attachments/assets/a88a2823-9724-4a7a-aee4-fafbfef068a6" width="720" alt="Paper-Agent 会话工作台" />
  <br>
  <em>输入研究主题，实时追踪检索论文、精读论文、生成综述等各环节进度（综述内部的分析、大纲、逐节写作以同一张卡片的实时状态文字呈现）</em>
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/e312fc8b-d84e-46f5-86df-5202d2197dfe" width="720" alt="Paper-Agent 系统设置" />
  <br>
  <em>在浏览器中可视化配置模型 Provider 与 Agent 档位，一键测试连通性</em>
</p>

---

## 🎯 为什么是 Paper-Agent？

做学术调研时，你一定经历过这些：

| 场景 | 传统方式 | **Paper-Agent** |
|------|---------|-----------------|
| 初步了解陌生研究方向 | 手动搜索多个来源，逐个打开论文判断相关度，**耗时费力** | 从 arXiv / OpenAlex / Semantic Scholar 自动检索，按主题和约束去重、筛选、整理 |
| 论文阅读与资料积累 | 读完一篇记一篇笔记，资料散落在各处，**容易丢失和重复** | 先读摘要判断相关性，再按条件下载全文、解析、切分，逐块精读并把全文与笔记留在本地 |
| 撰写领域综述 | 边读边写，反复调整结构，**常常写到一半推倒重来** | 先从论文整体分析、再做全局综合，据此生成结构化大纲，然后逐节按证据写作 |
| 管理长流程任务 | 每跑一步都担心进度、状态和重启后丢失，**不敢中途停下** | 会话、运行状态、阶段产物和实时进度都在工作台可见；刷新后历史与实时流可接回，进程中断的任务会被标记为「已中断」，可基于工作区已保存的产物继续下一轮调研 |
| 控制模型成本 | 全程用一个模型档位，**不清楚每个环节花了多少 token** | 精读、追问、综述等子 Agent 的 token 用量会累加到对应工具卡片；主对话按轮次上报，多轮对话下看到的是最近一轮的用量 |

> **Paper-Agent 不只是论文摘要工具，而是一个完整的 AI 研究助理——它找得到论文、读得懂全文、理得清脉络、写得出综述。**

---

## ✨ 核心特性

| | 特性 | 一句话说明 |
|--|------|-----------|
| 🔍 | **多来源论文检索** | 内置 arXiv、OpenAlex、Semantic Scholar 连接器，统一为 `PaperDocument`，按年份、来源、数量和排除词筛选并去重评分；配好嵌入模型后，还会把主题与候选论文的语义相似度作为排序的最后一档参考（命中源数、概念组匹配、引用数、年份都相同时才起作用） |
| 📖 | **从摘要到全文的渐进式阅读** | 先读摘要判断相关性，满足条件的论文走下载 → PDF 转 Markdown → 分块 → 逐块精读并汇总；全文下载或转换失败时自动降级为「基于摘要的精读」，并把失败原因告知用户 |
| 🔬 | **分层研究分析** | `AnalyseAgent` 先把工作区全部论文的结构化摘要做一次整体分析，再做一次全局综合，形成研究现状、共识、争议、空白、时间演化与展望等结构化内容 |
| ✍️ | **证据约束下的综述写作** | `WritingOutlineAgent` 生成大纲与证据映射，`WritingAgent` 逐节写作、证据不足时检索补充、完成后审查并限次修改 |
| 📡 | **实时会话工作台** | SSE 实时推送检索、阅读、分析、大纲与逐节写作进度，SQLite + 文件系统持久化，刷新后历史可恢复 |
| 🎛️ | **可视化模型配置** | 在浏览器中管理 Provider 协议、API 地址、密钥、各 Agent 档位与 embedding 参数，一键测试连通性，保存即生效 |

---

## 🔧 架构

```mermaid
flowchart TB
    subgraph Main["主对话 Agent (researchAgent)"]
        direction TB
        U[用户输入] --> LLM[LLM 工具循环]
        LLM --> T1[search_papers]
        LLM --> T2[evaluate_papers]
        LLM --> T3[deep_read_paper]
        LLM --> T4[ask_paper]
        LLM --> T5[generate_review]
        LLM --> T6[expand_by_citations]
        LLM --> T7[list_papers]
        LLM --> T8[get_paper_details]
        LLM --> T9[remove_papers]
        LLM --> T10[download_paper]
    end
    
    subgraph SubAgents["子 Agent（agent-as-tool）"]
        direction TB
        DR[DeepReadAgent\n全文精读 map-reduce]
        PQ[PaperQAAgent\n基于全文的追问]
        RV[ReviewPipeline\n编排 analyse/outline/writing]
    end
    
    T3 --> DR
    T4 --> PQ
    T5 --> RV
    
    subgraph Sources["外部数据源"]
        direction LR
        AX[arXiv]
        OA[OpenAlex]
        SS[Semantic Scholar]
    end
    
    T1 --> Sources
    T6 --> OA
    T6 --> SS
```

> 主对话共注册 10 个工具（上图全部列出）。引文扩展（`expand_by_citations`）只对实现了该接口的 OpenAlex 与 Semantic Scholar 生效，arXiv 连接器未实现，会按空结果处理。

---

## 📦 技术栈

| 层级 | 技术选型 |
|------|---------|
| 运行时 | Python 3.12+、`uv`、Uvicorn |
| 后端 API | FastAPI、REST、Server-Sent Events（SSE） |
| 编排模式 | Orchestrator-workers（主 Agent + 子 Agent 以 tool 形式注册） |
| Agent | 主对话 Agent（researchAgent）+ 精读 / 追问 / 综述三个子 Agent |
| LLM 适配 | OpenAI 兼容协议、Anthropic Messages 协议 |
| 论文来源 | arXiv、OpenAlex、Semantic Scholar |
| 全文处理 | `pypdf`、Markdown 转换、文本分块 |
| 语义重排 | 任意 OpenAI 兼容的 embedding 接口；作为检索排序的最后一档参考，未配置则该档不参与 |
| 会话存储 | SQLite + 本地 JSON/Markdown 文件 |
| 前端 | Vue 3、TypeScript、Vite、Vue Router、Lucide |

---

## 📂 项目目录

```text
Paper-Agent/
├── main.py                         # FastAPI 本地启动入口
├── pyproject.toml                  # Python 项目元数据与依赖
├── package.json                    # 根目录前端快捷命令
├── config/
│   ├── model.json                  # 本地模型配置，不提交
│   ├── model.example.json          # 模型配置示例
│   └── system.yaml                 # 系统默认参数
├── front/                          # Vue 3 + TypeScript 前端
│   ├── src/api/                    # 会话与设置 API 客户端
│   ├── src/components/             # 工作台、状态和会话组件
│   ├── src/views/                  # 会话工作台、系统配置页
│   └── vite.config.ts              # 开发服务器、代理和端口配置
├── src/
│   ├── agents/                     # Agent 定义、模型调用和写作工具
│   ├── api/                        # FastAPI 应用与路由
│   ├── graph/                      # 运行期基础设施：运行上下文、取消控制、节点事件上报
│   ├── llm/                        # Provider 适配、配置解析和统一响应
│   ├── models/                     # 会话、阅读与工作区领域模型
│   ├── paper_retrieval/            # 论文模型、检索服务和来源连接器
│   ├── repositories/               # SQLite、JSON 与阶段产物持久化
│   ├── services/                   # 会话、运行、设置和工作流服务
│   └── utils/                      # 日志、缓存、全文解析和分块工具
├── data/                           # 本地数据库、论文缓存与会话数据
├── logs/                           # 运行日志
└── test/                           # unittest 测试与联调辅助代码
```

---

## 🚀 快速开始

### 1. 安装项目依赖

在项目根目录执行：

```powershell
uv venv --python 3.12
uv sync
npm run front:install
```

如果你已经有可用的 Python 3.12 虚拟环境，也可以直接执行 `uv sync` 和 `npm run front:install`。

### 2. 创建本地模型配置

推荐通过 Web 工作台的「系统配置」页面修改和测试配置。也可以手动拷贝 `config/model.example.json` 为 `config/model.json`，至少确认：

1. `providers` 中存在一个可用 Provider，并填写 `api_base`；
2. `api_key` 或 `api_key_env` 能提供有效密钥；
3. `agents.default_agent` 已配置；
4. `embedding_profiles` 已指向可用的 embedding Provider（默认档位名是 `default_embedding`，也可以用顶层 `default_embedding_profile` 指定别的名字；只配一个档位时它自动就是默认档位）。

### 3. 启动后端

```powershell
uv run python main.py
```

后端默认监听 `127.0.0.1:8000`，开发模式自动重载。API 文档：<http://127.0.0.1:8000/docs>

### 4. 启动前端

```powershell
npm run front:dev
```

打开 <http://127.0.0.1:5173/>，先进入「系统配置」测试模型，再进入会话工作台创建研究任务。

前端默认只监听本机，并代理 `/api`、`/webui` 请求到 `127.0.0.1:8000`。如需局域网其他设备访问：

```powershell
npm run front:dev:network
```

### 5. 只用一个端口运行（不需要 Node）

上面第 4 步的双服务模式是给开发用的。如果只是想把整个应用跑起来自己用，可以先构建一次前端，
之后由后端直接托管界面，**不需要 Node，也不需要 5173 端口**：

```powershell
npm run front:build
uv run python main.py
```

注意 `main.py` 是带 `reload=True` 启动的（开发时改代码自动重启，会额外拉一个监视进程）。
想要真正的单进程运行，用分发用的那个启动器，它显式关掉了 reload：

```powershell
uv run python scripts/launch.py
```

然后访问 <http://127.0.0.1:8000/> 即可（不是 5173）。后端会挂载 `front/dist` 并接管前端路由，
`/api` 与页面同源，因此不需要任何代理配置。

> 注意：`front/dist` 只在构建后存在。缺了它后端会打一条 warning 并跳过界面挂载，
> 此时只有 `/docs` 和 `/api/*` 可用。

想把这个形态做成「解压双击即用」的分发包，用内置的打包与启动脚本：

```powershell
uv run python scripts/package.py    # 生成 Paper-Agent-<日期>.zip
```

包内自带 `start.bat` 和 `uv.exe`，同事解压后双击即可，不用装 Python 或 Node。
面向使用者的说明见 [使用说明.md](使用说明.md)。

---

## 🔧 模型配置

系统支持配置多个 Provider。内置档位只有 `default_agent` 一个，AgentSpec 中声明了档位的子 Agent 都指向它；主对话另有可选档位 `research_agent`（未配置时回退 `default_agent`）。

- 配置主文件：`config/model.json`（含密钥，不入库），示例见 `config/model.example.json`，系统参数见 `config/system.yaml`

| Agent | 运行时实际使用的档位 | 主要职责 |
| --- | --- | --- |
| `researchAgent`（主对话） | `research_agent`，未配置时回退 `default_agent` | 多轮对话、工具调用与任务分派 |
| `deepReadAgent` / `paperQaAgent` | 复用主对话的档位快照 | 全文精读、基于全文的追问 |
| `AnalyseAgent` | 复用主对话的档位快照 | 分析论文，并综合研究现状与趋势 |
| `WritingOutlineAgent` | 复用主对话的档位快照 | 生成正文大纲和证据映射 |
| `WritingAgent` | 复用主对话的档位快照 | 逐节写作、调用资料工具和审查修改 |
| `ReadAgent` | 复用主对话的档位快照 | 阅读摘要，判断相关性并整理笔记 |

`default_agent` 是唯一必需的档位，缺失时配置无法工作。

> 说明：`AgentSpec` 里声明的 `llm_profile` 在当前装配路径下不生效——对话中被当作工具调用的子 Agent 直接复用主对话的模型快照，因此配了 `research_agent` 时它们跑的也是 `research_agent` 的模型。

### Provider 后端

支持 `backend` 类型：`openai`、`openai_compat`、`anthropic`、`anthropic_compat`。示例：

```json
{
  "providers": {
    "my_provider": {
      "backend": "openai_compat",
      "api_key_env": "OPENAI_API_KEY",
      "api_base": "https://api.openai.com/v1",
      "extra_headers": {},
      "extra_body": {}
    }
  }
}
```

`api_key` 与 `api_key_env` 二选一即可。使用兼容网关时通常需要同时填写 `backend`、`api_base` 和模型名称。

### 系统参数

`config/system.yaml` 存放系统级默认值和阅读参数：`defaults.llm` 是所有 Agent 档位未声明字段时的兜底生成参数，`defaults.embedding` 是嵌入档位默认值，`paper_retrieval` 是检索数据源密钥，`read` 是阅读与下载参数（缓存目录、连接/下载超时、最大文件大小）。

> `read.chunk_size` / `read.chunk_overlap` 目前只是配置项：实际分块固定按每 1200 字符、按 PDF 页切分且不重叠，改这两项不会生效。

---


## 👤 作者

**The-never-lemon**

<p align="center">
  <a href="https://github.com/The-never-lemon">
    <img src="https://github.com/The-never-lemon.png" width="80" height="80" style="border-radius:50%" alt="The-never-lemon" />
  </a>
  <br>
  <strong><a href="https://github.com/The-never-lemon">@The-never-lemon</a></strong>
</p>

---

## ❤️ 特别致谢

感谢 **@GreatZack** 对 Paper-Agent 的持续投入与核心贡献：

<p align="center">
  <a href="https://github.com/GreatZack">
    <img src="https://github.com/GreatZack.png" width="80" height="80" style="border-radius:50%" alt="GreatZack" />
  </a>
  <br>
  <strong><a href="https://github.com/GreatZack">@GreatZack</a></strong>
</p>

---

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request。建议贡献前先完成：

1. 在 `test/` 中补充或更新对应行为的测试；
2. 运行 `uv run python -m unittest discover -s test -v`；
3. 运行 `npm run front:build`，确保前端类型检查和构建通过；
4. 在 PR 描述中说明改动范围、配置影响和复现步骤。

项目地址：<https://github.com/The-never-lemon/PaperAgentMain>

---

<div align="center">

**让论文检索更快，让研究脉络更清楚。**

</div>