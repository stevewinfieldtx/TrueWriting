# CPP Specification — v1 (draft for Steve's review)

**Status:** First-pass design, written from scratch against the detection + generation requirements. Decisive but opinionated. Every section marked "Decision:" is a call I made — push back on any that feels wrong.

**Location note:** This doc belongs in a dedicated TDE repo when that exists. Suggest creating `github.com/stevewinfieldtx/TDE` and moving this there, with a pointer from `Chimera_Secured/architecture_spec.md`.

---

## 1. Purpose — what a CPP is, what it isn't

A **Communication Personality Profile (CPP)** is a compact, sovereignty-safe fingerprint of one person's communication style, derived from a corpus of their output in one or more modalities (email, YouTube, podcast), and designed to serve two downstream use cases simultaneously:

1. **Detection** — Chimera and future defensive apps compare incoming messages against a CPP to catch impersonation. Needs discriminative statistical features.
2. **Generation** — composition apps (email writing, script drafting, social posts) use a CPP to steer LLM output so it sounds like the person. Needs steerable stylistic features.

**A CPP is not:**
- A full text corpus (that's the input, not the artifact)
- A per-writer language model (too expensive to store at 60K-seat scale; addressed in §9)
- A personality test result (no MBTI, no Big Five — those are interpretive, not empirical)
- A creative-writing voice guide (that's a prose document; a CPP is machine-readable)

**A CPP is not personally sensitive content.** See §2 — sovereignty is baked into the schema, not layered on top.

---

## 2. Sovereignty guarantee

The sovereignty pitch is "CPPs leave the customer environment, raw content never does." For that to be true, the CPP format must structurally prevent content reconstruction. The rules:

- **No full sentences.** Nothing in the CPP is a quotable fragment of what the writer said.
- **No phrase fragments longer than 4 tokens.**
- **No phrase fragment with frequency < 5.** If a phrase appeared only once or twice in the corpus, it's uniquely identifying and it doesn't go in the CPP. Common phrases ("thanks for the update", "let me know") are fine.
- **No named entities from content.** Names of people, companies, projects, locations are stripped at the TDE ingest stage before features are computed.
- **No email addresses, no URLs, no phone numbers, no IDs.** Regex-scrub at ingest.
- **Embeddings are over normalized n-gram distributions, not over full messages.** You can't invert a 3-gram histogram into prose.
- **Topic labels are coarse (K=20 clusters max).** Not "working on the merger with Acme Corp" — just "business_transactions".

**Decision:** Sovereignty rules are enforced by a validation layer every CPP must pass before storage. A `cpp_validate()` function that rejects any CPP containing regex-detectable PII or phrase fragments that violate the rules. Fail-closed.

---

## 3. Schema (v1)

Two-level structure: per-modality source CPPs + a unified cross-modality CPP.

### 3.1 Top-level record

```jsonc
{
  "cpp_id": "uuid",                           // stable id, never reused
  "writer_id": "string",                      // TDE-internal writer identifier
  "schema_version": "1.0.0",                  // semver; bumped on breaking changes
  "generated_at": "ISO8601",
  "source_manifest_hash": "sha256",           // content-addresses the input corpus
  "validation_status": "passed|failed|stale",
  "source_cpps": {
    "email":   { ...source_cpp... },          // null if no email corpus
    "youtube": { ...source_cpp... },          // null if no video corpus
    "podcast": { ...source_cpp... }           // null if no podcast corpus
  },
  "unified": { ...unified_cpp... }            // cross-modality merged features
}
```

### 3.2 `source_cpp` (per-modality block)

Six feature categories. Some apply across all modalities (lexical, semantic, behavioral), some are modality-specific (structural, prosodic).

```jsonc
{
  "modality": "email|youtube|podcast",
  "corpus_stats": {
    "sample_count": 427,                      // emails, videos, or podcast episodes
    "total_tokens": 183441,
    "date_range": { "earliest": "ISO", "latest": "ISO" },
    "median_sample_tokens": 178
  },

  // Category 1: Lexical — strongest stylometric signal, always computed
  "lexical": {
    "function_word_freqs": { "the": 0.0412, "and": 0.0338, ... },  // top ~150 function words
    "content_word_top50": [ {"w": "quarterly", "rate_per_1k": 2.1}, ... ],
    "ttr": 0.287,                             // type-token ratio
    "avg_word_length_chars": 4.49,
    "hapax_ratio": 0.412,                     // words appearing exactly once / total
    "signature_deviation_vector_uri": "pgvector://..."  // 768-d embedding of lexical distinctiveness
  },

  // Category 2: Structural — modality-specific
  "structural": {
    // Email fields (only present when modality=email):
    "email_greeting_pattern_distribution": { "hi_firstname": 0.62, "hey": 0.18, ... },
    "email_closing_pattern_distribution": { "thanks": 0.71, "best": 0.11, ... },
    "paragraph_count_distribution": { "p25": 1, "p50": 2, "p75": 4, "p95": 8 },
    "sentence_length_distribution": { "p25": 8, "p50": 14, "p75": 22, "p95": 38 },

    // YouTube fields (only when modality=youtube):
    "video_segment_count_distribution": { ... },
    "intro_outro_template_presence": 0.85,
    "on_screen_text_density": 0.23,           // OCR-derived signal

    // Podcast fields (only when modality=podcast):
    "monologue_vs_dialogue_ratio": 0.64,
    "topic_shift_rate_per_minute": 0.11,
    "guest_interaction_patterns": { ... }
  },

  // Category 3: Stylometric — character-level; cross-modality (from transcript for audio/video)
  "stylometric": {
    "char_ngram_3_top500_uri": "pgvector://...",   // 3-gram distribution embedding
    "char_ngram_4_top500_uri": "pgvector://...",
    "char_ngram_5_top500_uri": "pgvector://...",
    "punctuation_profile": {
      "exclamation_per_1k": 3.2,
      "question_per_1k": 4.8,
      "ellipsis_per_1k": 0.9,
      "em_dash_per_1k": 0.4,
      "oxford_comma_ratio": 0.71
    },
    "case_profile": {
      "all_caps_word_per_1k": 0.8,
      "sentence_case_ratio": 0.94,
      "title_case_outside_proper_nouns_per_1k": 2.1
    }
  },

  // Category 4: Prosodic — audio-derived, null for email
  "prosodic": {
    "speaking_rate_wpm": { "p25": 142, "p50": 158, "p75": 174 },
    "pause_distribution": { "short_pct": 0.62, "medium_pct": 0.28, "long_pct": 0.10 },
    "pitch_range_semitones": { "p25": 3.2, "p50": 5.8, "p75": 9.1 },
    "filler_word_rate": { "um_per_1k": 8.2, "uh_per_1k": 5.1, "like_per_1k": 11.8 },
    "energy_variance_index": 0.34
  },

  // Category 5: Semantic — cross-modality, derived from text (or transcript)
  "semantic": {
    "topic_distribution_k20": { "business_ops": 0.31, "personal_logistics": 0.18, ... },
    "phrase_fingerprint_embedding_uri": "pgvector://...",  // 1536-d; aggregate embedding
    "sentiment_baseline": { "valence": 0.12, "arousal": 0.38 },
    "sentiment_range_p10_p90": { "valence": [-0.2, 0.48], "arousal": [0.15, 0.68] }
  },

  // Category 6: Behavioral — cross-modality, contextual
  "behavioral": {
    "formality_baseline": 0.62,               // 0=casual, 1=formal
    "formality_variance": 0.18,
    "energy_baseline": 0.44,
    "confidence_ratio": 1.8,                  // assertives / hedges
    "hedge_markers_per_1k": 4.2,
    "intensifier_markers_per_1k": 6.1,
    "ask_type_distribution": {
      "request_action": 0.41, "request_info": 0.28,
      "share_info": 0.22, "social": 0.09
    }
  }
}
```

### 3.3 `unified_cpp` (cross-modality block)

Only the features that survive cross-modality merge. Computed by weighted merge across present `source_cpps` (weights = token count per source).

Includes: `lexical.*`, `semantic.*`, `behavioral.*`, and a compact `style_embedding` (a single 768-d vector learned contrastively — see §5.3). Excludes all structural (modality-specific) and all prosodic (audio-only) fields.

---

## 4. Multi-modality handling

### 4.1 Why per-source CPPs AND a unified CPP

Two reasons:

- **Detection is modality-specific.** Chimera evaluates incoming *emails*, so it queries `source_cpps.email` for the comparison. It doesn't care about podcast prosody when scoring an email. Having a dedicated email block makes this fast and clean.
- **Generation is often cross-modality.** A composition app asked to write a LinkedIn post for a person who has both video and podcast content benefits from the fused style signal. That's what `unified` is for.

### 4.2 What happens when only one modality exists

Common case for Chimera's initial pilot: only email. Then `source_cpps.email` is populated, the others are null, and `unified` is a downcast copy of `source_cpps.email`'s lexical/semantic/behavioral sections.

**Decision:** Unified CPP always exists (even from a single source) because downstream apps shouldn't have to branch on "do I have one source or many." They always query `unified` unless they specifically need modality context.

### 4.3 How features combine across modalities

- **Function-word frequencies:** weighted average by token count.
- **Char n-grams:** weighted average in probability space, re-normalized.
- **Phrase fingerprint embedding:** weighted mean in vector space.
- **Behavioral features:** weighted average.
- **Topic distribution:** weighted average across Jensen-Shannon-aligned topic spaces (requires shared K=20 topic model trained globally across all writers — see §5.4).
- **Style embedding:** the contrastive model (§5.3) takes multi-modal input and produces a single embedding directly; no manual merge.

---

## 5. How the TDE builds a CPP

Pipeline, per writer:

### 5.1 Ingest
For each source corpus (email maildir, YouTube channel, podcast feed):
1. Extract text (MIME parsing for email, transcript for A/V — use Whisper for audio).
2. PII-scrub: strip named entities, emails, phones, URLs, IDs.
3. Normalize whitespace and encoding; preserve case and punctuation.
4. Compute corpus-level stats.

**Decision:** Use Whisper (large-v3 or equivalent) for A/V transcription at ingest. Budget ~$0.006/minute of audio — negligible for pilot scale.

### 5.2 Feature extraction
Run six feature-category extractors (one per section in §3.2) in parallel.

**Decision:** Each extractor is a pure function `(normalized_corpus) -> feature_dict`. No shared state. Makes parallel processing trivial and makes unit testing cheap.

### 5.3 Style embedding (contrastive)
Train a small contrastive model that takes chunks of a writer's normalized text and produces a 768-d embedding where chunks from the same writer cluster and chunks from different writers separate. Use the Enron 91-writer corpus as training data.

**Decision:** Use a pre-trained sentence encoder (e.g., `all-mpnet-base-v2`) fine-tuned contrastively on writer-pair positive/negative samples. Don't train from scratch. ~2 hours on a single GPU gets us a usable model; we only need the embedding function, not state-of-the-art.

**Honest tradeoff:** This embedding is opaque. Chimera can use it for detection signal, but it can't explain *why* a flag fired in terms of features. Explainable flags come from §3.2 feature deltas (e.g., "function-word divergence 4.2σ"). The embedding is a safety net, not the primary signal.

### 5.4 Global topic model
Train a topic model (BERTopic or LDA with K=20) once, over a pooled corpus from all writers in the TDE. Re-train on schedule (monthly). Every writer's `topic_distribution` is that writer's projection onto the shared topic space, which makes distributions directly comparable across writers.

### 5.5 Validate and store
Run `cpp_validate()`. If passed, write to the CPP store. If failed, log and alert.

---

## 6. Storage

**Decision:** Postgres + pgvector for the primary store.

```sql
CREATE TABLE cpps (
  cpp_id           UUID PRIMARY KEY,
  writer_id        TEXT NOT NULL,
  schema_version   TEXT NOT NULL,
  generated_at     TIMESTAMPTZ NOT NULL,
  source_manifest_hash  TEXT NOT NULL,
  cpp_json         JSONB NOT NULL,          -- everything except embeddings
  style_embedding  VECTOR(768),             -- unified style embedding
  phrase_embedding VECTOR(1536),            -- unified phrase fingerprint
  validation_status TEXT NOT NULL,
  UNIQUE (writer_id, schema_version, source_manifest_hash)
);

CREATE INDEX ON cpps (writer_id);
CREATE INDEX ON cpps USING hnsw (style_embedding vector_cosine_ops);
CREATE INDEX ON cpps USING gin (cpp_json);

CREATE TABLE cpp_source_manifests (
  manifest_hash  TEXT PRIMARY KEY,
  writer_id      TEXT NOT NULL,
  sources        JSONB NOT NULL,            -- [{type, count, date_range, uri}, ...]
  created_at     TIMESTAMPTZ NOT NULL
);
```

**Scale:** 60K writers × ~500 KB CPP = 30 GB primary table. Manageable in a single Postgres instance. Embeddings add ~2 KB each × 60K = ~120 MB. All fits in a $50/month Railway Postgres plan.

**Decision:** One Postgres for TDE's CPP store. Separate from the Railway `selfless-purpose` project that holds Chimera's Enron raw-email corpus — that's a research dataset, not a production store. The TDE deserves its own Railway project, e.g., `tde-prod`.

---

## 7. Versioning and updates

### 7.1 Schema versioning

`schema_version` is semver. Breaking changes require a full re-build of all CPPs. That's fine early on — at pilot scale a rebuild is hours, not days.

### 7.2 Corpus updates (the harder problem)

As a writer generates new content, their CPP should evolve. Options:

- **(a) Full rebuild on schedule** — rebuild every writer's CPP weekly from their full corpus. Simple. Doesn't capture recency.
- **(b) Full rebuild + rolling window** — maintain two CPPs per writer: `lifetime` and `last_90_days`. Detection uses `lifetime` with `last_90_days` as a secondary comparison. Catches evolving voice.
- **(c) Incremental update** — add new content to existing CPP statistics by weighted combine. Fastest but accumulates drift.

**Decision:** Start with **(b)** — lifetime + 90-day. Incremental update (c) is a trap: statistical drift compounds and you lose the ability to trust old baselines. Full rebuild is cheap enough at our scale to just do it right.

### 7.3 Rebuild trigger

`source_manifest_hash` is the content address. If the hash changes (new content added), rebuild. If it hasn't, the CPP is still current — no work to do.

---

## 8. How consumers read CPPs

### 8.1 Chimera (detection)

```python
cpp = cpp_store.get(writer_id, modality="email")
score = chimera_score(incoming_email, cpp)
# Uses cpp.source_cpps.email.lexical.function_word_freqs,
#      cpp.source_cpps.email.stylometric.char_ngram_* embeddings,
#      cpp.unified.style_embedding as secondary signal.
```

Chimera pins a `schema_version` it's compatible with. If the TDE upgrades to 1.1.0, Chimera either upgrades or continues reading 1.0.x CPPs (which stay in the store during the transition).

### 8.2 Email composition (generation)

```python
cpp = cpp_store.get(writer_id, modality="email")
prompt = build_prompt(user_intent, cpp.source_cpps.email)
# Uses cpp.source_cpps.email.structural.email_greeting_pattern_distribution,
#      cpp.source_cpps.email.behavioral.formality_baseline,
#      cpp.source_cpps.email.semantic.phrase_fingerprint_embedding
#      to build style anchors that steer the generation LLM.
```

### 8.3 Cross-modality generation (e.g., LinkedIn post for video creator)

```python
cpp = cpp_store.get(writer_id, modality=None)  # unified
prompt = build_prompt(user_intent, cpp.unified)
```

---

## 9. Open questions / decisions to push back on

I made v1 calls on these. Each is a place where your instinct might differ from mine — the earlier we disagree, the cheaper the fix.

1. **Per-writer language models — in or out?**
   Storing a small fine-tuned LM per writer (LoRA adapter, ~20 MB each) would give us a genuinely high-signal perplexity score for detection. But at 60K seats × 20 MB = 1.2 TB. My v1 says **out** — use the style_embedding as the neural signal instead. Your call. If you want per-writer LMs, I'd gate them to the top-risk writers (executives, finance) rather than everyone.

2. **Topic model granularity — K=20?**
   Coarse topic distributions (K=20) preserve privacy and stay comparable across writers. Fine-grained (K=200) gives richer generation steering but starts identifying specific projects/companies. My v1 says **K=20**. A hybrid (K=20 stored, K=200 computed on demand inside customer environment only) is possible.

3. **Whisper for A/V at ingest — or delegate to the customer?**
   Whisper is cheap at pilot scale but at enterprise scale ($0.006/min × 60K users × 5 hrs video each = ~$108K/month). My v1 says **TDE runs Whisper centrally** for MVP, but we should plan for "customer's own transcription" as a scale path.

4. **Sovereignty validator — how strict?**
   My v1 rules (no phrases > 4 tokens, freq >= 5) are conservative. They might strip too much signal. Real counter: run the validator against one Enron writer's CPP and measure how much signal we lose. I can do that test before we lock the rules.

5. **Writer id scheme — what is it?**
   For TDE-internal id, my v1 assumes an opaque UUID per writer. But writers exist in customer tenants. Do we want `writer_id` to be `(tenant_id, external_user_id)` so we can multi-tenant cleanly? Probably yes. My v1 calls this out but doesn't decide.

6. **Unified CPP — computed eagerly or lazily?**
   My v1 says eagerly (stored alongside source CPPs). Alternative: compute lazily at query time from the source CPPs. Lazy saves storage but costs ms per query. At Chimera hot-path latency budgets (<50ms), lazy is risky. My v1 says **eager**, but I'm 70/30 on this.

---

## 10. What I need to build next (order matters)

1. `cpp_schema.py` — dataclasses for the structures in §3. Makes everything else type-checkable.
2. `cpp_validate.py` — the sovereignty validator. Write it before any extractor so no CPP can exist without passing it.
3. The six feature extractors (§5.2). Start with `lexical.py` and `stylometric.py` — those are highest-signal and have the most prior art.
4. `style_embedding.py` — fine-tune the contrastive model on Enron pairs.
5. `topic_model.py` — train the global K=20 topic model on Enron pooled corpus.
6. `cpp_builder.py` — orchestration; reads corpus, runs extractors, assembles, validates, stores.
7. First real CPPs: 40 Enron writers. This is the Gate 2 unlock from `Chimera_Secured/pilot_readiness_plan.md`.

No step in this list is large. The full sequence to 40 Enron CPPs is 2-3 working sessions if nothing surprises us.
