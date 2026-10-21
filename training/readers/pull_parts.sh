#!/usr/bin/env bash
# Pull a finished stage-2 run off a live lease's pod in 8 parallel parts, when a slow single stream would lose the
# lease's deadline (training.readers.lease downloads out/ in one stream). Waits until the pod's
# out/stage2/small.pt exists and out/stage2/model.pt is gone (the shrink finished), then copies best.json,
# log.jsonl, calibration_outputs.jsonl and small.pt, and checks small.pt's md5 against the pod's.
# Expand small.pt afterwards with python -m training.readers.shrink_state expand.
# usage: bash training/readers/pull_parts.sh <host> <port> <local run dir>
set -u
HOST=root@$1
PORT=$2
D=$3
KEY="$HOME/.ssh/runpod_prereasoner"
S="ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o LogLevel=ERROR $HOST"
O="-P $PORT -o StrictHostKeyChecking=no -o LogLevel=ERROR -o ConnectTimeout=20"
mkdir -p "$D/stage2"
until $S "test -s /workspace/out/stage2/small.pt && test ! -e /workspace/out/stage2/model.pt" 2>/dev/null; do sleep 30; done
echo "$(date +%H:%M:%S) shrunk weights ready"
for f in best.json log.jsonl calibration_outputs.jsonl; do
  for t in 1 2 3; do scp -i "$KEY" $O "$HOST:/workspace/out/stage2/$f" "$D/stage2/$f" && break; sleep 3; done
done
$S "cd /workspace/out/stage2 && md5sum small.pt && split -n 8 -d -a 1 small.pt /workspace/part." > "$D/stage2/small.md5"
for i in 0 1 2 3 4 5 6 7; do
  (for t in 1 2 3 4 5 6; do scp -i "$KEY" $O "$HOST:/workspace/part.$i" "$D/stage2/" && break; sleep 3; done) &
  sleep 2
done
wait
cat "$D"/stage2/part.? > "$D/stage2/small.pt.parts" && rm "$D"/stage2/part.?
remote_md5=$(cut -c1-32 "$D/stage2/small.md5")
local_md5=$(md5sum "$D/stage2/small.pt.parts" | cut -c1-32)
if [ "$remote_md5" = "$local_md5" ]; then
  mv "$D/stage2/small.pt.parts" "$D/stage2/small.pt"
  echo "$(date +%H:%M:%S) pulled, md5 $local_md5 matches"
else
  echo "$(date +%H:%M:%S) md5 mismatch: pod $remote_md5 local $local_md5"
fi
