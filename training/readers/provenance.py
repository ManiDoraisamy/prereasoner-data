"""The approved source names for reader-training records and their evaluation-boundary check."""

EVALUATION_SOURCES = frozenset({"dev", "test", "syn_dev", "syn_test"})
SPIDER_TRAIN_SETS = ("train_spider", "train_others")
ALLOWED_READER_SOURCES = frozenset({
    *SPIDER_TRAIN_SETS, "bird_train", "syn_train", "synsql", "reworded", "lookup", "structure",
})


def assert_no_evaluation_sources(rows, artifact):
    """Allow only named training corpora; reject held-out and unknown provenance labels."""
    unknown = sorted({str(row.get("source", "")) for row in rows
                      if str(row.get("source", "")).casefold() not in ALLOWED_READER_SOURCES})
    if unknown:
        normalized = {source.casefold() for source in unknown}
        kinds = "held-out evaluation" if any(
            source in EVALUATION_SOURCES or source.endswith(("_dev", "_test")) for source in normalized
        ) else "unapproved"
        raise ValueError(f"{artifact} contains {kinds} or unknown sources: {', '.join(unknown)}")
