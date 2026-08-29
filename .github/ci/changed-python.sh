#!/usr/bin/env bash
set -euo pipefail

base_sha="${1:?base SHA required}"
head_sha="${2:?head SHA required}"

git cat-file -e "${base_sha}^{commit}"
git cat-file -e "${head_sha}^{commit}"
git diff --name-only --diff-filter=ACMR "$base_sha" "$head_sha" -- '*.py'
