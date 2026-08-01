-- studykernel schema (v0)
--
-- Sources: DESIGN.md §15 (schema deltas), Datasourcing.md §6 (role/style/
-- provenance fields), DATA_SOURCING_ELAR.md §2.3 (passages) and §2.4 (the
-- `anchored` explanation policy).
--
-- Naming note: DESIGN.md §15 carries `exam_id` forward from the v0.2 lineage.
-- v0.3 restructured the system around products rather than exam packs, so the
-- column is `product_id` here. Same key, current vocabulary.
--
-- No table, column, or CHECK constraint in this file may name a specific exam.
-- tests/kernel_purity_test.py enforces that.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- products

CREATE TABLE IF NOT EXISTS products (
    product_id     TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    objective_type TEXT NOT NULL,
    config_json    TEXT NOT NULL,          -- full merged product + objective pack
    pack_digest    TEXT NOT NULL,          -- detects pack edits between sessions
    loaded_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    product_id         TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    slug               TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    item_count         INTEGER,            -- items in a real sitting, NULL if untimed/unbounded
    explanation_policy TEXT NOT NULL
        CHECK (explanation_policy IN ('withheld', 'pinned', 'pinned_strict', 'anchored')),
    blueprint_weight   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (product_id, slug)
);

-- --------------------------------------------------------------- taxonomy

CREATE TABLE IF NOT EXISTS tags (
    product_id      TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    slug            TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    section_slug    TEXT,
    parent_slug     TEXT,                  -- taxonomy nesting, not prerequisites
    level           INTEGER,               -- diagnostic level where the pack defines one
    coverage_weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (product_id, slug)
);

-- Prerequisite DAG. Seeded edges arrive from the pack at confidence 1.0;
-- learned edges accrue from diagnosis `prerequisite_gaps`. DESIGN.md §9.
CREATE TABLE IF NOT EXISTS tag_edges (
    product_id     TEXT NOT NULL,
    parent_slug    TEXT NOT NULL,          -- prerequisite
    child_slug     TEXT NOT NULL,          -- depends on parent
    confidence     REAL NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('seed', 'learned')),
    n_observations INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, parent_slug, child_slug)
);

-- ------------------------------------------------------------------ items

CREATE TABLE IF NOT EXISTS passages (
    passage_id TEXT PRIMARY KEY,           -- sha256(normalized text)[:16]
    text       TEXT NOT NULL,
    genre      TEXT,
    source     TEXT NOT NULL,
    license    TEXT NOT NULL,
    word_count INTEGER
);

CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,         -- sha256(product_id + normalized stem)[:16]
    product_id   TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    section_slug TEXT NOT NULL,
    item_type    TEXT NOT NULL,            -- mc-one | numeric | essay | ...
    stem         TEXT NOT NULL,
    choices_json TEXT,                     -- JSON array, NULL for free-response
    answer_key   TEXT,                     -- NULL only for essay items
    official_expl TEXT,
    passage_id   TEXT REFERENCES passages(passage_id),

    -- Corpus role and voice. Datasourcing.md §1 and §6.
    role  TEXT NOT NULL DEFAULT 'pool'   CHECK (role  IN ('anchor', 'pool')),
    style TEXT NOT NULL DEFAULT 'proxy'  CHECK (style IN ('native', 'proxy')),

    -- Provenance. Never optional: bad labels feed the DAG, the DAG drives
    -- routing, and a confidently wrong study plan is invisible. DESIGN.md §10.
    source            TEXT NOT NULL,
    source_ref        TEXT,
    license           TEXT NOT NULL,
    redistributable   INTEGER NOT NULL DEFAULT 0,
    model_transformed INTEGER NOT NULL DEFAULT 0,

    -- Difficulty. Hand and imported labels are priors only; Elo does the real
    -- work from attempt data. DESIGN.md §6.1.
    difficulty_prior     REAL,
    difficulty_prior_src TEXT,

    rating           REAL NOT NULL,
    rating_deviation REAL NOT NULL,
    volatility       REAL NOT NULL,
    n_attempts       INTEGER NOT NULL DEFAULT 0,

    flags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_band
    ON items (product_id, section_slug, rating);
CREATE INDEX IF NOT EXISTS idx_items_passage ON items (passage_id);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    tag_slug     TEXT NOT NULL,
    label_source TEXT NOT NULL
        CHECK (label_source IN ('official', 'human', 'model', 'imported')),
    reviewed     INTEGER NOT NULL DEFAULT 0,
    confidence   REAL,
    PRIMARY KEY (item_id, tag_slug)
);

CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags (tag_slug);

-- Hand-labeled reference set. Every labeling-prompt revision scores against it.
CREATE TABLE IF NOT EXISTS gold_labels (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    tag_slug   TEXT NOT NULL,
    labeled_by TEXT NOT NULL,
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (item_id, tag_slug)
);

-- --------------------------------------------------------- learner state

CREATE TABLE IF NOT EXISTS learners (
    learner_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learner_state (
    learner_id       TEXT NOT NULL REFERENCES learners(learner_id) ON DELETE CASCADE,
    product_id       TEXT NOT NULL,
    tag_slug         TEXT NOT NULL,
    rating           REAL NOT NULL,
    rating_deviation REAL NOT NULL,
    volatility       REAL NOT NULL,
    n_attempts       INTEGER NOT NULL DEFAULT 0,
    -- Mastery sits on the lower bound, never the point estimate. DESIGN.md §6.2.
    reliability_lo   REAL,
    reliability_hi   REAL,
    variance_rolling REAL,
    trigger_miss_rate REAL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (learner_id, product_id, tag_slug)
);

-- ------------------------------------------------- append-only activity log

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id   TEXT NOT NULL REFERENCES learners(learner_id) ON DELETE CASCADE,
    product_id   TEXT NOT NULL,
    item_id      TEXT NOT NULL REFERENCES items(item_id),
    started_at   TEXT NOT NULL,
    submitted_at TEXT NOT NULL,

    -- Pre-answer capture, written blind. DESIGN.md §9.
    confidence          INTEGER CHECK (confidence BETWEEN 1 AND 3),
    rationale           TEXT,
    verification_method TEXT,               -- product-gated capture field

    answer_given TEXT,
    correct      INTEGER NOT NULL CHECK (correct IN (0, 1)),
    -- Continuous where correctness is binary. DESIGN.md §9.
    min_hint_level INTEGER NOT NULL DEFAULT 0 CHECK (min_hint_level BETWEEN 0 AND 5),

    time_to_first_selection_ms INTEGER,
    time_total_ms              INTEGER,
    pass_number                INTEGER NOT NULL DEFAULT 1,

    rating_delta REAL,                      -- feeds variance_rolling
    resolved     INTEGER NOT NULL DEFAULT 0 -- explain-back gate cleared
);

CREATE INDEX IF NOT EXISTS idx_attempts_learner
    ON attempts (learner_id, product_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON attempts (item_id);

CREATE TABLE IF NOT EXISTS exchanges (
    exchange_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,           -- keeps early data interpretable
    briefing       TEXT NOT NULL,
    payload_json   TEXT,                    -- the structured return block
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    attempt_id             INTEGER PRIMARY KEY REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    error_code             TEXT NOT NULL,
    prerequisite_gaps_json TEXT NOT NULL DEFAULT '[]',
    one_fix                TEXT NOT NULL,   -- singular by prompt contract
    trigger_miss           INTEGER NOT NULL DEFAULT 0,
    explain_back           TEXT,
    explain_back_ok        INTEGER
);

-- Product data, not kernel constants. DESIGN.md §5.
CREATE TABLE IF NOT EXISTS error_code_weights (
    product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    code       TEXT NOT NULL,
    weight     REAL NOT NULL,
    PRIMARY KEY (product_id, code)
);

-- ------------------------------------------------------- objective tracking

-- Position over time is itself a progress artifact. DESIGN.md §15.
CREATE TABLE IF NOT EXISTS objective_state (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id        TEXT NOT NULL,
    product_id        TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    p_success         REAL NOT NULL,
    position_estimate REAL,
    margin            REAL,
    satisfied         INTEGER NOT NULL DEFAULT 0,
    computed_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_objective_state_time
    ON objective_state (learner_id, product_id, computed_at);
