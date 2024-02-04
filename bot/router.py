"""
router.py — decides which model to use based on query intent.
Default Codex model for factual/data lookups, creative Codex model for open-ended questions.
"""

CREATIVE_TRIGGERS = [
    "why should", "what makes", "tell me about", "what's exciting",
    "convince me", "unique", "vision", "future", "roadmap",
    "what do you think", "describe", "explain the idea", "what is the goal",
    "potential", "opportunity", "worth joining", "interesting",
    "innovative", "how does this compare", "what sets",
]

def route_query(query: str) -> str:
    """Returns 'creative' for creative queries, 'factual' for everything else."""
    lowered = query.lower()
    if any(t in lowered for t in CREATIVE_TRIGGERS):
        return "creative"
    return "factual"
