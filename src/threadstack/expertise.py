"""Profile-based expert routing for thread investigations."""

from __future__ import annotations

from collections.abc import Iterable

from .models import AccountType, Agent, AgentStatus, ExpertiseMatch, PublicAgent
from .semantic import cosine_similarity, embed_text, text_tokens, thread_search_text


def route_experts(
    query: str,
    agents: Iterable[Agent],
    limit: int = 5,
) -> list[ExpertiseMatch]:
    limit = max(1, min(limit, 20))
    query_tokens = set(text_tokens(query))
    query_embedding = embed_text(query)
    matches: list[ExpertiseMatch] = []
    for agent in agents:
        if agent.account_type is not AccountType.AGENT or agent.status is not AgentStatus.ACTIVE:
            continue
        profile_terms = list(dict.fromkeys([*agent.capabilities, *agent.specialties]))
        if not profile_terms and not (agent.description or agent.bio):
            continue
        profile_text = thread_search_text(
            agent.name,
            profile_terms,
            [item for item in (agent.description, agent.bio) if item],
        )
        profile_tokens = set(text_tokens(profile_text))
        matched_terms = sorted(
            {
                term.casefold()
                for term in profile_terms
                if set(text_tokens(term)) & query_tokens
            }
        )
        term_score = len(matched_terms) / max(len(profile_terms), 1)
        token_score = len(profile_tokens & query_tokens) / max(len(query_tokens), 1)
        semantic_score = max(0.0, cosine_similarity(embed_text(profile_text), query_embedding))
        score = min(1.0, 0.6 * term_score + 0.25 * token_score + 0.15 * semantic_score)
        if score < 0.05:
            continue
        public_agent = PublicAgent.model_validate(agent.model_dump(exclude={"email"}))
        matches.append(
            ExpertiseMatch(
                agent=public_agent,
                score=round(score, 4),
                matched_terms=matched_terms,
            )
        )
    return sorted(
        matches,
        key=lambda match: (match.score, match.agent.reputation, match.agent.name),
        reverse=True,
    )[:limit]
