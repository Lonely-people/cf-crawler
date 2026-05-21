#!/usr/bin/env bash
#
# cf-crawler 自动化脚本
# 1. 清除 tutorials/ 和 urls.txt 旧内容
# 2. 按题目拆分 Tutorial → tutorials/
#
# 用法:
#   ./run.sh                  # 默认读取 problems.txt，全新运行
#   ./run.sh problems.txt     # 指定文件
#   ./run.sh --resume         # 从上次中断处继续

set -euo pipefail

RESUME_FLAG=""
PROBLEMS_FILE="problems.txt"

for arg in "$@"; do
    case "$arg" in
        --resume) RESUME_FLAG="--resume" ;;
        *) PROBLEMS_FILE="$arg" ;;
    esac
done

TUTORIALS_DIR="./tutorials"
URLS_FILE="./urls.txt"
CHECKPOINT_FILE=".crawler_checkpoint.json"

echo "=== cf-crawler 自动化 ==="

# ---- 清理（仅在非续传模式下） ----
if [ -z "$RESUME_FLAG" ]; then
    echo "[1/2] 清理旧内容..."
    rm -rf "$TUTORIALS_DIR"/*
    echo "# Codeforces Blog URLs - 每行一个 URL，以 # 开头的行会被忽略" > "$URLS_FILE"
    rm -f "$CHECKPOINT_FILE"
    echo "  ✓ 已清除"
else
    echo "[1/2] 断点续传模式，跳过清理"
fi

# ---- 按题目拆分爬取 ----
echo "[2/2] 按题目拆分爬取 Tutorial..."
python cf_blog_crawler.py -P "$PROBLEMS_FILE" -o "$TUTORIALS_DIR" $RESUME_FLAG

echo "=== 完成 ==="
