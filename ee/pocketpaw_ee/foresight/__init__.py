# ee/pocketpaw_ee/foresight/__init__.py
# Updated: 2026-05-25 (feat/foresight-v14-decision-graph-stub) — RFC 08
# §14.4 wiring:
#   - Lazy-export surface grew: DecisionGraphRef (protocol),
#     NoOpDecisionGraphRef (v0.5 default implementation),
#     SYNTHETIC_PRECEDENT_PREFIX (id-format constant for downstream
#     code that distinguishes synthetic from real precedent ids).
# Updated: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4:
#   - Bumped __version__ to 0.4.0 — aggregator primitives shipped at
#     ``foresight/aggregator.py`` and the cloud-side backtest gate
#     (``ee/cloud/foresight/{service,router,dto,domain}.py``) consumes
#     them as the unlock criterion for forward sims (RFC §13.1 gate 7).
#   - Lazy-export surface grew: accuracy_meets_threshold,
#     ThresholdDecision, summarize_by, group_pairs_by,
#     per_scenario_template_summary, per_anchor_namespace_summary,
#     rolling_accuracy, rolling_accuracy_series, confidence_drift,
#     ConfidenceDrift, modal_outcome_distribution,
#     ModalOutcomeDistribution, index_predictions.
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3:
#   - Bumped __version__ to 0.3.0 — CAMEL hard-dep promotion, OASIS
#     substrate wiring (AgentGraph + PawSocialAgent), real LiteLLM
#     fallback, tier_pool builder, calibration loop, CAMEL
#     FunctionTool → SDK Permissions translator.
#   - Lazy-export surface grew: TierMix, build_tier_pool,
#     tier_distribution, make_paw_social_agent, PredictionRecord,
#     PredictionBuffer, CalibrationPair, CalibrationSummary,
#     pair_against_reality, aggregate_pairs, apply_correction,
#     build_prediction_record, Correction, CORRECTION_CAP,
#     translate_camel_tools_to_sdk_overrides.
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2:
#   - Bumped __version__ to 0.2.0 to reflect the OASIS substrate
#     vendoring + adapter expansion + PawAgent wrapping.
#   - Added LiteLLMFallbackBackend to the lazy-export surface (stub at
#     v0.2; PR 3 wires the real proxy).
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Foresight module — the "rehearse the future" engine for the Paw IS.
#
# Public surface (v0.3, RFC 08 PR 3):
#   - ForesightWorld   — Fabric-backed world stub with optional
#                        OASIS AgentGraph (world.py)
#   - SoulSeededPersona — soul-seeded persona w/ PawAgent fidelity
#                          (persona.py)
#   - make_paw_social_agent — OASIS SocialAgent subclass factory
#                              (persona.py · PR 3)
#   - ClaudeCodeBackend — CC SDK ↔ CAMEL BaseModelBackend adapter
#                          (llm/adapter.py)
#   - LiteLLMFallbackBackend — real litellm.acompletion proxy
#                                (llm/adapter.py · PR 3)
#   - translate_camel_tools_to_sdk_overrides — FunctionTool → SDK
#       Permissions translator (llm/adapter.py · PR 3)
#   - TierMix, build_tier_pool, tier_distribution — tier-pool
#       primitives (llm/tier_pool.py · PR 3)
#   - PredictionRecord, PredictionBuffer, CalibrationPair,
#     CalibrationSummary, pair_against_reality, aggregate_pairs,
#     apply_correction, Correction, CORRECTION_CAP,
#     build_prediction_record — calibration loop primitives
#       (calibration.py · PR 3)
#   - run_scenario     — single-scenario smoke entrypoint
#                          (scenarios/runner.py)

from __future__ import annotations

__version__ = "0.4.0"

__all__ = [
    "CORRECTION_CAP",
    "CalibrationPair",
    "CalibrationSummary",
    "ClaudeCodeBackend",
    "ConfidenceDrift",
    "Correction",
    "DecisionGraphRef",
    "DeterministicFakeBackend",
    "ForesightWorld",
    "LiteLLMFallbackBackend",
    "ModalOutcomeDistribution",
    "NoOpDecisionGraphRef",
    "OceanDrift",
    "PredictionBuffer",
    "PredictionRecord",
    "RunResult",
    "SYNTHETIC_PRECEDENT_PREFIX",
    "ScenarioConfig",
    "SoulSeededPersona",
    "ThresholdDecision",
    "TierMix",
    "__version__",
    "accuracy_meets_threshold",
    "aggregate_pairs",
    "apply_correction",
    "build_prediction_record",
    "build_tier_pool",
    "confidence_drift",
    "group_pairs_by",
    "index_predictions",
    "make_paw_social_agent",
    "modal_outcome_distribution",
    "pair_against_reality",
    "per_anchor_namespace_summary",
    "per_scenario_template_summary",
    "rolling_accuracy",
    "rolling_accuracy_series",
    "run_scenario",
    "summarize_by",
    "tier_distribution",
    "translate_camel_tools_to_sdk_overrides",
]


def __getattr__(name: str):  # pragma: no cover — lazy import shim
    """Lazy re-export so ``import pocketpaw_ee.foresight`` doesn't pay
    the full import cost when callers only need the version. CAMEL is a
    heavy import (50+ backend adapters); we don't want a top-level
    foresight import to drag it in if a caller just wants ``__version__``.
    """
    if name == "ForesightWorld":
        from pocketpaw_ee.foresight.world import ForesightWorld

        return ForesightWorld
    if name in {"DecisionGraphRef", "NoOpDecisionGraphRef", "SYNTHETIC_PRECEDENT_PREFIX"}:
        from pocketpaw_ee.foresight.decision_graph_ref import (
            SYNTHETIC_PRECEDENT_PREFIX,
            DecisionGraphRef,
            NoOpDecisionGraphRef,
        )

        return {
            "DecisionGraphRef": DecisionGraphRef,
            "NoOpDecisionGraphRef": NoOpDecisionGraphRef,
            "SYNTHETIC_PRECEDENT_PREFIX": SYNTHETIC_PRECEDENT_PREFIX,
        }[name]
    if name in {"SoulSeededPersona", "OceanDrift", "make_paw_social_agent"}:
        from pocketpaw_ee.foresight.persona import (
            OceanDrift,
            SoulSeededPersona,
            make_paw_social_agent,
        )

        return {
            "OceanDrift": OceanDrift,
            "SoulSeededPersona": SoulSeededPersona,
            "make_paw_social_agent": make_paw_social_agent,
        }[name]
    if name in {
        "ClaudeCodeBackend",
        "LiteLLMFallbackBackend",
        "DeterministicFakeBackend",
        "translate_camel_tools_to_sdk_overrides",
    }:
        from pocketpaw_ee.foresight.llm.adapter import (
            ClaudeCodeBackend,
            DeterministicFakeBackend,
            LiteLLMFallbackBackend,
            translate_camel_tools_to_sdk_overrides,
        )

        return {
            "ClaudeCodeBackend": ClaudeCodeBackend,
            "DeterministicFakeBackend": DeterministicFakeBackend,
            "LiteLLMFallbackBackend": LiteLLMFallbackBackend,
            "translate_camel_tools_to_sdk_overrides": translate_camel_tools_to_sdk_overrides,
        }[name]
    if name in {"TierMix", "build_tier_pool", "tier_distribution"}:
        from pocketpaw_ee.foresight.llm.tier_pool import (
            TierMix,
            build_tier_pool,
            tier_distribution,
        )

        return {
            "TierMix": TierMix,
            "build_tier_pool": build_tier_pool,
            "tier_distribution": tier_distribution,
        }[name]
    if name in {
        "PredictionRecord",
        "PredictionBuffer",
        "CalibrationPair",
        "CalibrationSummary",
        "Correction",
        "CORRECTION_CAP",
        "pair_against_reality",
        "aggregate_pairs",
        "apply_correction",
        "build_prediction_record",
    }:
        from pocketpaw_ee.foresight.calibration import (
            CORRECTION_CAP,
            CalibrationPair,
            CalibrationSummary,
            Correction,
            PredictionBuffer,
            PredictionRecord,
            aggregate_pairs,
            apply_correction,
            build_prediction_record,
            pair_against_reality,
        )

        return {
            "CORRECTION_CAP": CORRECTION_CAP,
            "CalibrationPair": CalibrationPair,
            "CalibrationSummary": CalibrationSummary,
            "Correction": Correction,
            "PredictionBuffer": PredictionBuffer,
            "PredictionRecord": PredictionRecord,
            "aggregate_pairs": aggregate_pairs,
            "apply_correction": apply_correction,
            "build_prediction_record": build_prediction_record,
            "pair_against_reality": pair_against_reality,
        }[name]
    if name in {"run_scenario", "ScenarioConfig", "RunResult"}:
        from pocketpaw_ee.foresight.scenarios.runner import (
            RunResult,
            ScenarioConfig,
            run_scenario,
        )

        return {
            "run_scenario": run_scenario,
            "ScenarioConfig": ScenarioConfig,
            "RunResult": RunResult,
        }[name]
    if name in {
        "ThresholdDecision",
        "ConfidenceDrift",
        "ModalOutcomeDistribution",
        "accuracy_meets_threshold",
        "confidence_drift",
        "group_pairs_by",
        "index_predictions",
        "modal_outcome_distribution",
        "per_anchor_namespace_summary",
        "per_scenario_template_summary",
        "rolling_accuracy",
        "rolling_accuracy_series",
        "summarize_by",
    }:
        from pocketpaw_ee.foresight.aggregator import (
            ConfidenceDrift,
            ModalOutcomeDistribution,
            ThresholdDecision,
            accuracy_meets_threshold,
            confidence_drift,
            group_pairs_by,
            index_predictions,
            modal_outcome_distribution,
            per_anchor_namespace_summary,
            per_scenario_template_summary,
            rolling_accuracy,
            rolling_accuracy_series,
            summarize_by,
        )

        return {
            "ConfidenceDrift": ConfidenceDrift,
            "ModalOutcomeDistribution": ModalOutcomeDistribution,
            "ThresholdDecision": ThresholdDecision,
            "accuracy_meets_threshold": accuracy_meets_threshold,
            "confidence_drift": confidence_drift,
            "group_pairs_by": group_pairs_by,
            "index_predictions": index_predictions,
            "modal_outcome_distribution": modal_outcome_distribution,
            "per_anchor_namespace_summary": per_anchor_namespace_summary,
            "per_scenario_template_summary": per_scenario_template_summary,
            "rolling_accuracy": rolling_accuracy,
            "rolling_accuracy_series": rolling_accuracy_series,
            "summarize_by": summarize_by,
        }[name]
    raise AttributeError(f"module 'pocketpaw_ee.foresight' has no attribute {name!r}")
