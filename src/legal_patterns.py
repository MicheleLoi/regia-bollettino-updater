# SPDX-License-Identifier: AGPL-3.0-only
"""
legal_patterns.py — Haiku batch generation for BeccarIA legal_content_methodology pattern v2.

Implements the scaffold-not-answer doctrine (decision_log 2026-05-28 SID-20260528-093309):
Haiku produces structural scaffolds (workflow steps, broad normative areas, plain Italian)
and the per-pattern "nota_per_avvocato" transfers citation responsibility to the lawyer.

Pipeline per target:
    1. load prompt template + 27 targets
    2. for each target: call Haiku, parse JSON, validate against schema v2
    3. on validation failure: up to 2 refinement rounds with error feedback
    4. on success: append to bulletin_legal_patterns.json
    5. on persistent failure: append to bulletin_legal_patterns_pending_review.json

Cost guard: hard cap $10 (expected ~$1.50). Pricing is Haiku 4.5 ($1/Mtok in, $5/Mtok out).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# anthropic + dotenv are runtime deps (see pyproject.toml). Imported lazily inside
# run_batch so the rest of the CLI keeps working if they are not installed yet.


# ---------------------------------------------------------------------------
# Canonical constants (verbatim from MHC-Work canon — DO NOT EDIT IN PLACE)
# ---------------------------------------------------------------------------

# nota_per_avvocato canonical text. Verbatim from:
#   notes/research/beccaria/legal_patterns_schema_v1_20260528.md §2
# Validation enforces equality with this exact string.
NOTA_CANONICAL = (
    "Questo pattern è un'impalcatura di lavoro generata da AI. La struttura "
    "(i passaggi proposti, la successione, le varianti) ti è offerta come "
    "schema di partenza per il tuo caso.\n\n"
    "Le aree normative indicate (codici, capi, sezioni) sono affidabili "
    "come orientamento: ti dicono dove cercare. I riferimenti puntuali ai "
    "singoli articoli sono di tua responsabilità: verificali sul testo della "
    "legge o con la skill /verifica-fonti prima di citarli in un atto.\n\n"
    "Le varianti opzionali sono ipotesi: scartane quante vuoi. Tu sei "
    "l'avvocato — questo è uno strumento al tuo servizio, non un'autorità."
)


# ---------------------------------------------------------------------------
# Legacy schema conversion (scaffold v2 → bulletin_patterns.json schema 1.0.0)
# ---------------------------------------------------------------------------

def _derive_task_name(pattern_id: str) -> str:
    """legal_civile_contratti_trasporto_001 → legal_civile_contratti_trasporto"""
    return re.sub(r"_\d{3}$", "", pattern_id)


def _compose_prompt_template(pattern: dict) -> str:
    """Compose legacy prompt_template field as markdown block from scaffold v2 pattern."""
    nota = pattern.get("nota_per_avvocato", "")
    reasoning = pattern.get("reasoning_explanation", "")
    aree = pattern.get("search", {}).get("aree_normative_rilevanti", [])
    steps = pattern.get("default_approach", [])
    variations = pattern.get("optional_variations", [])

    parts: list[str] = []
    if nota:
        parts.append(nota)
        parts.append("")
    if reasoning:
        parts.append("## Approccio")
        parts.append(reasoning)
        parts.append("")
    if aree:
        parts.append("## Aree normative rilevanti")
        for area in aree:
            parts.append(f"- {area}")
        parts.append("")
    if steps:
        parts.append("## Passaggi proposti")
        for step in steps:
            sid = step.get("step_id", "?")
            name = step.get("name", "")
            rationale = step.get("rationale", "")
            riferimenti = step.get("riferimenti_da_consultare", "")
            parts.append(f"{sid}. **{name}** — {rationale}")
            if riferimenti:
                parts.append(f"   Da consultare: {riferimenti}")
        parts.append("")
    if variations:
        parts.append("## Varianti possibili")
        for var in variations:
            parts.append(f"- {var}")
        parts.append("")
    return "\n".join(parts).strip()


def _to_legacy_pattern(p: dict) -> dict:
    """Map scaffold v2 pattern to legacy schema 1.0.0 fields (BeccarIA pattern-extractor)."""
    use_cases = p.get("search", {}).get("use_case_examples", []) or []
    return {
        "task_name": _derive_task_name(p["pattern_id"]),
        "description": p.get("title", ""),
        "prompt_template": _compose_prompt_template(p),
        "example_input": use_cases[0] if use_cases else None,
        "example_output": None,
        # source_* fields = editorial attribution (curated + published from this repo).
        # Distinct from CONTENT license: pattern data is proprietary (see top-level
        # content_license in bulletin_patterns.json + NOTICE.md in regia-bollettino-updater).
        # SID-20260529-manual founder direttiva: "il bollettino non ha bisogno di essere
        # AGPL". Code (this script) = AGPL-3.0; content (generated patterns) = proprietary.
        "source_repo": "MicheleLoi/legal-tech-cowork",
        "source_owner": "MicheleLoi",
        "source_url": "https://github.com/MicheleLoi/legal-tech-cowork",
        "source_commit": None,
        "source_license": "proprietary",
        "extraction_confidence": "high",
    }

# Haiku model id (decision_log canon § generated_by.model)
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Haiku 4.5 pricing (USD per 1M tokens) — official as of 2026-05-28.
# If pricing changes upstream, update here.
PRICE_INPUT_PER_MTOK = 1.0
PRICE_OUTPUT_PER_MTOK = 5.0

# Hard cost cap for full batch (USD). Expected ~$1.50, $10 is 6x margin.
HARD_COST_CAP_USD = 10.0

# Max refinement rounds (1 first attempt + 2 refinements = 3 total tries).
MAX_REFINEMENT_ROUNDS = 2

# Max tokens per response (Haiku output budget per pattern).
MAX_TOKENS_RESPONSE = 6000

VALID_LEGAL_AREAS = [
    "civile", "penale", "amministrativo", "tributario",
    "lavoro", "gdpr_compliance", "corporate", "generic",
]

# Specific-article detection regex. Matches "art. 1693 cc", "art. 35 GDPR",
# "art. 336 cpp", "art. 35(3) GDPR", "art. 2103-bis cc" etc.
# Tolerates "~" prefix (used to signal approximate ranges, e.g. "artt. ~1678-1702").
SPECIFIC_ARTICLE_RE = re.compile(
    r"\bart\.?\s*\d+(?:\s*(?:bis|ter|quater|quinquies))?(?:\s*\(\d+\))?"
    r"(?:\s*[A-Z]\.?\s*[A-Z]\.?\s*[A-Z]?\.?|\s*cc|\s*cp|\s*cpp|\s*GDPR)",
    re.IGNORECASE,
)

# Forbidden developer-jargon tokens in user-visible body.
FORBIDDEN_TOKENS = [
    "skill", "plugin", "frontmatter", "commit", "repository",
    "API", "AGPL", "rationale", "draft", "metadata",
]

AUTHORITATIVE_MARKERS = [
    "devi mappare", "questo pattern ti guida",
    "occorre stabilire", "devi indicare",
]


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_targets(path: Path) -> list[dict[str, Any]]:
    """Load the pilot domain coverage JSON (list of 27 targets)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_prompt_template(path: Path) -> str:
    """Load the verbatim Haiku prompt template (with {{PLACEHOLDER}} slots)."""
    return Path(path).read_text(encoding="utf-8")


def instantiate_prompt(
    template: str,
    target: dict[str, Any],
    date_iso: str,
    batch_id: str,
    timestamp_iso: str,
) -> str:
    """Substitute {{PLACEHOLDER}} slots with actual values.

    Uses str.replace (not str.format) to avoid clashing with the JSON braces
    embedded in the template body.
    """
    return (
        template
        .replace("{{LEGAL_AREA}}", target["legal_area"])
        .replace("{{TOPIC_PRIMARY}}", target["topic_primary"])
        .replace("{{TOPIC_SUB}}", target["topic_sub"])
        .replace("{{JURISDICTION}}", target.get("jurisdiction", "IT"))
        .replace("{{DATE_ISO}}", date_iso)
        .replace("{{BATCH_ID}}", batch_id)
        .replace("{{TIMESTAMP_ISO}}", timestamp_iso)
    )


# ---------------------------------------------------------------------------
# Validation (schema v2 + scaffold-not-answer enforcement)
# ---------------------------------------------------------------------------

def validate_pattern_v2(pattern: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate a generated pattern against schema v2.0 + scaffold rules.

    Returns (errors, warnings). errors=[] means the pattern is publishable.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- structural required fields ---
    required_top = [
        "pattern_id", "pattern_type", "schema_version", "generated_by",
        "domain", "nota_per_avvocato", "search", "title",
        "reasoning_explanation", "default_approach", "optional_variations",
        "provenance_chain", "human_validated", "founder_curated",
        "empirical_use_count", "empirical_validation_count",
        "empirical_rejection_count",
    ]
    for key in required_top:
        if key not in pattern:
            errors.append(f"Missing required top-level field: {key}")

    if errors:
        # Skip nested checks if scaffold is broken; refinement will hit these first.
        return errors, warnings

    # --- fixed-value fields ---
    if pattern.get("schema_version") != "2.0":
        errors.append(
            f"schema_version must be '2.0', got: {pattern.get('schema_version')!r}"
        )
    if pattern.get("pattern_type") != "legal_content_methodology":
        errors.append(
            f"pattern_type must be 'legal_content_methodology', "
            f"got: {pattern.get('pattern_type')!r}"
        )

    # --- pattern_id naming ---
    pid = pattern.get("pattern_id", "")
    if not re.match(r"^legal_[a-z_]+_\d{3}$", pid):
        errors.append(f"pattern_id naming violates schema: {pid!r}")

    # --- domain enum ---
    domain = pattern.get("domain", {})
    if domain.get("legal_area") not in VALID_LEGAL_AREAS:
        errors.append(
            f"legal_area not in enum: {domain.get('legal_area')!r} "
            f"(valid: {VALID_LEGAL_AREAS})"
        )

    # --- nota_per_avvocato VERBATIM canonical (critical) ---
    nota = pattern.get("nota_per_avvocato", "")
    if nota != NOTA_CANONICAL:
        snippet = nota[:120] if isinstance(nota, str) else "<not a string>"
        errors.append(
            "nota_per_avvocato is NOT verbatim canonical. Must copy template "
            f"string literally. Got: {snippet!r}..."
        )

    # --- aree_normative_rilevanti ---
    search = pattern.get("search", {})
    aree = search.get("aree_normative_rilevanti", [])
    if not isinstance(aree, list):
        errors.append(
            f"search.aree_normative_rilevanti must be a list, "
            f"got {type(aree).__name__}"
        )
        aree = []
    if not (2 <= len(aree) <= 5):
        errors.append(
            f"aree_normative_rilevanti count {len(aree)} not in [2, 5]"
        )
    for area in aree:
        if not isinstance(area, str):
            errors.append(
                f"aree_normative_rilevanti element not a string: {type(area).__name__}"
            )
            continue
        # Tilde "~" in the string signals approximate range (allowed).
        # Without "~", any "art. NNN" match is a specific-article violation.
        if "~" not in area and SPECIFIC_ARTICLE_RE.search(area):
            errors.append(
                f"aree_normative_rilevanti contains specific article (vietato): "
                f"{area!r} — use a broad range with ~ prefix (es. "
                f"'artt. ~1678-1702')"
            )

    # --- default_approach steps ---
    steps = pattern.get("default_approach", [])
    if not isinstance(steps, list):
        errors.append(
            f"default_approach must be a list, got {type(steps).__name__}"
        )
        steps = []
    if not (3 <= len(steps) <= 7):
        errors.append(f"default_approach step count {len(steps)} not in [3, 7]")

    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            errors.append(f"step {i}: not a dict")
            continue
        rif = step.get("riferimenti_da_consultare")
        if rif is None:
            errors.append(f"step {i}: missing riferimenti_da_consultare")
        elif not isinstance(rif, str):
            errors.append(
                f"step {i}: riferimenti_da_consultare must be string, "
                f"got {type(rif).__name__}"
            )
        else:
            if SPECIFIC_ARTICLE_RE.search(rif):
                errors.append(
                    f"step {i}: riferimenti_da_consultare contains specific "
                    f"article (vietato): {rif!r}"
                )
        if "citations" in step:
            errors.append(
                f"step {i}: legacy v1 field 'citations' present — must use "
                f"'riferimenti_da_consultare'"
            )

    # --- optional_variations ---
    vars_ = pattern.get("optional_variations", [])
    if not isinstance(vars_, list):
        errors.append(
            f"optional_variations must be a list, got {type(vars_).__name__}"
        )
        vars_ = []
    if not (2 <= len(vars_) <= 4):
        errors.append(f"optional_variations count {len(vars_)} not in [2, 4]")

    # --- reasoning_explanation length + framing ---
    reasoning = pattern.get("reasoning_explanation", "")
    if not isinstance(reasoning, str):
        errors.append("reasoning_explanation must be a string")
        reasoning = ""
    # Sentence-count heuristic: count terminator punctuation.
    sentence_count = reasoning.count(".") + reasoning.count("?") + reasoning.count("!")
    if sentence_count > 5:  # tolerance: 4 sentences spec + 1
        errors.append(
            f"reasoning_explanation >4 sentences (count {sentence_count})"
        )

    reasoning_lower = reasoning.lower()
    for marker in AUTHORITATIVE_MARKERS:
        if marker in reasoning_lower:
            warnings.append(
                f"reasoning_explanation may use authoritative framing: "
                f"{marker!r} — prefer scaffold framing"
            )

    # --- forbidden developer/itanglish tokens in body ---
    body = " ".join([
        reasoning,
        " ".join(s.get("rationale", "") for s in steps if isinstance(s, dict)),
        " ".join(a for a in aree if isinstance(a, str)),
    ])
    body_lower = body.lower()
    for token in FORBIDDEN_TOKENS:
        # Word-boundary match to avoid false positive on inflected words.
        if re.search(rf"\b{re.escape(token.lower())}\b", body_lower):
            errors.append(
                f"forbidden developer/itanglish token in body: {token!r}"
            )

    # --- legacy v1 fields (regression guard) ---
    if "citation_authorities" in search:
        errors.append(
            "legacy v1 field 'search.citation_authorities' present — must be removed in v2"
        )
    if "[VERIFICA]" in json.dumps(pattern, ensure_ascii=False):
        warnings.append(
            "[VERIFICA] marker present — v2 design uses structural disclaimer "
            "instead. Signal that Haiku misunderstood the prompt."
        )

    return errors, warnings


# ---------------------------------------------------------------------------
# Refinement prompt
# ---------------------------------------------------------------------------

def refinement_prompt(errors: list[str], last_attempt: str) -> str:
    """Build the refinement prompt when validation fails."""
    errors_block = "\n".join(f"- {e}" for e in errors)
    return f"""Il pattern che hai generato presenta questi problemi:

{errors_block}

Per favore rigenera il JSON correggendo SPECIFICAMENTE questi punti.
Ricorda i vincoli scaffold-not-answer:
- VIETATO citare singoli articoli (art. NNN cc/cp/cpp/GDPR)
- USA aree larghe con range approssimato (~)
- nota_per_avvocato VERBATIM dalla stringa canonica
- riferimenti_da_consultare per-step deve essere stringa descrittiva, non lista articoli

Output: SOLO JSON, niente prosa esplicativa pre/post, niente ```json fence```.

Mantieni tutto il resto del pattern uguale dove possibile.

Per riferimento, il tuo ultimo output era (TRONCATO se lungo):

{last_attempt[:3000]}
"""


# ---------------------------------------------------------------------------
# Generation + per-pattern orchestration
# ---------------------------------------------------------------------------

@dataclass
class CostTracker:
    """Cumulative cost in USD, computed from anthropic usage counters."""
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, in_tok: int, out_tok: int) -> None:
        self.input_tokens += in_tok
        self.output_tokens += out_tok

    @property
    def usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        )

    def __str__(self) -> str:
        return (
            f"in={self.input_tokens} tok, out={self.output_tokens} tok, "
            f"cost=${self.usd:.4f}"
        )


def _strip_json_fence(raw: str) -> str:
    """Remove ```json``` fences from raw output if Haiku added them despite the rule."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    return raw.strip()


def generate_pattern(
    target: dict[str, Any],
    client: Any,  # anthropic.Anthropic — typed Any to avoid import-time dep
    prompt_template: str,
    batch_id: str,
    date_iso: str,
    timestamp_iso: str,
    cost: CostTracker,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Generate one pattern with up to MAX_REFINEMENT_ROUNDS refinements.

    Returns (pattern_ok, None) on success, (None, pending_review_entry) on failure.
    """
    prompt = instantiate_prompt(
        prompt_template, target, date_iso, batch_id, timestamp_iso
    )

    last_raw = ""
    last_errors: list[str] = []
    last_warnings: list[str] = []

    for attempt in range(1, MAX_REFINEMENT_ROUNDS + 2):
        # Cost cap check BEFORE the next API call.
        if cost.usd > HARD_COST_CAP_USD:
            return None, {
                "target": target,
                "errors": ["HARD_COST_CAP_REACHED"],
                "last_attempt_raw": last_raw,
                "warnings": last_warnings,
                "attempts": attempt - 1,
            }

        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=MAX_TOKENS_RESPONSE,
            messages=[{"role": "user", "content": prompt}],
        )
        cost.add(response.usage.input_tokens, response.usage.output_tokens)

        raw = response.content[0].text
        last_raw = raw
        raw_clean = _strip_json_fence(raw)

        try:
            pattern = json.loads(raw_clean)
        except json.JSONDecodeError as e:
            errors = [f"Output not valid JSON: {e}"]
            warnings_ = []
        else:
            errors, warnings_ = validate_pattern_v2(pattern)

        last_errors = errors
        last_warnings = warnings_

        if not errors:
            return pattern, None

        # Failure — build refinement prompt for next attempt (if any remain).
        if attempt <= MAX_REFINEMENT_ROUNDS:
            prompt = refinement_prompt(errors, raw_clean)

    return None, {
        "target": target,
        "errors": last_errors,
        "warnings": last_warnings,
        "last_attempt_raw": last_raw,
        "attempts": MAX_REFINEMENT_ROUNDS + 1,
    }


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

@dataclass
class BatchResult:
    patterns_ok: list[dict[str, Any]] = field(default_factory=list)
    pending_review: list[dict[str, Any]] = field(default_factory=list)
    cost: CostTracker = field(default_factory=CostTracker)
    aborted: bool = False
    abort_reason: str = ""


def run_batch(
    repo_root: Path,
    dry_run: bool = False,
    limit: int | None = None,
    coverage_path: Path | None = None,
    append_mode: bool = False,
) -> BatchResult:
    """Run the Haiku batch over the pilot domain coverage targets.

    Parameters:
        repo_root: regia-bollettino-updater repo root (cwd-equivalent).
        dry_run: if True, run on a single target (sanity check).
        limit: cap the number of targets processed (None = all 27).
        coverage_path: override default coverage file path.
        append_mode: if True, load existing bulletin_patterns.json, skip targets
            whose task_name is already covered (dedup), generate only the new
            targets, and merge results (existing patterns preserved verbatim,
            including their post-hoc enrichment fields like keywords/legal_area/
            jurisdiction added by external script). Doctrine ratificata
            2026-05-28 SID-20260528-manual.

    Writes:
        output/bulletin_legal_patterns.json
        output/bulletin_legal_patterns_pending_review.json
    """
    # Lazy imports — these are runtime deps that may not be installed when
    # the rest of the CLI is invoked. Only required at batch-execution time.
    try:
        import anthropic
        from dotenv import load_dotenv
    except ImportError as e:
        raise RuntimeError(
            f"Missing runtime dependency for legal-patterns batch: {e}. "
            f"Run: pip install -e \".[dev]\" inside the repo venv to install."
        ) from e

    load_dotenv(repo_root / ".env", override=True)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found in environment. "
            "Add it to .env or export it before invoking."
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Coverage file: override or default
    if coverage_path is None:
        coverage_path = repo_root / "output" / "pilot_domain_coverage_v1.json"
    prompt_path = repo_root / "src" / "legal_patterns_prompt_v2.txt"
    # Output target: bulletin_patterns.json (legacy schema 1.0.0 consumed by
    # BeccarIA pattern-extractor v4.0.0). Scaffold-not-answer patterns get
    # converted to legacy format via _to_legacy_pattern() before write.
    # Decision 2026-05-28 SID-20260528-093309: skill stabile, bollettino mutabile;
    # no dual-bollettino routing; one file = one consumer skill.
    out_ok_path = repo_root / "output" / "bulletin_patterns.json"
    out_pending_path = repo_root / "output" / "bulletin_legal_patterns_pending_review.json"

    targets = load_targets(coverage_path)
    prompt_template = load_prompt_template(prompt_path)

    # ── APPEND MODE — load existing patterns + dedup targets ────────────────
    # Doctrine append-only ratificata 2026-05-28 SID-20260528-manual:
    # il bullettino cresce per accumulo; pattern esistenti preservati verbatim
    # (NON re-rolled da Haiku) per non perdere stochasticity + post-hoc
    # enrichment fields. Solo target non ancora coperti vengono generati.
    existing_patterns: list[dict[str, Any]] = []
    if append_mode and out_ok_path.exists():
        existing_doc = json.loads(out_ok_path.read_text(encoding="utf-8"))
        existing_patterns = existing_doc.get("patterns", [])
        existing_task_names = {p.get("task_name") for p in existing_patterns}
        before_count = len(targets)
        targets = [
            t for t in targets
            if _derive_task_name(t["pattern_id"]) not in existing_task_names
        ]
        skipped = before_count - len(targets)
        print(f"[append] Loaded {len(existing_patterns)} existing patterns; "
              f"skipped {skipped} already-covered targets; "
              f"will generate {len(targets)} new.")

    if dry_run:
        targets = targets[:1]
    elif limit is not None:
        targets = targets[:limit]

    now_utc = datetime.now(timezone.utc)
    date_iso = now_utc.strftime("%Y-%m-%d")
    timestamp_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    batch_id = f"batch_pilot_002_{now_utc.strftime('%Y%m%d')}"

    result = BatchResult()

    print(f"[batch] {batch_id} — {len(targets)} target(s), model={HAIKU_MODEL}")
    if dry_run:
        print("[batch] dry_run=True (single target sanity check)")
    print(f"[batch] cost cap: ${HARD_COST_CAP_USD:.2f}")
    print("-" * 70)

    for idx, target in enumerate(targets, 1):
        pid = target["pattern_id"]
        print(f"[{idx}/{len(targets)}] {pid} ... ", end="", flush=True)

        try:
            pattern, pending = generate_pattern(
                target=target,
                client=client,
                prompt_template=prompt_template,
                batch_id=batch_id,
                date_iso=date_iso,
                timestamp_iso=timestamp_iso,
                cost=result.cost,
            )
        except Exception as e:
            # Anthropic API errors, network etc. Record as pending and continue.
            print(f"ERROR: {type(e).__name__}: {e}")
            result.pending_review.append({
                "target": target,
                "errors": [f"API/runtime error: {type(e).__name__}: {e}"],
                "warnings": [],
                "last_attempt_raw": "",
                "attempts": 0,
            })
            continue

        if pattern is not None:
            print(f"OK ({result.cost})")
            result.patterns_ok.append(pattern)
        else:
            err_short = (
                pending["errors"][0][:80] if pending and pending.get("errors") else "?"
            )
            print(f"PENDING_REVIEW: {err_short} ({result.cost})")
            result.pending_review.append(pending)

        # Cost cap abort.
        if result.cost.usd > HARD_COST_CAP_USD:
            result.aborted = True
            result.abort_reason = (
                f"Hard cost cap ${HARD_COST_CAP_USD:.2f} reached: "
                f"{result.cost}"
            )
            print(f"[batch] ABORTED: {result.abort_reason}")
            break

    # Convert scaffold v2 patterns to legacy schema 1.0.0 format (one-shot,
    # consumed by BeccarIA pattern-extractor). Pending_review file keeps
    # the raw v2 format for audit (it's internal, not published).
    new_legacy_patterns = [_to_legacy_pattern(p) for p in result.patterns_ok]

    # Append mode: merge existing patterns (preserved verbatim) + new generated.
    # Existing patterns may have post-hoc enrichment fields (keywords/legal_area/
    # jurisdiction added externally to schema v1.1.0); preserved as-is.
    # Newly generated patterns are at schema v1.0.0 (lack enrichment fields);
    # external enrichment script must run after this batch to bring them to v1.1.0.
    if append_mode:
        merged_patterns = existing_patterns + new_legacy_patterns
        # Preserve schema_version from existing doc (1.1.0 if previously enriched);
        # the merge as-is leaves new patterns without enrichment fields until
        # external enrichment step runs.
        schema_version = existing_doc.get("schema_version", "1.0.0") if append_mode and out_ok_path.exists() else "1.0.0"
    else:
        merged_patterns = new_legacy_patterns
        schema_version = "1.0.0"

    # Write outputs.
    out_ok_path.parent.mkdir(parents=True, exist_ok=True)
    out_ok_path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "generated_at": timestamp_iso,
                "source_count": 1,
                # Content license: dati proprietari (editorial curation © MicheleLoi).
                # Distinct from the code license of this generator script (AGPL-3.0
                # in repo MicheleLoi/regia-bollettino-updater). The two are separate
                # assets per founder direttiva SID-20260529-manual: "il bollettino
                # non ha bisogno di essere AGPL".
                "content_license": "proprietary",
                "content_license_notice": (
                    "Content of this bulletin is proprietary, © MicheleLoi 2026, "
                    "All Rights Reserved. The generator script (regia-bollettino-updater) "
                    "is AGPL-3.0 but distinct from this content. The consumer skill "
                    "(BeccarIA, legal-tech-cowork) is also AGPL-3.0 but consumes this "
                    "proprietary content via HTTPS. See NOTICE.md for full details."
                ),
                "patterns": merged_patterns,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_pending_path.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "generated_at": timestamp_iso,
                "batch_id": batch_id,
                "pending": result.pending_review,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("-" * 70)
    print(f"[batch] DONE")
    print(f"[batch] patterns OK: {len(result.patterns_ok)}")
    print(f"[batch] pending review: {len(result.pending_review)}")
    print(f"[batch] cost: {result.cost}")
    print(f"[batch] output OK: {out_ok_path}")
    print(f"[batch] output pending: {out_pending_path}")

    return result
