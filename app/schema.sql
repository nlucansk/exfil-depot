CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id          INT PRIMARY KEY,
    hub_id      INT,
    title       TEXT NOT NULL,
    slug        TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS spt_versions (
    id            INT PRIMARY KEY,
    version       TEXT NOT NULL,
    version_major INT,
    version_minor INT,
    version_patch INT,
    mod_count     INT,
    link          TEXT,
    color_class   TEXT
);

CREATE TABLE IF NOT EXISTS mods (
    id                  INT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'mod',   -- 'mod' | 'addon'
    hub_id              INT,
    guid                TEXT,
    name                TEXT NOT NULL,
    slug                TEXT,
    teaser              TEXT,
    description_html    TEXT,
    thumbnail_url       TEXT,
    thumbnail_local     TEXT,
    downloads           BIGINT DEFAULT 0,
    favourites          INT DEFAULT 0,
    detail_url          TEXT,
    featured            BOOLEAN DEFAULT FALSE,
    contains_ads        BOOLEAN DEFAULT FALSE,
    contains_ai_content BOOLEAN DEFAULT FALSE,
    cheat_notice        BOOLEAN DEFAULT FALSE,
    fika_compatibility  TEXT,
    category_id         INT,
    license_name        TEXT,
    license_link        TEXT,
    owner_id            INT,
    owner_name          TEXT,
    published_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ,
    raw                 JSONB NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS mods_downloads_idx ON mods (downloads DESC);
CREATE INDEX IF NOT EXISTS mods_updated_idx   ON mods (updated_at DESC);
CREATE INDEX IF NOT EXISTS mods_category_idx  ON mods (category_id);

CREATE TABLE IF NOT EXISTS mod_versions (
    id                     INT NOT NULL,
    kind                   TEXT NOT NULL DEFAULT 'mod',
    mod_id                 INT NOT NULL,
    version                TEXT,
    description_html       TEXT,
    forge_link             TEXT,     -- dead after 2026-08-12
    github_url             TEXT,     -- survives (resolved release asset)
    external_url           TEXT,     -- author-hosted (gdrive/mediafire/...)
    local_path             TEXT,     -- served from this container: /files/...
    content_length         BIGINT,
    spt_version_constraint TEXT,
    downloads              BIGINT DEFAULT 0,
    fika_compatibility     TEXT,
    published_at           TIMESTAMPTZ,
    created_at             TIMESTAMPTZ,
    raw                    JSONB NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS mv_mod_idx ON mod_versions (kind, mod_id);

CREATE TABLE IF NOT EXISTS version_dependencies (
    kind         TEXT NOT NULL,
    version_id   INT NOT NULL,
    dep_mod_id   INT NOT NULL,
    dep_name     TEXT,
    dep_slug     TEXT,
    dep_guid     TEXT,
    PRIMARY KEY (kind, version_id, dep_mod_id)
);

CREATE TABLE IF NOT EXISTS source_code_links (
    kind    TEXT NOT NULL,
    mod_id  INT NOT NULL,
    url     TEXT NOT NULL,
    label   TEXT,
    PRIMARY KEY (kind, mod_id, url)
);
