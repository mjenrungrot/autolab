## MACHINE-AUDITABLE CHECKLIST
- [ ] All required placeholders are resolved and concrete (no unresolved tokens or placeholder marker text).
- [ ] Required artifacts listed below are present and syntactically valid.
- [ ] File budget checks (`line_limit`, `char_limit`, `byte_limit`) pass for required artifacts.
- [ ] Run `{{python_bin}} .autolab/verifiers/template_fill.py --stage {{stage}}` and fix failures before finishing.
- [ ] If experiment memory was touched, `docs/todo.md` and `{{iteration_path}}/documentation.md` are consistent and non-contradictory.
