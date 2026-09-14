from scripts.validate_sources import DEFAULT_CSV, DEFAULT_JSONL, load_jsonl, validate_inventory
from scenic_pilot.knowledge.ingestion import load_documents


def test_curated_source_inventory_passes_quality_gate() -> None:
    records = load_jsonl(DEFAULT_JSONL)
    assert len(records) == 50
    assert validate_inventory(records, DEFAULT_CSV) == []


def test_demo_and_curated_data_are_separated() -> None:
    records = load_jsonl(DEFAULT_JSONL)
    assert all(record["demo"] is False for record in records)
    assert all("/demo/" not in str(DEFAULT_JSONL).replace("\\", "/") for record in records)


def test_curated_inventory_is_compatible_with_ingestion() -> None:
    documents = load_documents(DEFAULT_JSONL)
    assert len(documents) == 50
    assert all(document.scenic_id == "zhuhai-chimelong" for document in documents)
    assert all(document.demo is False for document in documents)
