## VERIFICATION RITUAL (5 steps -- run before marking stage complete)

1. **Self-check**: Re-read the OUTPUTS (STRICT) list and confirm every required artifact exists and is non-empty.
2. **Schema validation**: Run `autolab verify --stage {{stage}}` and resolve every reported error.
3. **Template-fill fallback**: If `autolab verify` is unavailable, run:
   `{{python_bin}} .autolab/verifiers/template_fill.py --stage {{stage}}`
4. **Placeholder scan**: Search your outputs for `{{`, `<`, `TODO`, `TBD`, `FIXME`. If any remain, replace with concrete values or remove.
5. **Scope audit**: Confirm every file you created or edited is inside the `allowed_edit_dirs` from your runtime context. If you touched anything outside, revert it.

Do not declare the stage complete until all five steps pass. If a verifier fails, fix the artifact and rerun from step 2.

> See stage-specific verification notes in your stage prompt below for additional checks.
