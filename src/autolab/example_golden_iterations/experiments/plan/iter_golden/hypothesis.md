# Hypothesis Statement

Applying a calibrated augmentation schedule should improve validation accuracy without destabilizing training.

## Research Context and Baseline Evidence

- Prior runs in this repo show minority-class recall lagging while overall validation accuracy plateaus.
- Baseline training recipe is stable, so this iteration isolates augmentation schedule effects rather than architecture changes.

## Methodology Workflow

1. `configs/train_golden.yaml` + baseline data split -> run baseline training recipe -> produce baseline metrics artifact (`experiments/plan/iter_golden/runs/20260201T120000Z_baseline/metrics.json`).
1. Baseline config + calibrated augmentation schedule -> run variant training recipe -> produce variant metrics artifact (`experiments/plan/iter_golden/runs/20260201T120000Z_variant/metrics.json`).
1. Baseline and variant metrics -> aggregate primary metric deltas and stability checks -> produce analysis summary (`analysis/summary.md`).

## Experimental Units and Data Scope

- Unit of analysis: one training run using the configured dataset split.
- Data scope: existing training/validation split referenced by the golden config.
- Exclusions: no relabeling, no new data ingestion, no inference-time ensembling.

## Intervention and Control

- Intervention: calibrated augmentation schedule in the training pipeline.
- Control: current production-like training recipe with existing augmentation behavior.

## Measurement and Analysis Plan

PrimaryMetric: validation_accuracy; Unit: %; Success: baseline +1.0%

- Aggregation rule: compare mean validation accuracy from completed runs.
- Baseline comparison rule: variant minus baseline validation accuracy on the same split.
- Success threshold interpretation: hypothesis passes when delta meets or exceeds +1.0% and stability checks remain acceptable.

## Reproducibility Commitments

- Seed strategy: use fixed seeds documented in design and launch artifacts.
- Config provenance: all experiment settings flow from versioned config files in this repository.
- Data/version assumptions: dataset split and preprocessing remain unchanged from baseline.
- Artifact expectations: each run emits `run_manifest.json`, `metrics.json`, and summary artifacts required by downstream stages.

## Implementation Grounding

- Expected implementation surfaces: `src/pkg/train.py`, `src/pkg/augment.py`, and `configs/train_golden.yaml`.
- Dependency assumptions: current training entrypoint and metric logging paths remain available.
- Feasibility risks: over-augmentation can degrade precision even if headline accuracy improves.

## Scope In

- Training-time augmentation logic and schedule parameters.
- Configuration wiring needed to compare baseline and calibrated schedules.

## Scope Out

- Architecture changes.
- Dataset relabeling.
- Inference-time ensembling.

## Expected Delta

- +1.0% to +1.8% absolute validation accuracy.

## Operational Success Criteria

- No run failures.
- Stability checks remain within baseline tolerance.

## Risks and Failure Modes

- Over-augmentation may reduce precision.

## Constraints for Design Stage

- Keep compute mode local for this iteration.
- Keep walltime under 1 hour.
- Preserve baseline-vs-variant comparison logic and explicit run artifact generation.

## Structured Metadata (machine-parsed)

- target_delta: +1.0
- metric_name: validation_accuracy
- metric_mode: maximize
