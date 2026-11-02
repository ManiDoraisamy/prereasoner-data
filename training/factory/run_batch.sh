#!/bin/sh
# One batch of the factory for a side: author, execute, rewrite, second route, sandbox. Every stage skips what is done; every token is journalled.
# usage: run_batch.sh <side> <tabs> [workers]    Set FACTORY_ENGINE_COMMIT for a side pinned apart.
set -e
SIDE=$1
TABS=$2
WORKERS=${3:-20}
F=C:/work/prereasoner-readers-artifacts/factory
NEXT=/c/work/prereasoner-next
READERS=/c/work/prereasoner-readers
RUN="python C:/work/prereasoner-next/training/factory/llm_run.py"
export PYTHONIOENCODING=utf-8 OMP_NUM_THREADS=4
cd $NEXT
python -m training.factory.factory author-plan --side "$SIDE" --tabs "$TABS"
(cd $READERS && $RUN $F/staged/$SIDE/author.requests.jsonl author_$SIDE --workers "$WORKERS" | tail -1)
python -m training.factory.factory execute --side "$SIDE" --workers 5 | tail -1
python -m training.factory.factory rewrite-plan --side "$SIDE"
(cd $READERS && $RUN $F/staged/$SIDE/rewrite.requests.jsonl rewrite_$SIDE --workers "$WORKERS" | tail -1)
python -m training.factory.factory route-plan --side "$SIDE"
(cd $READERS && $RUN $F/staged/$SIDE/route.requests.jsonl route_$SIDE --workers "$WORKERS" | tail -1)
python -m training.factory.factory route-run --side "$SIDE" --workers 5
python training/factory/ledger.py $F/journal/ | tail -1
