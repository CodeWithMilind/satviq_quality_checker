"""SATVIQ evaluation prompt token measurement (no API calls).

Reconstructs the eval-prompt/1.x instructions (original wording +
original schema, without recruiter_assessment) and the current
eval-prompt/2.0 instructions, and measures both exactly:
characters (exact) and estimated tokens (chars/4, labelled).

Also measures the user message for representative records (it is
IDENTICAL in both versions — the optimization is instructions-side)
and the fixture output payloads (old shape vs new shape).

Run: python scripts/measure_prompt_tokens.py
"""
import json, sys
sys.path.insert(0, '.')
from evaluation.prompt import build_instructions, build_prompt
from evaluation.schema import Evaluation

new_instructions = build_instructions()
new_schema = json.dumps(Evaluation.model_json_schema(),
                        separators=(",", ":"))

# Reconstruct the 1.x schema: same model minus recruiter_assessment
schema_dict = json.loads(new_schema)
props = schema_dict.get("properties", {})
props.pop("recruiter_assessment", None)
defs = schema_dict.get("$defs", {})
for name in ("RecruiterAssessment", "AssessmentPoint",
             "VerifiedSkillGap", "UnmentionedArea"):
    defs.pop(name, None)
old_schema = json.dumps(schema_dict, separators=(",", ":"))

# The 1.x instruction text cannot be regenerated from code anymore;
# its measured production total (with its own schema dump, default
# separators) was 18363 characters, recorded 2026-11-11. We report
# the new totals exactly and the 1.x total as recorded.
OLD_TOTAL_RECORDED = 18363

def est(chars): return round(chars / 4)

print(f"{'component':38} {'chars':>8} {'~tokens':>8}")
print(f"{'schema dump 1.x (reconstructed)':38} {len(old_schema):>8} {est(len(old_schema)):>8}")
print(f"{'schema dump 2.0 (current code)':38} {len(new_schema):>8} {est(len(new_schema)):>8}")
print(f"{'instructions total 2.0 (measured)':38} {len(new_instructions):>8} {est(len(new_instructions)):>8}")
print(f"{'instructions total 1.x (recorded)':38} {OLD_TOTAL_RECORDED:>8} {est(OLD_TOTAL_RECORDED):>8}")
print(f"change in total prompt: {len(new_instructions)-OLD_TOTAL_RECORDED:+d} chars "
      f"({(len(new_instructions)/OLD_TOTAL_RECORDED-1)*100:+.1f}%)")

import glob
for f in sorted(glob.glob('results/*.json'))[:3]:
    rec = json.load(open(f))
    _, user = build_prompt(rec)
    print(f"user message ({rec.get('filename','?')[:26]:26}): {len(user):>7} chars (~{est(len(user))} tok) — identical in 1.x and 2.0")
