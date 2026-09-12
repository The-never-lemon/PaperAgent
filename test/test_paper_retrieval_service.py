import asyncio
import unittest

from src.paper_retrieval.connectors.base import PaperSearchConnector
from src.paper_retrieval.models import PaperDocument, SearchRequest
from src.paper_retrieval.service import PaperSearchService


class _FakeConnector(PaperSearchConnector):
    """测试用 connector，用来稳定验证编排层行为。"""

    def __init__(self, source_name: str, items: list[PaperDocument]):
        self.source_name = source_name
        self.items = items
        self.seen_requests: list[SearchRequest] = []

    def search(self, request: SearchRequest) -> list[PaperDocument]:
        """记录请求并返回预设结果，避免测试依赖真实外部网络。"""

        self.seen_requests.append(request)
        return list(self.items)


class _AsyncFakeConnector(_FakeConnector):
    """带异步入口的测试 connector，用来验证新的异步搜索主链路。"""

    async def async_search(self, request: SearchRequest, *, client=None) -> list[PaperDocument]:
        """异步入口里继续记录请求，确保服务层真的走到了 async 接口。"""

        self.seen_requests.append(request)
        await asyncio.sleep(0)
        return list(self.items)


class PaperSearchServiceTest(unittest.TestCase):
    def test_single_source_search_returns_standardized_response(self):
        service = PaperSearchService(
            connectors={
                "openalex": _FakeConnector(
                    "openalex",
                    [
                        PaperDocument(
                            id="oa-1",
                            title="Graph Neural Networks",
                            authors=["Alice"],
                            year=2024,
                            source="openalex",
                        )
                    ],
                )
            }
        )

        response = service.search(topic="graph neural networks", source="openalex", limit=5)

        self.assertEqual(response.sources_used, ["openalex"])
        self.assertEqual(response.source_results["openalex"], 1)
        self.assertEqual(response.total, 1)
        self.assertEqual(response.papers[0].title, "Graph Neural Networks")

    def test_multi_source_search_deduplicates_by_doi(self):
        duplicate_a = PaperDocument(
            id="a1",
            title="Shared Paper",
            authors=["Alice"],
            doi="10.1000/shared",
            source="openalex",
        )
        duplicate_b = PaperDocument(
            id="b1",
            title="Shared Paper",
            authors=["Bob"],
            doi="10.1000/shared",
            source="semantic_scholar",
        )
        unique = PaperDocument(
            id="c1",
            title="Unique Paper",
            authors=["Carol"],
            source="arxiv",
        )
        service = PaperSearchService(
            connectors={
                "openalex": _FakeConnector("openalex", [duplicate_a]),
                "semantic_scholar": _FakeConnector("semantic_scholar", [duplicate_b]),
                "arxiv": _FakeConnector("arxiv", [unique]),
            }
        )

        response = service.search(topic="shared query", limit=5)

        self.assertEqual(response.total, 2)
        self.assertEqual(sorted(response.source_results.keys()), ["arxiv", "openalex", "semantic_scholar"])

    def test_multi_source_search_over_fetches_for_each_connector(self):
        """每个来源都按超量拉取倍数拿到请求数量，而不是把 limit 平摊给各来源。"""

        service = PaperSearchService(
            connectors={
                "openalex": _FakeConnector("openalex", []),
                "arxiv": _FakeConnector("arxiv", []),
            }
        )

        response = service.search(topic="shared query", limit=5, truncate=False)

        # 中文注释：服务层会把用户期望的 5 篇乘以 OVER_FETCH_FACTOR 之后才向各来源要，
        # 目的是让客户端过滤（年份、排除词）之后仍然凑得够用户要的数量。
        expected_per_source = 5 * PaperSearchService.OVER_FETCH_FACTOR
        self.assertEqual(response.total, 0)
        self.assertEqual(service._connectors["openalex"].seen_requests[0].limit, expected_per_source)
        self.assertEqual(service._connectors["arxiv"].seen_requests[0].limit, expected_per_source)

    def test_invalid_source_returns_error(self):
        service = PaperSearchService(connectors={})

        response = service.search(topic="anything", source="missing", limit=3)

        self.assertIn("sources", response.errors)
        self.assertEqual(response.total, 0)

    def test_async_multi_source_search_uses_requested_sources(self):
        service = PaperSearchService(
            connectors={
                "openalex": _AsyncFakeConnector(
                    "openalex",
                    [PaperDocument(id="oa-1", title="OpenAlex Paper", authors=["Alice"], source="openalex")],
                ),
                "arxiv": _AsyncFakeConnector(
                    "arxiv",
                    [PaperDocument(id="ax-1", title="arXiv Paper", authors=["Bob"], source="arxiv")],
                ),
                "semantic_scholar": _AsyncFakeConnector(
                    "semantic_scholar",
                    [PaperDocument(id="ss-1", title="Semantic Paper", authors=["Carol"], source="semantic_scholar")],
                ),
            }
        )

        response = asyncio.run(
            service.async_search(
                topic="agent search",
                concept_groups=[["multi-agent"]],
                sources=["openalex", "arxiv"],
                limit=5,
                truncate=False,
            )
        )

        expected_per_source = 5 * PaperSearchService.OVER_FETCH_FACTOR
        self.assertEqual(response.sources_used, ["openalex", "arxiv"])
        self.assertEqual(response.total, 2)
        self.assertEqual(service._connectors["openalex"].seen_requests[0].limit, expected_per_source)
        self.assertEqual(service._connectors["arxiv"].seen_requests[0].limit, expected_per_source)
        self.assertEqual(service._connectors["semantic_scholar"].seen_requests, [])


if __name__ == "__main__":
    unittest.main()
