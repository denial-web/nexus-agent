"""
Import the a-s-flc-decisions HuggingFace dataset (806 examples) into the
labeling queue as golden evaluation examples.

Usage:
    python scripts/import_asflc_dataset.py [--dataset-id USER/REPO] [--limit N]

Requires: pip install datasets  (HuggingFace datasets library)
"""
import argparse
import json
import logging
import sys
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DATASET_ID = "a-s-flc-decisions"


def import_dataset(dataset_id: str, limit: int | None = None) -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        logger.error("Install the datasets library: pip install datasets")
        sys.exit(1)

    sys.path.insert(0, ".")
    from app.db import SessionLocal
    from app.models.labeling_queue import LabelingItem

    logger.info("Loading dataset '%s' from HuggingFace...", dataset_id)
    try:
        ds = load_dataset(dataset_id, split="train")
    except Exception:
        logger.exception("Failed to load dataset '%s'", dataset_id)
        sys.exit(1)

    if limit:
        ds = ds.select(range(min(limit, len(ds))))

    logger.info("Loaded %d examples", len(ds))

    db = SessionLocal()
    imported = 0

    try:
        for row in ds:
            prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
            response = row.get("response") or row.get("output") or row.get("answer") or ""
            category = row.get("category") or row.get("domain") or "asflc"

            if not prompt:
                continue

            paths_raw = row.get("paths") or row.get("decision_paths")
            if isinstance(paths_raw, str):
                try:
                    paths_raw = json.loads(paths_raw)
                except (json.JSONDecodeError, ValueError):
                    paths_raw = None

            critic_output = {
                "source": "asflc_golden_dataset",
                "category": category,
                "decision_paths": paths_raw,
            }

            for key in ("chosen_path", "confidence", "chain_regret", "loops"):
                if key in row:
                    critic_output[key] = row[key]

            item = LabelingItem(
                id=uuid.uuid4().hex,
                trace_id=f"asflc-import-{uuid.uuid4().hex[:8]}",
                source_node="asflc_import",
                failure_type="golden_example",
                prompt=prompt,
                response=response,
                critic_output=critic_output,
                label="correct_flag",
                reviewer_id="asflc_dataset_import",
                status="labeled",
            )
            db.add(item)
            imported += 1

            if imported % 100 == 0:
                db.commit()
                logger.info("Imported %d/%d...", imported, len(ds))

        db.commit()
        logger.info("Import complete: %d examples imported", imported)
    finally:
        db.close()

    return imported


def main():
    parser = argparse.ArgumentParser(description="Import A-S-FLC golden dataset")
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID, help="HuggingFace dataset ID")
    parser.add_argument("--limit", type=int, default=None, help="Max examples to import")
    args = parser.parse_args()
    import_dataset(args.dataset_id, args.limit)


if __name__ == "__main__":
    main()
