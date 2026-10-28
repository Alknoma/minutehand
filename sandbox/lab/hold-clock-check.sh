#!/bin/sh
# Checks `runsc debug --hold-clock` / `--release-clock` on the built sandbox, in the lab container (`sandbox/README.md`):
#
#   docker run --rm --privileged -v "$PWD:/work" -v "$PWD/sandbox/dist:/opt/gv:ro" minutehand-lab \
#     sandbox/lab/hold-clock-check.sh
#
# Three sandboxes, each started with --network=none, each line it prints stamped with the host's clock as it arrives:
#   a. a loop printing time.time() and time.monotonic() every 0.2 s, held for 5 s: no line arrives while held, and the
#      first after release reads a moment past the held value, not 5 s past it; beside it a loop that never sleeps,
#      printing both clocks every 20,000 reads, keeps printing while held and reads the held value every time (the
#      VDSO's parameters are held, not only the syscall path);
#   b. `sleep 2` started just before a 10 s hold: it ends about 2 s after release, its own clock reading 2 s;
#   c. `sleep 30` started just before a hold, the held clock advanced 1 h: it ends at the advance (a timer the clock
#      passes fires at once, held or not), its own clock reading 3600 s, and the clock stays held until release.
# Each prints its evidence and PASS or FAIL; the script exits non-zero if any fails.
set -u
RUNSC=${RUNSC:-/opt/gv/runsc}
STATE=${STATE:-/tmp/hold-clock-check}
FAILED=0

now() { date +%s.%N; }
stamp() { python3 -u -c 'import sys, time
for line in sys.stdin: print(f"{time.time():.6f} {line}", end="", flush=True)'; }
# start <name> <command...>: starts a sandbox running the command, its output stamped into $STATE/<name>.out.
start() {
  name=$1; shift
  rm -rf "$STATE/$name"; mkdir -p "$STATE/$name"
  ("$RUNSC" --root "$STATE/$name" --network=none --ignore-cgroups do --name "$name" --state-dir "$STATE/$name" \
     -- "$@" 2>"$STATE/$name.err" | stamp > "$STATE/$name.out") &
}
# ctl <name> <flags...>: one control call; prints only what the sandbox answered (runsc's own "Found sandbox" goes).
ctl() {
  name=$1; shift
  "$RUNSC" --root "$STATE/$name" debug "$@" "$name" 2>"$STATE/$name.ctl.err" >"$STATE/$name.ctl.out"
  status=$?
  grep -v '^Found sandbox' "$STATE/$name.ctl.out"
  [ $status = 0 ] || { echo "runsc debug $* $name: exit $status" >&2; cat "$STATE/$name.ctl.err" >&2; }
  return $status
}
until_line() { # <name> <pattern>: waits for a line matching the pattern, up to 60 s
  i=0
  until grep -q "$2" "$STATE/$1.out" 2>/dev/null; do
    i=$((i + 1)); [ $i -gt 1200 ] && { echo "timed out waiting for '$2' in $1"; cat "$STATE/$1.err"; return 1; }
    sleep 0.05
  done
}
until_blocked() { # <name>: waits until a task is blocked with a deadline (a sleep has started), up to 60 s
  i=0
  until ctl "$1" --deadlines 2>/dev/null | grep -q '"timed":[1-9]'; do
    i=$((i + 1)); [ $i -gt 1200 ] && { echo "timed out waiting for a blocked task in $1"; return 1; }
    sleep 0.05
  done
}
stop() { "$RUNSC" --root "$STATE/$1" kill "$1" KILL 2>/dev/null; wait 2>/dev/null; }
verdict() { if [ "$1" = 0 ]; then echo "$2: PASS"; else echo "$2: FAIL"; FAILED=1; fi; }
rm -rf "$STATE"; mkdir -p "$STATE"

echo "=== a. a printing loop, held 5 s"
start a sh -c 'python3 -u -c "
import time
while True:
    for _ in range(20_000):
        rt, mono = time.time_ns(), time.monotonic_ns()
    print(f\"spin rt={rt} mono={mono}\", flush=True)
" & exec python3 -u -c "
import time
while True:
    print(f\"tick rt={time.time():.3f} mono={time.monotonic():.3f}\", flush=True)
    time.sleep(0.2)
"'
until_line a tick && sleep 2
ctl a --hold-clock; HOLD=$(now); ctl a --deadlines | tee "$STATE/a.held1"
sleep 5
ctl a --deadlines | tee "$STATE/a.held2"; RELEASE=$(now); ctl a --release-clock
sleep 2; stop a
python3 - "$STATE/a.out" "$HOLD" "$RELEASE" "$STATE/a.held1" "$STATE/a.held2" <<'PY'
import json, sys
import re
lines = re.findall(r"^(\S+) tick (rt=\S+) (mono=\S+)$", open(sys.argv[1]).read(), re.M)
hold, release = float(sys.argv[2]), float(sys.argv[3])
held1, held2 = (json.loads(open(p).read()) for p in sys.argv[4:6])
ticks = [(float(h), float(rt[3:]), float(mono[5:])) for h, rt, mono in lines]
before = [t for t in ticks if t[0] < hold]
during = [t for t in ticks if hold + 0.05 < t[0] < release]
after = [t for t in ticks if t[0] >= release]
last, first = before[-1], after[0]
print(f"last tick before hold: host {last[0]:.3f}  sandbox rt {last[1]:.3f} mono {last[2]:.3f}")
print(f"deadlines while held, 5 s apart: now_ns {held1['now_ns']} and {held2['now_ns']}, held={held1['held']}")
print(f"ticks while held (host {hold:.3f}..{release:.3f}): {len(during)}")
print(f"first tick after release: host +{first[0] - release:.3f}s  sandbox rt {first[1]:.3f} (+{first[1] - last[1]:.3f}s"
      f" since the last tick before hold) mono +{first[2] - last[2]:.3f}s")
print(f"ticks after release: {len(after)} in {after[-1][0] - release:.1f}s of host time")
spins = re.findall(r"^(\S+) spin rt=(\d+) mono=(\d+)$", open(sys.argv[1]).read(), re.M)
spins = [(float(h), int(rt), int(mono)) for h, rt, mono in spins]
# Lines stamped after the hold that were written before it landed read less than the held value: from the first that
# reads it, every read while held must.
held_spins = [s for s in spins if hold < s[0] < release]
held_spins = held_spins[next((i for i, s in enumerate(held_spins) if s[1] >= held1["now_ns"]), len(held_spins)):]
held_rts, held_monos = {s[1] for s in held_spins}, {s[2] for s in held_spins}
print(f"spinning reads while held: {len(held_spins)} lines over {held_spins[-1][0] - held_spins[0][0]:.1f}s of host time,"
      f" {len(held_rts)} distinct rt ({min(held_rts)}) and {len(held_monos)} distinct mono; held now_ns {held1['now_ns']}")
ok = (not during and held1["now_ns"] == held2["now_ns"] and held1["held"] and first[1] - last[1] < 1.5
      and first[2] - last[2] < 1.5 and first[0] - release < 2.0 and len(after) >= 2
      and len(held_spins) >= 3 and held_rts == {held1["now_ns"]} and len(held_monos) == 1)
sys.exit(0 if ok else 1)
PY
verdict $? a

echo "=== b. sleep 2 started just before a 10 s hold"
start b sh -c 'echo "start $(date +%s.%N)"; sleep 2; echo "done $(date +%s.%N)"'
until_line b start; until_blocked b; HOLD=$(now); ctl b --hold-clock; ctl b --deadlines > "$STATE/b.held"
sleep 10
grep -q " done " "$STATE/b.out"; DONE_WHILE_HELD=$((1 - $?))
RELEASE=$(now); ctl b --release-clock
until_line b " done "; stop b
python3 - "$STATE/b.out" "$HOLD" "$RELEASE" "$DONE_WHILE_HELD" "$STATE/b.held" <<'PY'
import json, sys
lines = {l.split()[1]: (float(l.split()[0]), float(l.split()[2])) for l in open(sys.argv[1])}
hold, release, done_while_held = float(sys.argv[2]), float(sys.argv[3]), sys.argv[4] == "1"
held = json.loads(open(sys.argv[5]).read())
start, done = lines["start"], lines["done"]
left = held["earliest_ns"] / 1e9
print(f"started host {start[0]:.3f}, held at host +{hold - start[0]:.3f}s with {left:.3f}s of the sleep left, held 10 s")
print(f"done while held: {done_while_held}")
print(f"done at host +{done[0] - release:.3f}s after release; sandbox clock between start and done: {done[1] - start[1]:.3f}s")
ok = (not done_while_held and held["held"] and left - 0.1 < done[0] - release < left + 1.0
      and 2.0 <= done[1] - start[1] < 3.0)
sys.exit(0 if ok else 1)
PY
verdict $? b

echo "=== c. sleep 30 started before a hold, the held clock advanced 1 h"
start c sh -c 'echo "start $(date +%s.%N)"; sleep 30; echo "done $(date +%s.%N)"; exec sleep 86400'
until_line c start; until_blocked c; ctl c --hold-clock
ADVANCE=$(now); ctl c --advance-clock=1h
until_line c " done "; ctl c --deadlines | tee "$STATE/c.held"
sleep 2; RELEASE=$(now); ctl c --release-clock; ctl c --deadlines | tee "$STATE/c.released"
stop c
python3 - "$STATE/c.out" "$ADVANCE" "$RELEASE" "$STATE/c.held" "$STATE/c.released" <<'PY'
import json, sys
lines = {l.split()[1]: (float(l.split()[0]), float(l.split()[2])) for l in open(sys.argv[1])}
advance, release = float(sys.argv[2]), float(sys.argv[3])
held, released = (json.loads(open(p).read()) for p in sys.argv[4:6])
start, done = lines["start"], lines["done"]
print(f"done at host +{done[0] - advance:.3f}s after the advance ({release - done[0]:.3f}s before release);"
      f" sandbox clock between start and done: {done[1] - start[1]:.3f}s")
print(f"held={held['held']} now_ns {held['now_ns']} after the advance; released now_ns {released['now_ns']}"
      f" (+{(released['now_ns'] - held['now_ns']) / 1e9:.3f}s over the 2 s still held)")
ok = (done[0] - advance < 1.0 and 3600 <= done[1] - start[1] < 3605 and held["held"] and not released["held"]
      and released["now_ns"] - held["now_ns"] < 0.5e9)
sys.exit(0 if ok else 1)
PY
verdict $? c

exit $FAILED
