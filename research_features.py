"""Modular use-case toggles for research, growth, and recommend.

Each flag maps to a documented product use case. Profiles set a coherent
bundle; individual CLI flags can still override afterward.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Iterable, List, Optional


PROFILES = ("verify", "grow", "full", "ask")


@dataclass
class ResearchFeatures:
    """On/off switches for claim research and generation behavior.

    substantiate     Verify/rewrite weak cards (core research).
    expand           Fan supported cards into workflow/style children (phase).
    seed             Draft gap claims for missing preferred styles/variables.
    propagate        Emit specialize/expand children from a support plan.
    evidence_adds    Emit sibling mechanism cards from a support plan.
    focused_propagate  Second planner pass when support produced no children.
    require_sourcewise_for_growth  Only add/propagate when sourcewise_supported.
    recommend_gap_draft  Recommend may write candidates on coverage misses.
    """

    substantiate: bool = True
    expand: bool = True
    seed: bool = True
    propagate: bool = True
    evidence_adds: bool = True
    focused_propagate: bool = True
    require_sourcewise_for_growth: bool = False
    recommend_gap_draft: bool = True

    def enabled(self) -> List[str]:
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def disabled(self) -> List[str]:
        return [f.name for f in fields(self) if not getattr(self, f.name)]

    def summary(self) -> str:
        on = ", ".join(self.enabled()) or "(none)"
        off = ", ".join(self.disabled()) or "(none)"
        return f"on=[{on}] off=[{off}]"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def allow_growth(self, truth: Optional[dict]) -> bool:
        """Whether add/propagate side-effects are permitted for this truth."""
        if self.require_sourcewise_for_growth:
            return bool((truth or {}).get("sourcewise_supported"))
        return True


def profile_features(name: str) -> ResearchFeatures:
    """Named bundles for common operator intents."""
    key = (name or "full").strip().lower()
    if key == "verify":
        # Check existing claims only — no new cards from research.
        return ResearchFeatures(
            substantiate=True,
            expand=False,
            seed=False,
            propagate=False,
            evidence_adds=False,
            focused_propagate=False,
            require_sourcewise_for_growth=False,
            recommend_gap_draft=False,
        )
    if key == "grow":
        # Verify + evidence-tied growth; no coverage seeding.
        return ResearchFeatures(
            substantiate=True,
            expand=True,
            seed=False,
            propagate=True,
            evidence_adds=True,
            focused_propagate=True,
            require_sourcewise_for_growth=False,
            recommend_gap_draft=False,
        )
    if key == "ask":
        # Product Q&A: answer from library; gap drafts opt-in via --draft-gaps.
        return ResearchFeatures(
            substantiate=False,
            expand=False,
            seed=False,
            propagate=False,
            evidence_adds=False,
            focused_propagate=False,
            require_sourcewise_for_growth=False,
            recommend_gap_draft=False,
        )
    if key == "full":
        return ResearchFeatures()
    raise ValueError(f"unknown profile {name!r}; choose one of {PROFILES}")


def features_from_args(args: Any) -> ResearchFeatures:
    """Build features from argparse namespace (profile first, then overrides)."""
    profile = getattr(args, "profile", None) or "full"
    feats = profile_features(profile)

    # Continuous explore still respects explicit disables.
    if getattr(args, "no_expand", False):
        feats.expand = False
    if getattr(args, "no_seed", False):
        feats.seed = False
    if getattr(args, "no_propagate", False):
        feats.propagate = False
        feats.focused_propagate = False
    if getattr(args, "no_evidence_adds", False):
        feats.evidence_adds = False
    if getattr(args, "no_focused_propagate", False):
        feats.focused_propagate = False
    if getattr(args, "growth_requires_sourcewise", False):
        feats.require_sourcewise_for_growth = True
    if getattr(args, "no_draft", False):
        feats.recommend_gap_draft = False
    if getattr(args, "draft_gaps", False):
        feats.recommend_gap_draft = True
    return feats


def add_feature_arguments(parser: Any, *, include_recommend: bool = False) -> None:
    """Attach shared modulation flags to a CLI parser."""
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="full",
        help=(
            "Use-case bundle: verify=check only; grow=verify+propagate/adds; "
            "full=grow+seed; ask=recommend-oriented defaults."
        ),
    )
    parser.add_argument("--no-expand", action="store_true", help="Disable expand phase (workflow fan-out rounds).")
    parser.add_argument("--no-seed", action="store_true", help="Disable coverage-gap seeding.")
    parser.add_argument("--no-propagate", action="store_true", help="Disable specialize/expand child drafts.")
    parser.add_argument(
        "--no-evidence-adds",
        action="store_true",
        help="Disable sibling mechanism adds from a support plan.",
    )
    parser.add_argument(
        "--no-focused-propagate",
        action="store_true",
        help="Skip the second planner pass that forces workflow drafts.",
    )
    parser.add_argument(
        "--growth-requires-sourcewise",
        action="store_true",
        help="Only add/propagate when the claim is tagged sourcewise_supported.",
    )
    if include_recommend:
        parser.add_argument(
            "--no-draft",
            action="store_true",
            help="Recommend: do not write gap drafts to candidates.jsonl.",
        )
        parser.add_argument(
            "--draft-gaps",
            action="store_true",
            help="Recommend: explicitly allow gap drafts (overrides ask profile default off only if set with care).",
        )


def describe_use_cases() -> Iterable[Dict[str, str]]:
    """Stable table for docs/tests: id → intent → feedback."""
    return (
        {
            "id": "search",
            "intent": "Find related library cards",
            "command": "embed_pipeline.py query",
            "feedback": "Ranked hits with scores; no writes",
            "toggle": "(always on)",
        },
        {
            "id": "ask",
            "intent": "Answer a product question; optionally draft gaps",
            "command": "embed_pipeline.py recommend [--no-draft]",
            "feedback": "ready advice or gaps list; candidates if drafting on",
            "toggle": "recommend_gap_draft / --no-draft / profile ask",
        },
        {
            "id": "verify_internal",
            "intent": "Does our library already support/contradict this?",
            "command": "… validate|research --evidence internal --profile verify",
            "feedback": "verdict + sources (library only); report note",
            "toggle": "substantiate + evidence internal",
        },
        {
            "id": "verify_external",
            "intent": "Do independent sources support this?",
            "command": "… validate|research --evidence external --profile verify",
            "feedback": "verdict + full sources list + research-reports/",
            "toggle": "substantiate + evidence external",
        },
        {
            "id": "promote_or_fix",
            "intent": "Apply support/conflict to an existing card",
            "command": "passive-research.py --profile verify",
            "feedback": "validate/modify/remove ops; card confidence/status",
            "toggle": "substantiate (adds/propagate off in verify)",
        },
        {
            "id": "evidence_adds",
            "intent": "Mint sibling mechanisms only after support",
            "command": "passive-research.py --profile grow",
            "feedback": "add ops → candidates or cards; draft ≤0.45",
            "toggle": "evidence_adds / --no-evidence-adds",
        },
        {
            "id": "propagate",
            "intent": "Fan a supported claim into workflows/styles",
            "command": "passive-research.py propagate | --profile grow",
            "feedback": "propagate ops with refines/expands links",
            "toggle": "propagate + expand / --no-propagate / --no-expand",
        },
        {
            "id": "seed",
            "intent": "Fill missing preferred styles/variables",
            "command": "passive-research.py --until-done --profile full",
            "feedback": "seed add ops; research-reports/ notes seed phase",
            "toggle": "seed / --no-seed",
        },
        {
            "id": "manual_add",
            "intent": "Operator types a hypothesis",
            "command": "passive-research.py add --claim …",
            "feedback": "draft card in candidates or cards.jsonl",
            "toggle": "(CLI command; not a session flag)",
        },
        {
            "id": "ingest",
            "intent": "Draft many claims from a URL/file/paste",
            "command": "embed_pipeline.py ingest",
            "feedback": "candidates.jsonl batch for review",
            "toggle": "(CLI command)",
        },
    )


__all__ = [
    "PROFILES",
    "ResearchFeatures",
    "add_feature_arguments",
    "describe_use_cases",
    "features_from_args",
    "profile_features",
]
