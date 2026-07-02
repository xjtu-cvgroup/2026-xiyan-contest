#!/bin/bash
# 打包提交用 ZIP：start.sh 必须位于 ZIP 根目录（任务书 10.1）
set -e
cd "$(dirname "$0")"

OUT_DIR=dist
OUT=$OUT_DIR/gameclient.zip
mkdir -p "$OUT_DIR"
rm -f "$OUT"

zip -r "$OUT" start.sh main.py README.md lychee lychee_basic_client \
    -x "*.pyc" -x "*__pycache__*"

echo "----"
unzip -l "$OUT"
echo "打包完成: $OUT"
