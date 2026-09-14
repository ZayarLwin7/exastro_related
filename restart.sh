#!/bin/bash
# Reliable restart for Exastro_Automate.
cd "$(dirname "$0")"

echo "stopping existing instances…"
for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ "$p" = "$$" ] && continue
    [ "$p" = "$PPID" ] && continue
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in
        *Exastro_Automate*app\.py*)
            kill -9 "$p" 2>/dev/null && echo "  killed $p :: ${cmd:0:60}"
            ;;
    esac
done
sleep 1

if ss -tln 2>/dev/null | grep -q ':9200 '; then
    echo "ERROR: port 9200 still busy"; exit 1
fi

NoHup=../.venv/bin/python
if [ -x "$NoHup" ]; then PY="$NoHup"; else PY="python3"; fi

echo "starting fresh…"
nohup "$PY" app.py > .server.log 2>&1 &
sleep 2

echo "healthz: $(curl -s --max-time 5 http://127.0.0.1:9200/healthz)"
