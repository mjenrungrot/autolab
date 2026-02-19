## Change Summary

- Added calibrated augmentation schedule configuration and wiring in training pipeline.
- Added deterministic seed handling for augmentation schedule tests.

## Files Updated

- `src/pkg/train.py`
- `src/pkg/augment.py`
- `configs/train_golden.yaml`

## Verifier Outputs

- tests: pass
- dry_run: pass
- schema: pass

## Commands Executed

- `python -m pytest -q`
- `python -m pkg.train --config configs/train_golden.yaml --dry-run`
- `python .autolab/verifiers/template_fill.py --stage implementation`

## Dry Run

- command: `python -m pkg.train --config configs/train_golden.yaml --dry-run`
- status: pass
- evidence: `experiments/plan/iter_golden/implementation/commands.log`

## Evidence Paths

- `.autolab/logs/run.log`
- `experiments/plan/iter_golden/implementation/commands.log`

## Risks and Follow-ups

- Monitor minority-class precision drift in post-run analysis.
