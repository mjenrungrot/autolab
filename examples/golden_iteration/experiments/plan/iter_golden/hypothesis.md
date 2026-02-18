# Hypothesis Statement

Applying a calibrated augmentation schedule should improve validation accuracy without destabilizing training.

## Motivation
- Current baseline underfits minority classes and plateaus early.

## Scope In
- Training-time augmentations and schedule parameters.

## Scope Out
- Architecture changes, dataset relabeling, and inference-time ensembling.

## Primary Metric
PrimaryMetric: validation_accuracy; Unit: %; Success: baseline +1.0%

## Expected Delta
- +1.0% to +1.8% absolute validation accuracy.

## Operational Success Criteria
- No run failures.
- Stability checks remain within baseline tolerance.

## Risks and Failure Modes
- Over-augmentation may reduce precision.

## Constraints for Design Stage
- Keep compute mode local.
- Keep runtime under 1 hour.
