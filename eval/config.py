"""
评估体系全局配置文件
存放所有评估相关的可调参数，后续任务都会从这里导入
"""

from pathlib import Path

# ==================== 路径配置 ====================
# 仓库根目录（让脚本自己计算）
REPO_ROOT = Path(__file__).resolve().parents[1]

# 会话数据目录
SESSIONS_DIR = REPO_ROOT / "data" / "sessions"

# 日志目录
LOGS_DIR = REPO_ROOT / "logs"

# SQLite 数据库路径
SQLITE_DB_PATH = REPO_ROOT / "data" / "session_store.db"

# 评估输出目录
EVAL_DIR = REPO_ROOT / "eval"

# 报告输出目录
REPORTS_DIR = EVAL_DIR / "reports"


# ==================== 评估流量标记 ====================
# 用来区分评估流量与真实用户流量，通过会话 title 前缀匹配
EVAL_SESSION_PREFIX = "eval_"


# ==================== 判断模型配置（后续任务 judge 用） ====================
# config/model.json 里的裁判模型档位名
EVAL_JUDGE_AGENT = "eval_judge"

# 裁判模型的采样温度（设为 0 以获得确定性结果）
JUDGE_TEMPERATURE = 0.0

# 并发评估的最大并发数
JUDGE_MAX_CONCURRENCY = 3

# 给裁判看的论文摘要截断长度（字符数）
ABSTRACT_TRUNCATE_CHARS = 800

# 给裁判看的综述全文截断长度（字符数）
REVIEW_TRUNCATE_CHARS = 12000

# 忠实性评估每条用例最多抽取的论断数
MAX_CLAIMS_PER_CASE = 10


# ==================== 价目表（成本估算用） ====================
# 按模型和操作类型的价格表（元/百万 token）
# 注意：这是占位价格，使用前必须验证真实的官方价目
PRICE_TABLE_CNY_PER_M_TOKENS = {
    "deepseek-v4-flash": {
        "input": 2.0,      # 输入 token 价格
        "output": 8.0,     # 输出 token 价格
    }
}

# 价目表的来源说明
PRICING_SOURCE_NOTE = "占位价格：请按所用模型官方价目核实后修改（2026-09 录入）"

# 单次全量评估成本硬上限（元），超过即中止后续用例
MAX_RUN_COST_CNY = 5.0


# ==================== 运行控制参数 ====================
# 端到端评估默认超时时间（秒）
E2E_DEFAULT_TIMEOUT_S = 900

# 综述用例超时时间（秒）- 实测需要 41 分钟，所以要放宽
E2E_REVIEW_TIMEOUT_S = 2400

# 端到端用例默认并发数（1 = 串行）
E2E_CONCURRENCY = 1


# ==================== 评分权重与阈值 ====================
# 聚合报告中各评估层的权重
LAYER_WEIGHTS = {
    "L1": 0.30,  # 检索评估权重
    "L2": 0.50,  # 端到端评估权重
    "L3": 0.20,  # 可靠性统计权重
}

# 各指标的通过阈值（值低于阈值时在报告里标记警告 ⚠️）
THRESHOLDS = {
    # L3 可靠性指标阈值
    "turn_completed_rate": 0.9,           # turn 完成率 >= 90%
    "tool_success_rate": 0.95,            # 工具成功率 >= 95%（个别工具可能放宽）
    "llm_retry_rate": 0.1,                # LLM 重试率 <= 10%
    "download_success_rate": 0.9,         # 论文下载成功率 >= 90%
    "abstract_fallback_rate": 0.2,        # abstract fallback 率 <= 20%
    "search_relaxed_rate": 0.3,           # 检索放宽率 <= 30%
    "evaluation_coverage": 0.8,           # 评估覆盖率 >= 80%
    "cancel_rate": 0.05,                  # 取消率 <= 5%
    "retry_after_fail_rate": 0.3,         # 失败后重试率 <= 30%

    # L1 检索评估阈值
    "l1_ndcg_at_10": 0.75,
    "l1_precision_at_10": 0.7,
    "l1_golden_recall": 0.6,
    "l1_abstract_completeness": 0.9,
    "l1_errors_empty_rate": 0.8,
    "l1_reranking_diff": 0.4,
    "l1_expansion_success_rate": 0.9,
    "l1_expansion_relevance": 1.2,
    "l1_search_p95_latency_s": 15,

    # L2 端到端评估阈值
    "l2_completion_rate": 0.9,
    "l2_citation_validity_rate": 1.0,
    "l2_relevance_judge_score": 3.5,
    "l2_factuality_judge_score": 3.5,
    "l2_review_factuality": 3,
    "l2_completeness": 3.5,
    "l2_organization": 3,
}
