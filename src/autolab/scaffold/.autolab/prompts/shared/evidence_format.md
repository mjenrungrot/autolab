## EVIDENCE RECORD FORMAT

Use this format when capturing evidence in stage outputs:

```yaml
- artifact_path: path/to/artifact
  what_it_proves: short claim supported by the artifact
  verifier_output_pointer: path/to/verifier/output/or/log
  excerpt: short quoted snippet from artifact/log (1-3 lines) -- required when available
  command: exact command used to collect or validate evidence -- required when available
```

Optional fields:

```yaml
  timestamp: ISO-8601 capture time
```

**Rule**: `excerpt` and `command` are required whenever the evidence is available. Omit only when the source does not support excerpts (e.g. binary artifacts) or when no command was executed to produce the evidence.
