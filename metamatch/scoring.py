"""
scoring.py
Shared match-ambiguity assessment.

All three matchers already score every candidate and sort them best-first,
then return only the winner. But a winner's raw confidence doesn't say how
*clearly* it won: 93% with a 40-point lead over the runner-up is a confident
identification, while 93% with a 2-point lead is a near-tie that happened to
land one side up. Auto-apply policies care about that difference a lot - the
first is safe to apply unattended, the second deserves a human look.

This module captures the information the matchers were throwing away: the
runner-up, the margin between first and second, and a coarse ambiguity label.
It's pure and side-effect-free - each matcher passes its already-sorted
candidate list and copies the result onto the winning match dict.
"""

from __future__ import annotations

# Margin (winner confidence minus runner-up confidence) thresholds for the
# coarse label. Tuned to be advisory, not load-bearing: the numeric margin is
# what auto-apply gates on; the label is for display and quick reasoning.
_CLEAR_MARGIN = 15.0     # winner well ahead of the field
_MODERATE_MARGIN = 5.0   # a discernible but not commanding lead


def classify_ambiguity(margin: "float | None") -> str:
    """Coarse label for how contested the top match was.
      none     - only one candidate; nothing to be ambiguous against
      low      - winner clearly ahead (margin >= 15)
      moderate - a real but modest lead (5 <= margin < 15)
      high     - near-tie (margin < 5); the runner-up is a live alternative
    """
    if margin is None:
        return "none"
    if margin >= _CLEAR_MARGIN:
        return "low"
    if margin >= _MODERATE_MARGIN:
        return "moderate"
    return "high"


def summarize_ambiguity(scored: list, *, confidence_key: str = "confidence",
                        label_fields: tuple = ()) -> "tuple[dict | None, float | None, str]":
    """Given candidates already sorted best-first (each a dict carrying
    `confidence_key`), return (runner_up, margin, ambiguity) describing how
    decisively the winner beat the field.

    runner_up is a slim summary of the second-place candidate - its confidence
    plus whichever `label_fields` it has (e.g. title/artist) so a UI can show
    "second guess: <x>" - or None when there was no competition. margin is the
    winner's confidence minus the runner-up's (None with a single candidate).
    A lone candidate is NOT ambiguous: margin None -> label "none", and
    auto-apply treats it as passing any margin requirement."""
    if len(scored) < 2:
        return None, None, "none"

    winner, second = scored[0], scored[1]
    try:
        margin = round(float(winner[confidence_key]) - float(second[confidence_key]), 1)
    except (KeyError, TypeError, ValueError):
        return None, None, "none"

    runner_up = {"confidence": second.get(confidence_key)}
    for field in label_fields:
        value = second.get(field)
        if value is not None:
            runner_up[field] = value

    return runner_up, margin, classify_ambiguity(margin)


def annotate_winner(scored: list, winner: dict, *, confidence_key: str = "confidence",
                    label_fields: tuple = ()) -> dict:
    """Convenience: compute the assessment over `scored` and write
    runner_up/margin/ambiguity onto `winner` (which may be a re-built match
    dict, not necessarily scored[0] itself - the TV matcher rebuilds its
    winner). Returns `winner` for chaining."""
    runner_up, margin, ambiguity = summarize_ambiguity(
        scored, confidence_key=confidence_key, label_fields=label_fields)
    winner["runner_up"] = runner_up
    winner["margin"] = margin
    winner["ambiguity"] = ambiguity
    return winner
