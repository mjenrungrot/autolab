#!/usr/bin/env bash
set -euo pipefail

python -m pkg.train --config configs/train_golden.yaml
