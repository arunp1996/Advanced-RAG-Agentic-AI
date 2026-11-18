
import time
from functools import wraps
from typing import TypedDict

from langgraph.graph import StateGraph, END

from .mcp_client import retrieve_via_mcp
from .llm import answer
from .google_search import google_search, should_search_web



def _trace_node(name, input_builder, output_builder):
    """Add a privacy-conscious Langfuse span around an async graph node."""

    def decorator(func):
        @wraps(func)
        async def wrapped(state):
            # Telemetry setup must never prevent the original node running.
            try:
                from langfuse import get_client

                client = get_client()
                observation = client.start_as_current_observation(
                    as_type="span",
                    name=name,
                    input=input_builder(state),
                )
                span = observation.__enter__()
            except Exception:
                return await func(state)

            # Preserve the original node's exception behavior.
            try:
                result = await func(state)
            except BaseException as exc:
                try:
                    observation.__exit__(
                        type(exc), exc, exc.__traceback__
                    )
                except Exception:
                    pass
                raise

            # Record only metadata, not prompts, documents, or answers.
            try:
                span.update(output=output_builder(result))
            except Exception:
                pass

            try:
                observation.__exit__(None, None, None)
            except Exception:
                pass

            return result

        return wrapped

    return decorator


class AgentState(TypedDict):
    question: str
    trusted_user_id: int
    memory: str
    history: list[dict]
    contexts: list[dict]
    response: str
    attempts: int
    evidence_found: bool
    retrieval_error: bool
    retrieval_ms: float
    llm_ms: float
    web_search_attempted: bool
    web_search_ms: float


FOLLOW_UP_CUES = (
    "it", "its", "they", "them", "that", "those", "these", "this",
    "above", "previous", "earlier", "mentioned", "second", "first",
    "third", "another", "explain more", "explain it", "elaborate",
    "give an example", "with an example", "why", "how about",
    "what about", "continue", "summarize that",
)


def is_follow_up(question: str) -> bool:
    normalized = question.lower().strip()
    words = normalized.split()

    return any(
        cue in words or cue in normalized
        for cue in FOLLOW_UP_CUES
    )


def build_retrieval_query(question: str, history: list[dict]) -> str:
    question = question.strip()

    if not history or not is_follow_up(question):
        return question

    valid_history = [
        item
        for item in history
        if isinstance(item, dict)
        and item.get("role") in ("user", "assistant")
        and isinstance(item.get("content"), str)
        and item["content"].strip()
    ]

    if not valid_history:
        return question

    recent_history = valid_history[-6:]
    context_parts = []

    for item in recent_history:
        role = item["role"]
        content = item["content"].strip()
        max_chars = 1200 if role == "assistant" else 500
        content = content[:max_chars]
        context_parts.append(f"{role}: {content}")

    conversation_context = "\n".join(context_parts)

    return (
        f"Current question: {question}\n\n"
        f"Relevant conversation context:\n{conversation_context}\n\n"
        "Retrieve document passages that best answer the current question. "
        "Use the conversation only to resolve references and follow-ups. "
        "The conversation itself is not evidence from uploaded documents."
    )


def usable_contexts(contexts) -> list[dict]:
    if not isinstance(contexts, list):
        return []

    return [
        item
        for item in contexts
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]


def is_mixed_source_question(question: str) -> bool:
    """Identify requests that explicitly need private and public evidence."""
    q = " ".join((question or "").lower().split())
    cues = (
        "compare my document with",
        "compare my pdf with",
        "compare my file with",
        "along with latest",
        "alongside current",
        "latest industry trends",
        "external sources",
        "web sources",
        "search the web",
        "search online",
        "from the internet",
    )
    return any(cue in q for cue in cues)


async def route_question_node(state: AgentState) -> dict:
    """Start the graph with a routing decision."""
    return {}


def route_initial_question(state: AgentState) -> str:
    question = state.get("question", "")
    if (
        should_search_web(question)
        and not is_mixed_source_question(question)
    ):
        return "web_search"
    return "retrieve"


@_trace_node(
    "rag.retrieve",
    lambda s: {
        "attempt": s.get("attempts", 0) + 1,
        "question_length": len(s.get("question", "")),
    },
    lambda r: {
        "context_count": len(r.get("contexts", [])),
        "retrieval_error": r.get("retrieval_error", False),
        "retrieval_ms": round(r.get("retrieval_ms", 0.0), 2),
    },
)
async def retrieve_node(state: AgentState) -> dict:
    """Retrieve authorized document chunks and measure retrieval time."""
    start = time.perf_counter()

    try:
        retrieval_query = build_retrieval_query(
            question=state["question"],
            history=state.get("history") or [],
        )

        contexts = await retrieve_via_mcp(
            question=retrieval_query,
            trusted_user_id=state["trusted_user_id"],
            limit=5,
        )

        if not isinstance(contexts, list):
            contexts = []

        return {
            "contexts": contexts,
            "attempts": state.get("attempts", 0) + 1,
            "retrieval_error": False,
            "retrieval_ms": (
                state.get("retrieval_ms", 0.0)
                + (time.perf_counter() - start) * 1000
            ),
        }

    except Exception:
        return {
            "contexts": [],
            "attempts": state.get("attempts", 0) + 1,
            "retrieval_error": True,
            "retrieval_ms": (
                state.get("retrieval_ms", 0.0)
                + (time.perf_counter() - start) * 1000
            ),
        }


@_trace_node(
    "rag.web_search",
    lambda s: {
        "question_length": len(s.get("question", "")),
        "existing_context_count": len(s.get("contexts", [])),
    },
    lambda r: {
        "context_count": len(r.get("contexts", [])),
        "web_search_attempted": r.get("web_search_attempted", False),
        "web_search_ms": round(r.get("web_search_ms", 0.0), 2),
    },
)
async def web_search_node(state: AgentState) -> dict:
    """Append public Google results without discarding private RAG contexts."""
    start = time.perf_counter()
    contexts = list(state.get("contexts") or [])
    try:
        results = await google_search(state["question"])
        contexts.extend(results)
    except Exception:
        # Web search must not break authorized document retrieval.
        pass

    elapsed_ms = (time.perf_counter() - start) * 1000
    return {
        "contexts": contexts,
        "web_search_attempted": True,
        "web_search_ms": state.get("web_search_ms", 0.0) + elapsed_ms,
    }


def route_after_retrieval(state: AgentState) -> str:
    if (
        not state.get("web_search_attempted", False)
        and should_search_web(state.get("question", ""))
    ):
        return "web_search"
    return "validate"


@_trace_node(
    "rag.validate_evidence",
    lambda s: {
        "input_context_count": len(s.get("contexts", [])),
    },
    lambda r: {
        "usable_context_count": len(r.get("contexts", [])),
        "evidence_found": r.get("evidence_found", False),
    },
)
async def validate_evidence_node(state: AgentState) -> dict:
    """Keep only usable text chunks before answer generation."""
    contexts = usable_contexts(state.get("contexts", []))

    return {
        "contexts": contexts,
        "evidence_found": bool(contexts),
    }


def route_after_validation(state: AgentState) -> str:
    if state["evidence_found"]:
        return "answer"

    # If web search already ran, do not retry private retrieval just because
    # public results were unavailable.
    if state.get("web_search_attempted", False):
        return "fallback"

    if state["attempts"] < 2:
        return "retry"

    return "fallback"


@_trace_node(
    "rag.generate_answer",
    lambda s: {
        "context_count": len(s.get("contexts", [])),
        "history_count": len(s.get("history") or []),
    },
    lambda r: {
        "answer_length": len(r.get("response", "")),
        "llm_ms": round(r.get("llm_ms", 0.0), 2),
    },
)
async def answer_node(state: AgentState) -> dict:
    """Generate the answer and measure LLM processing time."""
    start = time.perf_counter()

    response = await answer(
        question=state["question"],
        contexts=state["contexts"],
        memory=state.get("memory", ""),
        history=state.get("history") or [],
    )

    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "response": response,
        "llm_ms": state.get("llm_ms", 0.0) + elapsed_ms,
    }


@_trace_node(
    "rag.fallback",
    lambda s: {
        "web_search_attempted": s.get("web_search_attempted", False),
        "retrieval_error": s.get("retrieval_error", False),
        "attempts": s.get("attempts", 0),
    },
    lambda r: {
        "fallback_response_length": len(r.get("response", "")),
    },
)
async def fallback_node(state: AgentState) -> dict:
    if state.get("web_search_attempted", False):
        message = (
            "I couldn't find enough usable information in the available "
            "web results to answer this question reliably. Please try "
            "rephrasing the question or narrowing the search."
        )
    else:
        message = (
            "I couldn't find enough usable information in your uploaded "
            "documents to answer this question. Please try rephrasing "
            "the question or upload a relevant document."
        )
    return {"response": message}


def build_agent_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("route_question", route_question_node)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("web_search", web_search_node)
    workflow.add_node("validate_evidence", validate_evidence_node)
    workflow.add_node("generate_answer", answer_node)
    workflow.add_node("fallback", fallback_node)

    workflow.set_entry_point("route_question")

    workflow.add_conditional_edges(
        "route_question",
        route_initial_question,
        {
            "web_search": "web_search",
            "retrieve": "retrieve",
        },
    )

    workflow.add_conditional_edges(
        "retrieve",
        route_after_retrieval,
        {
            "web_search": "web_search",
            "validate": "validate_evidence",
        },
    )
    workflow.add_edge("web_search", "validate_evidence")

    workflow.add_conditional_edges(
        "validate_evidence",
        route_after_validation,
        {
            "answer": "generate_answer",
            "retry": "retrieve",
            "fallback": "fallback",
        },
    )

    workflow.add_edge("generate_answer", END)
    workflow.add_edge("fallback", END)

    return workflow.compile()


agent_graph = build_agent_graph()


async def run_agent(
    question: str,
    trusted_user_id: int,
    memory: str = "",
    history: list[dict] | None = None,
) -> dict:
    """Run the agent with optional, privacy-conscious Langfuse tracing."""

    initial_state = {
        "question": question,
        "trusted_user_id": trusted_user_id,
        "memory": memory,
        "history": history or [],
        "contexts": [],
        "response": "",
        "attempts": 0,
        "evidence_found": False,
        "retrieval_error": False,
        "retrieval_ms": 0.0,
        "web_search_attempted": False,
        "web_search_ms": 0.0,
        "llm_ms": 0.0,
    }

    result = None
    graph_started = False

    try:
        from langfuse import get_client

        client = get_client()

        with client.start_as_current_observation(
            as_type="span",
            name="advanced-rag-agent",
            input={
                "question_length": len(question),
                "history_count": len(history or []),
            },
        ) as span:
            graph_started = True
            result = await agent_graph.ainvoke(initial_state)

            # Record metadata only; never send document or answer content.
            try:
                span.update(output={
                    "status": "completed",
                    "answer_length": len(result.get("response", "")),
                    "retrieval_count": len(result.get("contexts", [])),
                    "attempts": result.get("attempts", 0),
                    "evidence_found": result.get("evidence_found", False),
                    "retrieval_error": result.get("retrieval_error", False),
                    "timings_ms": {
                        "retrieval": round(
                            result.get("retrieval_ms", 0.0), 2
                        ),
                        "llm": round(result.get("llm_ms", 0.0), 2),
                    },
                })
            except Exception:
                # A tracing update must not break a successful RAG answer.
                pass

    except Exception:
        if graph_started:
            # Do not rerun the graph if it already started: retrieval and
            # LLM calls may have occurred. Preserve genuine graph exceptions.
            if result is None:
                raise
            # The graph completed; a tracing shutdown/flush error must not
            # discard its result.
        else:
            # Langfuse setup failed before graph execution. Run without tracing.
            result = await agent_graph.ainvoke(initial_state)

    return {
        "answer": result["response"],
        "contexts": result["contexts"],
        "timings_ms": {
            "retrieval": round(result.get("retrieval_ms", 0.0), 2),
            "web_search": round(result.get("web_search_ms", 0.0), 2),
            "llm": round(result.get("llm_ms", 0.0), 2),
        },
    }
