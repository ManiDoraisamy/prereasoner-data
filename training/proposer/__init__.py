"""SQL proposer training pipeline (see README.md).

`is_validation_db` is the one held-out rule for Spider TRAIN databases: a database whose
md5 bucket is 0 is never used to fit the proposer or the arbiter, so both can be measured on
schemas they have not seen.
"""
import hashlib


def is_validation_db(db_id: str) -> bool:
    return int(hashlib.md5(db_id.encode(), usedforsecurity=False).hexdigest(), 16) % 10 == 0
