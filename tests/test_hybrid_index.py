import tempfile
import unittest
from pathlib import Path

from scenic_pilot.knowledge.chunking import chunk_documents
from scenic_pilot.knowledge.index import HybridIndex
from scenic_pilot.models import SourceDocument


def document(source_id: str, scenic_id: str, title: str, content: str) -> SourceDocument:
    return SourceDocument(
        source_id=source_id,
        scenic_id=scenic_id,
        title=title,
        category="transport",
        content=content,
        source_url=f"https://example.test/{source_id}",
    )


class HybridIndexTests(unittest.TestCase):
    def test_hybrid_search_and_metadata_filter(self) -> None:
        index = HybridIndex(
            chunk_documents(
                [
                    document("zh-traffic", "zhuhai-chimelong", "交通指南", "公共交通和接驳信息"),
                    document("other", "other-scenic", "其他景区", "公共交通和接驳信息"),
                ]
            )
        )
        hits = index.search(
            "珠海长隆交通", filters={"scenic_id": "zhuhai-chimelong"}, top_k=3
        )

        self.assertEqual([hit.chunk.source_id for hit in hits], ["zh-traffic"])
        self.assertGreater(hits[0].score, 0)

    def test_index_round_trip(self) -> None:
        index = HybridIndex(
            chunk_documents([document("zh-traffic", "zhuhai-chimelong", "交通", "接驳信息")])
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "index.json"
            index.dump(target)
            restored = HybridIndex.load(target)
            self.assertEqual(restored.search("接驳")[0].chunk.source_id, "zh-traffic")


if __name__ == "__main__":
    unittest.main()
