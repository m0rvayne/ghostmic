#!/usr/bin/env bash
# Regenerate checksums.txt. Run before tagging a release.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
shasum -a 256 \
    capture.py server.py watcher.py \
    process-audio-tap.swift statusbar.swift zoom-participants.swift \
    install.sh update.sh uninstall.sh requirements.txt \
    > checksums.txt
echo "Wrote checksums.txt for $(wc -l < checksums.txt | tr -d ' ') files"
