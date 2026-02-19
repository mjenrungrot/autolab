## EVIDENCE RECORD FORMAT

Use this format when capturing evidence in stage outputs:

```yaml
- artifact_path: path/to/artifact
  what_it_proves: short claim supported by the artifact
  verifier_output_pointer: path/to/verifier/output/or/log
```

Optional fields (recommended for faster review):

```yaml
- artifact_path: path/to/artifact
  what_it_proves: short claim supported by the artifact
  verifier_output_pointer: path/to/verifier/output/or/log
  excerpt: short quoted snippet from artifact/log (1-3 lines)
  command: exact command used to collect or validate evidence
  timestamp: ISO-8601 capture time
```
