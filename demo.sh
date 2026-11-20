#!/bin/sh
# Полное демо одной командой: два релиза мок-API, прогон набора, проверка KPI.
#   с Python 3.11+:  sh demo.sh
#   через Docker:    docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh demo.sh
set -u
cd "$(dirname "$0")"

python -m mockapi --release 3.7.0     --port 8787 --latency 0.3 &               STABLE=$!
python -m mockapi --release 3.8.0-rc1 --port 8788 --latency 0.3 --flaky 0.05 & RC=$!
trap 'kill $STABLE $RC 2>/dev/null' EXIT
sleep 1

echo "== 1. Релиз 3.7.0 (эталонный): ожидаем, что всё совпадёт"
python -m oilcheck run --workers 8 --out reports/stable

echo "== 2. Релиз 3.8.0-rc1: в продукте 4 регрессии, 5 % ответов API — 503"
OILCHECK_BASE_URL=http://127.0.0.1:8788 python -m oilcheck run --workers 8 --repeat 2 --out reports/rc

echo "== 3. KPI: внесённые отклонения на всех трёх слоях должны ловиться (цель ≥ 90 %)"
python -m oilcheck mutate --count 300 --seed 7 --mode api --out reports/kpi

echo "Отчёты: reports/stable/report.html, reports/rc/report.html, reports/kpi/mutation.md"
