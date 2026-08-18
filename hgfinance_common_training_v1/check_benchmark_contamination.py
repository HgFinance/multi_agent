#!/usr/bin/env python3
import json, hashlib, re, sys
from pathlib import Path
from difflib import SequenceMatcher

def norm(s):
    return re.sub(r"\s+", " ", str(s).lower()).strip()

def iter_jsonish(path):
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    elif path.suffix == ".json":
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            yield from obj
        elif isinstance(obj, dict):
            for key in ("items","examples","data","questions"):
                if isinstance(obj.get(key), list):
                    yield from obj[key]

def texts(obj):
    out = []
    for k in ("instruction","instruct","question","query","prompt","input","user"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
    if isinstance(obj.get("messages"), list):
        for m in obj["messages"]:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                out.append(m["content"])
    return out

train_file = Path(sys.argv[1])
bench_root = Path(sys.argv[2])
train = []
for obj in iter_jsonish(train_file):
    for t in texts(obj):
        train.append((obj.get("id","?"), norm(t)))

bench = []
for p in bench_root.rglob("*"):
    if p.suffix not in {".json",".jsonl"}:
        continue
    for obj in iter_jsonish(p):
        for t in texts(obj):
            bench.append((str(p), obj.get("id","?"), norm(t)))

exact = []
near = []
bench_map = {}
for p, bid, t in bench:
    bench_map.setdefault(hashlib.sha256(t.encode()).hexdigest(), []).append((p,bid,t))

for tid, t in train:
    h = hashlib.sha256(t.encode()).hexdigest()
    if h in bench_map:
        for p,bid,_ in bench_map[h]:
            exact.append((tid,p,bid))
    # Conservative near-match check only for similarly-sized strings.
    for p,bid,b in bench:
        if min(len(t),len(b)) < 40:
            continue
        ratio = min(len(t),len(b)) / max(len(t),len(b))
        if ratio < 0.75:
            continue
        s = SequenceMatcher(None, t, b).ratio()
        if s >= 0.90:
            near.append((tid,p,bid,round(s,4)))

print(json.dumps({"exact": exact, "near": near, "exact_count": len(exact), "near_count": len(near)}, ensure_ascii=False, indent=2))
sys.exit(1 if exact or near else 0)
