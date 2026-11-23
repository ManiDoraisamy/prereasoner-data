#!/bin/sh
# One batch of the factory for a side: author, execute, rewrite, second route, sandbox. Every stage skips what is done; every token is journalled.
# usage: run_batch.sh <side> <tabs> [workers]    Set FACTORY_ENGINE_COMMIT for a side pinned apart.
set -e
SIDE=$1
TABS=$2
WORKERS=${3:-20}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
F=$(python -c "from training.factory.paths import FACTORY; print(FACTORY.rstrip('/'))")
RUN="python training/factory/llm_run.py"
export PYTHONIOENCODING=utf-8 OMP_NUM_THREADS=4
python -m training.factory.factory author-plan --side "$SIDE" --tabs "$TABS"
$RUN $F/staged/$SIDE/author.requests.jsonl author_$SIDE --workers "$WORKERS" | tail -1
python -m training.factory.factory execute --side "$SIDE" --workers 5 | tail -1
python -m training.factory.factory rewrite-plan --side "$SIDE"
$RUN $F/staged/$SIDE/rewrite.requests.jsonl rewrite_$SIDE --workers "$WORKERS" | tail -1
python -m training.factory.factory route-plan --side "$SIDE"
$RUN $F/staged/$SIDE/route.requests.jsonl route_$SIDE --workers "$WORKERS" | tail -1
python -m training.factory.factory route-run --side "$SIDE" --workers 5
python training/factory/ledger.py $F/journal/ | tail -1
