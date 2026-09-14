import unittest

from src.agents.skill_loader import skill_section
from src.models.deep_read import DEEP_READ_SOURCE_FULLTEXT, DeepReadReport, DimensionScore


def _sample_report() -> DeepReadReport:
    """构造一份带换行和多条列表的精读报告，专门用来检查下载 Markdown 的层次。"""

    return DeepReadReport(
        title="Federated Segmentation",
        source=DEEP_READ_SOURCE_FULLTEXT,
        short_summary="这篇论文研究多医院联邦分割。\n通信开销是它关注的重点。",
        main_question="如何在保护数据不出院的前提下完成多医院 CT 分割？",
        methods=[
            "方法从各医院的 CT 图像出发，经过本地编码、服务器聚合，再返回分割结果。",
            "本地编码器接收本院图像，提取特征后发给服务器。",
            "服务器聚合各院特征，更新全局模型后再下发。",
        ],
        datasets=["CT 多中心数据集（三家医院，共 1200 例）"],
        contributions=[
            "提出了通信压缩模块，用来降低各医院上传特征的带宽。",
            "给出了跨医院特征对齐策略，用来减轻各院扫描仪差异。",
        ],
        main_results=[
            "在医院 A 的 CT 集上，本文方法的 Dice 是 0.91（基线是 0.84）。",
            "在医院 B 的 CT 集上，本文方法的 Dice 是 0.88（基线是 0.80）。",
        ],
        experimental_setup=(
            "数据集：使用三家医院的 CT 图像，共 1200 例。\n"
            "评价指标：Dice 系数。\n"
            "骨干与超参：U-Net，学习率 0.001。\n"
            "训练细节：本地训练 5 轮后再聚合。"
        ),
        conclusions="作者认为压缩后的通信仍能保持分割精度。\n同时也指出尚未在真实医院部署。",
        limitations=["未在真实医院部署。"],
        overall_score=82,
        overall_comment="与主题相关，实验覆盖了多家医院。",
        created_at="2026-09-15T01:00:00Z",
    )


class DeepReadMarkdownTest(unittest.TestCase):
    """精读报告下载 Markdown 的层次与换行。"""

    def test_methods_contributions_results_use_numbered_lists(self):
        """方法、贡献、主要结果应按叙述顺序编号，不能再全部打成无序短横线。"""

        markdown = _sample_report().to_markdown()

        self.assertIn("## 方法\n\n1. 方法从各医院的 CT 图像出发", markdown)
        self.assertIn("2. 本地编码器接收本院图像", markdown)
        self.assertIn("3. 服务器聚合各院特征", markdown)
        self.assertIn("## 贡献\n\n1. 提出了通信压缩模块", markdown)
        self.assertIn("2. 给出了跨医院特征对齐策略", markdown)
        self.assertIn("## 主要结果\n\n1. 在医院 A 的 CT 集上", markdown)
        self.assertIn("2. 在医院 B 的 CT 集上", markdown)
        self.assertNotIn("- 方法从各医院的 CT 图像出发", markdown)
        self.assertNotIn("- 提出了通信压缩模块", markdown)
        self.assertNotIn("- 在医院 A 的 CT 集上", markdown)

    def test_datasets_and_limitations_stay_bullets(self):
        """数据集和局限仍是并列项，继续用无序列表。"""

        markdown = _sample_report().to_markdown()

        self.assertIn("## 数据集\n\n- CT 多中心数据集", markdown)
        self.assertIn("## 局限\n\n- 未在真实医院部署。", markdown)

    def test_prose_fields_keep_line_breaks(self):
        """总结、实验设置、结论里的换行要保留，不能压成一行墙。"""

        markdown = _sample_report().to_markdown()

        self.assertIn("这篇论文研究多医院联邦分割。\n通信开销是它关注的重点。", markdown)
        self.assertIn("作者认为压缩后的通信仍能保持分割精度。\n同时也指出尚未在真实医院部署。", markdown)
        self.assertNotIn(
            "这篇论文研究多医院联邦分割。 通信开销是它关注的重点。",
            markdown,
        )
        self.assertNotIn(
            "数据集：使用三家医院的 CT 图像，共 1200 例。 评价指标：Dice 系数。",
            markdown,
        )

    def test_experimental_setup_labels_are_separated(self):
        """实验设置四行标签要能在 Markdown 里分开看，标签加粗。"""

        markdown = _sample_report().to_markdown()
        setup = markdown.split("## 实验设置\n\n", 1)[1].split("\n## ", 1)[0]

        self.assertIn("**数据集**：使用三家医院的 CT 图像，共 1200 例。", setup)
        self.assertIn("**评价指标**：Dice 系数。", setup)
        self.assertIn("**骨干与超参**：U-Net，学习率 0.001。", setup)
        self.assertIn("**训练细节**：本地训练 5 轮后再聚合。", setup)
        self.assertIn("\n\n", setup)

    def test_chunk_markers_are_stripped_from_reader_facing_fields(self):
        """分段笔记的 [paper:p0001:s0003] 是内部定位，不能出现在给人看的报告里。"""

        report = DeepReadReport(
            title="Attention",
            methods=["编码器先把句子切成 token[1706.03762:p0001:s0003]。"],
            contributions=["提出了多头注意力 1706.03762:p0002:s0001 用来并行看不同位置。"],
            short_summary="这篇论文提出了 Transformer[1706.03762:p0001]。",
            overall_comment="相关性高 [abc:c0001]。",
            relevance=DimensionScore(score=80, rationale="与主题相关[1706.03762:p0004:s0002]"),
        )
        markdown = report.to_markdown()
        payload = report.to_dict()

        self.assertNotIn("1706.03762:p0001:s0003", markdown)
        self.assertNotIn("1706.03762:p0002:s0001", markdown)
        self.assertNotIn("[1706.03762:p0001]", markdown)
        self.assertNotIn(":p0004:s0002", payload["short_summary"] + payload["methods"][0])
        self.assertNotIn(":p0001", payload["methods"][0])
        self.assertIn("编码器先把句子切成 token。", payload["methods"][0])
        self.assertIn("这篇论文提出了 Transformer。", payload["short_summary"])
        self.assertNotIn(":p0004", payload["relevance"]["rationale"])

    def test_oneline_setup_with_embedded_labels_is_split(self):
        """模型常把四块标签写进同一段 JSON 字符串，下载和展示时也必须切开。"""

        report = DeepReadReport(
            experimental_setup=(
                "数据集：使用 WMT14 英德平行语料。评价指标：BLEU。"
                "骨干与超参：Transformer base，学习率 0.0001。"
                "训练细节：warmup 4000 步，共训练 100k 步。"
            )
        )
        markdown = report.to_markdown()
        setup = markdown.split("## 实验设置\n\n", 1)[1].split("\n## ", 1)[0]
        payload = report.to_dict()["experimental_setup"]

        self.assertIn("**数据集**：使用 WMT14 英德平行语料。", setup)
        self.assertIn("**评价指标**：BLEU。", setup)
        self.assertIn("**骨干与超参**：Transformer base，学习率 0.0001。", setup)
        self.assertIn("**训练细节**：warmup 4000 步，共训练 100k 步。", setup)
        self.assertEqual(payload.count("\n"), 3)
        self.assertNotIn("数据集：使用 WMT14 英德平行语料。评价指标：BLEU。", setup)

    def test_unlabeled_setup_splits_into_sentences(self):
        """没有标签的实验设置也不能糊成一段墙，按句切开。"""

        report = DeepReadReport(
            experimental_setup="实验在 WMT14 上进行。骨干是 Transformer base。训练了 100k 步。"
        )
        payload = report.to_dict()["experimental_setup"]
        markdown = report.to_markdown()

        self.assertEqual(
            payload.split("\n"),
            ["实验在 WMT14 上进行。", "骨干是 Transformer base。", "训练了 100k 步。"],
        )
        self.assertIn("实验在 WMT14 上进行。\n\n骨干是 Transformer base。", markdown)

    def test_from_dict_cleans_old_reports(self):
        """已经落盘的旧报告打开时也要去掉分块号、切开实验设置。"""

        report = DeepReadReport.from_dict(
            {
                "methods": ["多头注意力[1706.03762:p0003:s0002]"],
                "experimental_setup": "数据集：WMT14。评价指标：BLEU。骨干与超参：base。训练细节：100k 步。",
            }
        )

        self.assertEqual(report.methods, ["多头注意力"])
        self.assertEqual(
            report.experimental_setup.split("\n"),
            ["数据集：WMT14。", "评价指标：BLEU。", "骨干与超参：base。", "训练细节：100k 步。"],
        )


class DeepReadSkillInjectionTest(unittest.TestCase):
    """精读报告汇总提示词要吃到叙述逻辑，分段阅读不要吃到。"""

    def test_narrative_section_exists(self):
        """技能文档必须能按小节名取出「叙述逻辑」。"""

        text = skill_section("paper-deep-reading", "叙述逻辑")
        self.assertIn("第 1 条用完整句子写总流程", text)
        self.assertIn("按数据集或实验设置分组", text)
        self.assertIn("数据集：", text)

    def test_report_prompts_include_narrative_and_drop_slot_templates(self):
        """汇总和摘要降级要带上叙述逻辑；旧的填空句式不能再出现。"""

        from src.agents.Prompts import (
            DEEP_READ_ABSTRACT_SYSTEM_PROMPT,
            DEEP_READ_MAP_SYSTEM_PROMPT,
            DEEP_READ_REDUCE_SYSTEM_PROMPT,
        )

        for prompt in (DEEP_READ_REDUCE_SYSTEM_PROMPT, DEEP_READ_ABSTRACT_SYSTEM_PROMPT):
            self.assertIn("第 1 条用完整句子写总流程", prompt)
            self.assertIn("能单独读懂的完整陈述句", prompt)
            self.assertNotIn("{组件}：{作用}", prompt)
            self.assertNotIn("针对{什么不足}", prompt)

        self.assertNotIn("第 1 条用完整句子写总流程", DEEP_READ_MAP_SYSTEM_PROMPT)
        self.assertNotIn("能单独读懂的完整陈述句", DEEP_READ_MAP_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
