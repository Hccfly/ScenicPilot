import unittest

from scenic_pilot.knowledge.chunking import (
    chunk_document,
    chunk_document_hierarchy,
    chunk_document_hierarchy_v2,
)
from scenic_pilot.models import SourceDocument


def make_document(content: str) -> SourceDocument:
    return SourceDocument(
        source_id="source-1",
        scenic_id="zhuhai-chimelong",
        title="测试资料",
        category="overview",
        content=content,
        source_url="https://example.test/source",
        authority_level=3,
        volatile=True,
        demo=True,
    )


class ChunkingTests(unittest.TestCase):
    def test_chunking_preserves_metadata_and_is_deterministic(self) -> None:
        document = make_document("第一段资料。第二段资料需要继续说明。第三段资料用于测试。")
        first = chunk_document(document, chunk_size=16, overlap=4)
        second = chunk_document(document, chunk_size=16, overlap=4)

        self.assertEqual([item.chunk_id for item in first], [item.chunk_id for item in second])
        self.assertGreaterEqual(len(first), 2)
        self.assertTrue(all(item.scenic_id == "zhuhai-chimelong" for item in first))
        self.assertTrue(all(item.volatile and item.demo for item in first))

    def test_empty_document_produces_no_chunks(self) -> None:
        self.assertEqual(chunk_document(make_document("   ")), [])

    def test_high_overlap_still_moves_forward_and_offsets_match(self) -> None:
        document = make_document("第一段 有空格。第二段继续。第三段继续。")
        chunks = chunk_document(document, chunk_size=16, overlap=15)
        normalized = "第一段 有空格。第二段继续。第三段继续。"

        self.assertLess(len(chunks), len(normalized) + 1)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(
                normalized[chunk.char_start : chunk.char_end], chunk.content
            )

    def test_parent_child_chunks_keep_ids_when_heading_content_changes(self) -> None:
        first = make_document("# 游玩建议\n先看地图，再进入园区。")
        second = make_document("# 游玩建议\n先看地图，再进入园区，并留意当天公告。")

        old_chunks = chunk_document_hierarchy(first)
        new_chunks = chunk_document_hierarchy(second)

        self.assertEqual(
            [item.chunk_id for item in old_chunks],
            [item.chunk_id for item in new_chunks],
        )
        self.assertNotEqual(
            [item.content_version for item in old_chunks],
            [item.content_version for item in new_chunks],
        )
        parent = next(item for item in new_chunks if item.level == "parent")
        child = next(item for item in new_chunks if item.level == "child")
        self.assertEqual(child.parent_chunk_id, parent.chunk_id)
        self.assertEqual(child.heading_path, ("游玩建议",))

    def test_v2_leaf_heading_child_expands_to_shared_parent_subtree(self) -> None:
        document = make_document(
            "## 酒店住宿\n\n### 企鹅酒店\n\n亲子主题酒店。\n\n"
            "### 飞船酒店\n\n拥有主题客房和无边际泳池。"
        )

        chunks = chunk_document_hierarchy_v2(
            document,
            child_size=32,
            child_overlap=4,
            parent_size=256,
        )

        parents = [item for item in chunks if item.level == "parent"]
        children = [item for item in chunks if item.level == "child"]
        self.assertEqual(len(parents), 1)
        self.assertIn("企鹅酒店", parents[0].content)
        self.assertIn("飞船酒店", parents[0].content)
        self.assertTrue(all(item.parent_chunk_id == parents[0].chunk_id for item in children))
        self.assertTrue(any(item.heading_path[-1] == "飞船酒店" for item in children))

    def test_v2_keeps_anchor_ids_when_content_changes_inside_budget(self) -> None:
        first = make_document("## 酒店住宿\n\n### 飞船酒店\n\n拥有主题客房。")
        second = make_document(
            "## 酒店住宿\n\n### 飞船酒店\n\n拥有主题客房和无边际泳池。"
        )

        old_chunks = chunk_document_hierarchy_v2(
            first, child_size=32, child_overlap=4, parent_size=256
        )
        new_chunks = chunk_document_hierarchy_v2(
            second, child_size=32, child_overlap=4, parent_size=256
        )

        self.assertEqual(
            [item.chunk_id for item in old_chunks],
            [item.chunk_id for item in new_chunks],
        )
        self.assertNotEqual(
            [item.content_version for item in old_chunks],
            [item.content_version for item in new_chunks],
        )


if __name__ == "__main__":
    unittest.main()
