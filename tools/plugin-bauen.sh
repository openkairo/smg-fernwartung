#!/bin/sh
# Baut das Plugin als einzelne Datei zum Weitergeben (zip mit plugin.json und dem Skill).
set -eu
cd "$(dirname "$0")/.."
version=$(python3 -c "import json;print(json.load(open('.claude-plugin/plugin.json'))['version'])")
ziel="dist/smg-fernwartung-$version.zip"
mkdir -p dist && rm -f "$ziel"
COPYFILE_DISABLE=1 zip -q -r -X "$ziel" .claude-plugin/plugin.json skills -x '*/__pycache__/*' -x '*.DS_Store'
echo "$ziel"
