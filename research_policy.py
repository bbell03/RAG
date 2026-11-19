"""Deterministic claim lifecycle rules for passive-research.py.

No models, retrieval, or disk I/O. Evidence score gates create/modify/propagate/
archive; card confidence is only checked against the draft cap (0.45).

See passive-research.md for the operator-facing constraints table.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Set


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ClaimLifecyclePolicy:
    support_threshold: float = 0.60
    propagation_threshold: float = 0.55
    archive_conflict_threshold: float = 0.35
    draft_confidence_cap: float = 0.45

    def allow_create(
        self,
        card: dict,
        truth: dict,
        known_claims: Optional[Set[str]] = None,
        duplicate_fn: Optional[Callable[[str], bool]] = None,
    ) -> PolicyDecision:
        """Allow a new evidence-derived draft only with support, novelty, and scope."""
        claim = _norm(card.get("claim"))
        verdict, score = _truth(truth)
        if verdict != "support" or score < self.support_threshold:
            return PolicyDecision(False, "creation requires support above threshold")
        if not claim:
            return PolicyDecision(False, "claim text is empty")
        if known_claims and claim in known_claims:
            return PolicyDecision(False, "claim duplicates a known normalized sentence")
        if duplicate_fn and duplicate_fn(card.get("claim", "")):
            return PolicyDecision(False, "semantic duplicate detected")
        if not any(
            card.get(field)
            for field in ("style_scope", "phase", "relevance_targets", "variables")
        ):
            return PolicyDecision(False, "claim lacks useful scope or variables")
        if _confidence(card) > self.draft_confidence_cap:
            return PolicyDecision(False, "draft confidence exceeds configured cap")
        return PolicyDecision(True, "supported, novel, scoped draft")

    def allow_modify(self, old_claim: str, new_claim: str, truth: dict) -> PolicyDecision:
        verdict, score = _truth(truth)
        if verdict != "support" or score < self.support_threshold:
            return PolicyDecision(False, "revision requires supporting evidence above threshold")
        if not _norm(new_claim):
            return PolicyDecision(False, "revised claim is empty")
        if _norm(old_claim) == _norm(new_claim):
            return PolicyDecision(False, "revised claim is unchanged")
        return PolicyDecision(True, "supported wording change")

    def allow_propagate(
        self,
        parent: dict,
        child: dict,
        direction: str,
        truth: dict,
        known_claims: Optional[Set[str]] = None,
        explicit_hypothesis: bool = False,
    ) -> PolicyDecision:
        """Require evidence for normal propagation; manual hypothesis mode is explicit."""
        verdict, score = _truth(truth)
        if not explicit_hypothesis and (
            verdict != "support" or score < self.propagation_threshold
        ):
            return PolicyDecision(False, "propagation requires supported evidence above threshold")

        parent_styles = set(_labels(parent.get("style_scope")))
        child_styles = set(_labels(child.get("style_scope")))
        parent_phase = set(_labels(parent.get("phase")))
        child_phase = set(_labels(child.get("phase")))
        parent_targets = set(_labels(parent.get("relevance_targets")))
        child_targets = set(_labels(child.get("relevance_targets")))
        parent_variables = set(_labels(parent.get("variables")))
        child_variables = set(_labels(child.get("variables")))

        if direction == "specialize":
            scope_delta = (
                child_styles - parent_styles
                or child_phase - parent_phase
                or child_targets - parent_targets
            )
            if not scope_delta:
                return PolicyDecision(False, "specialization does not change scope")
        elif direction == "expand":
            broadens_style = "general" in child_styles and "general" not in parent_styles
            adds_variable = bool(child_variables - parent_variables)
            adds_style = bool(child_styles - parent_styles)
            if not (broadens_style or adds_variable or adds_style):
                return PolicyDecision(False, "expansion adds no style or variable coverage")
        else:
            return PolicyDecision(False, "direction must be specialize or expand")

        claim = _norm(child.get("claim"))
        if not claim:
            return PolicyDecision(False, "child claim is empty")
        if _norm(parent.get("claim")) == claim or (known_claims and claim in known_claims):
            return PolicyDecision(False, "child duplicates parent or known claim")
        if _confidence(child) > self.draft_confidence_cap:
            return PolicyDecision(False, "propagated draft confidence exceeds cap")
        return PolicyDecision(True, "meaningful propagation draft")

    def allow_archive(self, truth: dict, repairable: bool = False) -> PolicyDecision:
        verdict, score = _truth(truth)
        if verdict != "conflict":
            return PolicyDecision(False, "archival requires a conflict verdict")
        if score < self.archive_conflict_threshold:
            return PolicyDecision(False, "conflict score is below archival threshold")
        if repairable:
            return PolicyDecision(False, "claim appears repairable; revise rather than archive")
        return PolicyDecision(True, "unrepairable conflict meets archival threshold")

    def verify_operation(self, op: dict, parent: Optional[dict] = None) -> PolicyDecision:
        """Structural verification only; evidence-specific gates run separately."""
        kind = op.get("op")
        card = op.get("card") or {}
        if kind in ("add", "propagate"):
            if not card.get("id") or not _norm(card.get("claim")):
                return PolicyDecision(False, "draft requires id and non-empty claim")
            if card.get("status") != "draft":
                return PolicyDecision(False, "new learning cards must remain drafts")
            if _confidence(card) > self.draft_confidence_cap:
                return PolicyDecision(False, "draft confidence exceeds configured cap")
            if kind == "propagate" and not (
                op.get("parent_id") or parent or card.get("refines") or card.get("expands")
            ):
                return PolicyDecision(False, "propagation must retain a parent relationship")
            return PolicyDecision(True, "draft structure verified")
        if kind in ("validate", "modify"):
            if not card.get("id") or not _norm(card.get("claim")):
                return PolicyDecision(False, "existing-card update requires id and claim")
            return PolicyDecision(True, "existing-card update structure verified")
        if kind == "remove":
            if not (op.get("card_id") or card.get("id")):
                return PolicyDecision(False, "archive operation requires card id")
            return PolicyDecision(True, "archive operation structure verified")
        return PolicyDecision(False, "unknown operation")


def _truth(truth: Optional[dict]):
    truth = truth or {}
    try:
        score = max(0.0, min(1.0, float(truth.get("score") or 0.0)))
    except (TypeError, ValueError):
        score = 0.0
    return truth.get("verdict"), score


def _confidence(card: dict) -> float:
    try:
        return max(0.0, min(1.0, float(card.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _norm(value) -> str:
    return " ".join(str(value or "").lower().split())


def _labels(value) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    return {str(item).strip() for item in value if str(item).strip()}


def _self_check():
    """Offline smoke check for evidence/draft confidence separation and gates."""
    policy = ClaimLifecyclePolicy()
    supported = {"verdict": "support", "score": 0.9}
    child = {
        "id": "child",
        "claim": "Scoped learning claim",
        "confidence": 0.4,
        "status": "draft",
        "phase": ["shaping"],
    }
    assert policy.allow_create(child, supported).allowed
    assert policy.verify_operation({"op": "add", "card": child}).allowed
    assert not policy.allow_create(child, {"verdict": "support", "score": 0.2}).allowed
    assert not policy.allow_archive({"verdict": "conflict", "score": 0.8}, repairable=True).allowed
    print("research lifecycle policy self-check ok")


if __name__ == "__main__":
    _self_check()
