# YAML Configuration Versions — SATVIQ Evaluation

This folder holds **frozen snapshots** of the deterministic Quality
Checker's completeness configuration, kept for calibration and
audit. It is documentation/archival only:

- Nothing in the running application reads these files.
- The live configuration remains
  `quality_checker/config/completeness.yaml` — the single source of
  truth for the Quality Checker, loaded exactly as before.
- The evaluation catalogue lives in `evaluation/config.yaml`.
- This is NOT the deleted `yaml files/` version store and NOT a
  revival of the removed YAML Editor AI. There is no automatic
  editing of YAML anywhere in SATVIQ: no LLM writes configuration.

## Contents

| File | What it is |
|---|---|
| `completeness_v1_baseline.yaml` | Byte-identical copy of the production `quality_checker/config/completeness.yaml` as of evaluation config `eval-1.0.0` (md5 `4624cb93e760bc123c44ef9d9886139b`, verified at copy time). This is the preserved original — the rollback target and the regression baseline. |

Every stored analysis records a `yaml_version` string of the form
`completeness@<first 12 hex of the live file's SHA-256>+eval-<evaluation config_version>`,
so any evaluation can be tied to the exact configuration pair that
produced it, and the Analytics page lists the versions in use.

## How configuration changes are made (candidate → activation)

Production YAML (either file above) is only ever changed through
this controlled flow — the same discipline the spec requires:

1. **Candidate** — draft the change in a separate candidate file
   (e.g. `completeness_v2_candidate.yaml` in this folder), with a
   written reason: which real resumes exposed the gap, what was
   mis-detected, and what the change should fix.
2. **Validation** — the candidate must parse, keep the existing
   structure (sections / importance / penalties unchanged unless
   that is the change under review), and pass
   `evaluation.config.validate_config`-style checks where it is an
   evaluation-config change. Duplicate aliases (case-insensitive)
   are rejected.
3. **Regression testing** — run the full test suite plus the
   Quality Checker's section-detection tests against the candidate
   (in a copy of the project with the candidate swapped in), and
   re-check a sample of real stored resumes: previously detected
   sections must not regress, and the targeted gap must actually
   close.
4. **Approval** — a human reviews the candidate diff, the test
   evidence and the reason, and explicitly approves activation.
   No approval, no activation.
5. **Activation** — copy the approved candidate over the live file,
   bump `config_version` in `evaluation/config.yaml` when the
   evaluation catalogue changed, and snapshot the newly live file
   here as the next `completeness_v<N>_*.yaml`. Previous snapshots
   are never deleted — rollback is copying a snapshot back through
   the same flow.

## Candidate aliases awaiting approval (NOT applied)

The headings below were observed on real resumes processed by
SATVIQ (including the generated calibration resumes) where the
deterministic checker did **not** detect the section, while the
content was present. They are proposals for the completeness
YAML's alias lists, recorded here for the next candidate round.
**None of them has been added to any production configuration.**

| Observed heading | Proposed section (QC key) | Evidence |
|---|---|---|
| Candidate Snapshot | `summary` | Synthetic calibration resume (fresher, AI) — heading used for the professional summary; checker reported Summary missing |
| Skill Inventory | `skills` | Same calibration resume — heading used for the skills list; checker reported Skills missing |
| Build Showcase | `projects` | Same calibration resume — heading used for projects; checker reported Projects missing |
| Credential Vault | `certifications` | Same calibration resume — heading used for certifications; checker reported Certifications missing |
| Learning Journey | `education` | Same calibration resume — heading used for education; checker reported Education missing |
| Academic Background | `education` | Common academic-resume heading for education history (spec Part 3.1 equivalence example); not in the current alias list |

Note the distinction the platform now makes: the **Muse AI
evaluation** already recognises these headings by meaning (they are
in its instructions as equivalence examples), so evaluations are
not blocked on this alias gap. Only the deterministic checker's
heading matching would change if these candidates are approved —
which is exactly why they go through the flow above instead of
being edited in directly.
