# Operational Guidelines & Rules: Direct Action & Minimal Analysis

1. **Zero Redundant Analysis**: Never inspect the same file, directory, or endpoint repeatedly across consecutive turns without an intervening action or code change.
2. **Immediate Action Bias**: Once a symptom or bug is identified (e.g., UI display issue, missing video, link error, storage leak), go directly to editing the root-cause file and validating it immediately.
3. **Quota Conservation**: Avoid issuing chains of speculative diagnostic reads. Perform the smallest necessary lookup, apply the surgical edit, verify, and complete the turn.
