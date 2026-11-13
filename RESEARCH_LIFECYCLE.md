# Passive research lifecycle: refactor notes

## Current controller and first extraction

`passive-research.py` remains the CLI and end-to-end session controller. Its command parsing, research loop, operation generation, and offline `_self_check()` are kept intact in this incremental refactor. The first extracted utility is `research_policy.py`: a deterministic, side-effect-free policy module with independently testable decisions for claim creation, revision, propagation, archival, and proposal structure.

This is deliberately a small first step—not yet a wholesale move of retrieval, model clients, or persistence. The next refactors can move those boundaries one at a time while preserving the existing CLI contract.

## Lifecycle contract

1. Select work from the library.
2. Retrieve library neighbors and optional external evidence.
3. Assess the claim as support, conflict, insufficient, or an operational error.
4. Ask the planner for proposed operations.
5. Enforce lifecycle policy with explicit evidence and scope gates.
6. Verify operation shape and relationships.
7. Confirm and stage/commit; archive is reversible, never a hard delete.
8. Revisit unresolved claims within the bounded session loop.

## Confidence fields are not interchangeable

Evidence `truth.score` measures the critic's assessment and is used by evidence gates. A draft card's `confidence` is only a provisional library value constrained by the draft cap (0.45). Verification must never treat one as the other. The policy API therefore receives the truth result separately from the card payload.

## Policy rules

- **Create:** supported evidence meets the support threshold; claim is nonempty, novel, and has useful scope/variables; draft confidence stays under the cap.
- **Modify:** evidence supports the corrected wording and the text materially changes.
- **Specialize:** supported evidence plus a meaningful style, phase, or workflow scope delta.
- **Expand:** supported evidence plus broader style coverage or an adjacent variable.
- **Archive:** conflict meets the conflict threshold and the claim is not repairable. Insufficient evidence is never deletion evidence.
- **Verify:** structural checks are separate from truth assessment; draft confidence is checked only against its cap.

The manual/explicit propagation path may mark children as hypotheses, but this exemption must be explicit (`explicit_hypothesis=True`) and does not upgrade the child's evidence status.

## Offline checks

Run the policy unit tests and its smoke check without web, embedding, or model credentials:

```bash
python -m unittest tests.test_research_policy
python research_policy.py
```

The existing controller's `_self_check()` remains a separate integration-level smoke check. These tests are not claimed to have been executed by the GitHub file-writing tool; run them in the project environment before relying on the refactor.
