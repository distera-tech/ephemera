"""Simulated inference runtime (EPHEMERA_MODE=simulation only).

No model runs. A deterministic heuristic produces a schema-valid analysis from
the document text so the full pipeline (prompt → transfer → output validation
→ UI) is exercised locally. Results are labelled as simulated everywhere.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter

from app.domain.models import BootstrapReport, InferenceRequest
from app.domain.ports import RuntimeContext

SIMULATED_MODEL_ID = "simulation/heuristic-analyzer"

_DOC = re.compile(r"<<<(EPHEMERA_DOC_[0-9a-f]+)>>>\n(.*)\n<<<END_\1>>>", re.DOTALL)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_INJECTION = re.compile(
    r"(ignore (all|any|the)? ?(previous|prior|above) instructions|disregard .{0,30}instructions|"
    r"system prompt|you are now|reveal .{0,20}(prompt|instructions|secret|key)|"
    r"run (the )?(command|shell)|curl http|rm -rf|api[_ ]key)",
    re.IGNORECASE,
)
_RISK_TERMS = {
    "terminat": ("Termination terms present; review notice periods and triggers.", "medium"),
    "indemnif": ("Indemnification obligations present; assess scope and caps.", "high"),
    "liabilit": ("Liability clauses present; check limitations and exclusions.", "medium"),
    "penalt": ("Penalty provisions present.", "medium"),
    "exclusiv": ("Exclusivity commitments may restrict future options.", "medium"),
    "auto-renew": ("Automatic renewal clause detected.", "low"),
    "automatically renew": ("Automatic renewal clause detected.", "low"),
    "confidential": ("Confidentiality obligations present.", "low"),
    "governing law": ("Governing law / jurisdiction clause present.", "low"),
}
_ENTITY = re.compile(
    r"\b([A-Z][a-zA-Z&]+(?:\s+(?:[A-Z][a-zA-Z&]+|Inc\.|Ltd\.|LLC|GmbH|S\.A\.)){1,3})\b"
)
_STOP = {"The", "This", "These", "Section", "Agreement", "Page", "Whereas", "In", "If"}


def heuristic_analysis(text: str) -> dict[str, object]:
    clean = " ".join(text.split())
    sentences = [s.strip() for s in _SENTENCE.split(clean) if len(s.strip()) > 25]
    lowered = clean.lower()
    if any(k in lowered for k in ("agreement", "contract", "hereby", "party", "parties")):
        doc_type = "Contract / agreement"
    elif any(k in lowered for k in ("invoice", "amount due", "payment terms")):
        doc_type = "Invoice / financial document"
    elif any(k in lowered for k in ("policy", "procedure", "shall comply")):
        doc_type = "Policy document"
    else:
        doc_type = "General document"

    risks: list[dict[str, str]] = []
    if _INJECTION.search(clean):
        risks.append(
            {
                "description": "Document contains text that attempts to instruct an AI system. It was treated as untrusted data and not followed.",
                "severity": "high",
            }
        )
    for term, (desc, sev) in _RISK_TERMS.items():
        if term in lowered and all(r["description"] != desc for r in risks):
            risks.append({"description": desc, "severity": sev})

    counts = Counter(
        m.group(1) for m in _ENTITY.finditer(text) if m.group(1).split()[0] not in _STOP
    )
    entities = [
        {"name": name[:200], "type": "organization_or_name"} for name, _ in counts.most_common(8)
    ]
    key_points = [s[:480] for s in sentences[1:6]] or [clean[:480] or "No text"]
    summary = " ".join(sentences[:3])[:1500] or clean[:1500] or "Empty document."
    return {
        "document_type": doc_type,
        "summary": "[Simulated analysis — no LLM was run] " + summary,
        "key_points": key_points,
        "potential_risks": risks[:10],
        "entities": entities,
        "requires_human_review": True,
    }


class SimulatedInferenceProvider:
    name = "simulated"
    is_simulated = True

    def __init__(self, time_scale: float = 1.0) -> None:
        self.scale = time_scale
        self._staged: dict[uuid.UUID, str] = {}

    async def bootstrap(self, ctx: RuntimeContext, timeout_s: float) -> BootstrapReport:
        await asyncio.sleep(3.0 * self.scale)
        return BootstrapReport(
            gpu_name="SIMULATED L40S",
            vram_mb=46068,
            driver_version="simulated",
            notes=("simulation",),
        )

    async def start(self, ctx: RuntimeContext, timeout_s: float) -> None:
        await asyncio.sleep(2.0 * self.scale)

    async def wait_ready(self, ctx: RuntimeContext, timeout_s: float) -> None:
        await asyncio.sleep(4.0 * self.scale)

    async def transfer(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        await asyncio.sleep(1.0 * self.scale)
        match = _DOC.search(request.user_prompt)
        self._staged[ctx.job_id] = match.group(2) if match else ""

    async def infer(self, ctx: RuntimeContext, timeout_s: float) -> str:
        await asyncio.sleep(4.0 * self.scale)
        text = self._staged.pop(ctx.job_id, "")
        return json.dumps(heuristic_analysis(text))

    async def cleanup(self, ctx: RuntimeContext, timeout_s: float) -> None:
        self._staged.pop(ctx.job_id, None)
        await asyncio.sleep(1.0 * self.scale)
