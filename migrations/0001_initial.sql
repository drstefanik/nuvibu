BEGIN;

SET LOCAL search_path TO public, pg_catalog;

CREATE TABLE public.episodes (
    id VARCHAR(36) NOT NULL,
    title VARCHAR(180) NOT NULL,
    working_slug VARCHAR(180) NOT NULL,
    age_min_months INTEGER NOT NULL,
    age_max_months INTEGER NOT NULL,
    theme VARCHAR(100) NOT NULL,
    hook VARCHAR(220) NOT NULL,
    target_words JSON NOT NULL,
    featured_characters JSON NOT NULL,
    duration_seconds INTEGER NOT NULL,
    bpm INTEGER NOT NULL,
    music_direction TEXT NOT NULL DEFAULT '',
    visual_pacing VARCHAR(32) NOT NULL,
    language VARCHAR(10) NOT NULL,
    status VARCHAR(16) NOT NULL,
    concept_json JSON NOT NULL,
    lyrics_text TEXT,
    storyboard_json JSON NOT NULL,
    qc_json JSON NOT NULL,
    estimated_cost_usd DOUBLE PRECISION NOT NULL,
    actual_cost_usd DOUBLE PRECISION NOT NULL,
    publish_title VARCHAR(220),
    publish_description TEXT,
    publish_tags JSON NOT NULL,
    scheduled_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT episodes_pkey PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_episodes_working_slug
    ON public.episodes (working_slug);

CREATE INDEX ix_episodes_status
    ON public.episodes (status);

CREATE TABLE public.assets (
    id VARCHAR(36) NOT NULL,
    episode_id VARCHAR(36) NOT NULL,
    kind VARCHAR(19) NOT NULL,
    provider VARCHAR(80) NOT NULL,
    variant INTEGER NOT NULL,
    path TEXT NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    duration_seconds DOUBLE PRECISION,
    width INTEGER,
    height INTEGER,
    selected BOOLEAN NOT NULL,
    metadata_json JSON NOT NULL,
    cost_usd DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT assets_pkey PRIMARY KEY (id),
    CONSTRAINT assets_episode_id_fkey
        FOREIGN KEY (episode_id)
        REFERENCES public.episodes (id)
        ON DELETE CASCADE
);

CREATE INDEX ix_assets_episode_id
    ON public.assets (episode_id);

CREATE INDEX ix_assets_kind
    ON public.assets (kind);

CREATE TABLE public.jobs (
    id VARCHAR(36) NOT NULL,
    episode_id VARCHAR(36) NOT NULL,
    job_type VARCHAR(60) NOT NULL,
    status VARCHAR(9) NOT NULL,
    attempt INTEGER NOT NULL,
    payload_json JSON NOT NULL,
    result_json JSON NOT NULL,
    error_text TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT jobs_pkey PRIMARY KEY (id),
    CONSTRAINT jobs_episode_id_fkey
        FOREIGN KEY (episode_id)
        REFERENCES public.episodes (id)
        ON DELETE CASCADE
);

CREATE INDEX ix_jobs_episode_id
    ON public.jobs (episode_id);

CREATE INDEX ix_jobs_job_type
    ON public.jobs (job_type);

CREATE INDEX ix_jobs_status
    ON public.jobs (status);

CREATE UNIQUE INDEX uq_jobs_active_episode_type
    ON public.jobs (episode_id, job_type)
    WHERE status IN ('PENDING', 'RUNNING');

CREATE TABLE public.publish_records (
    id VARCHAR(36) NOT NULL,
    episode_id VARCHAR(36) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    external_id VARCHAR(128),
    privacy_status VARCHAR(20) NOT NULL,
    made_for_kids BOOLEAN NOT NULL,
    response_json JSON NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT publish_records_pkey PRIMARY KEY (id),
    CONSTRAINT publish_records_episode_id_fkey
        FOREIGN KEY (episode_id)
        REFERENCES public.episodes (id)
        ON DELETE CASCADE
);

CREATE INDEX ix_publish_records_episode_id
    ON public.publish_records (episode_id);

CREATE INDEX ix_publish_records_external_id
    ON public.publish_records (external_id);

CREATE TABLE public.metric_snapshots (
    id VARCHAR(36) NOT NULL,
    episode_id VARCHAR(36) NOT NULL,
    captured_at TIMESTAMP WITH TIME ZONE NOT NULL,
    views INTEGER NOT NULL,
    watch_minutes DOUBLE PRECISION NOT NULL,
    average_view_duration_seconds DOUBLE PRECISION NOT NULL,
    average_view_percentage DOUBLE PRECISION NOT NULL,
    impressions INTEGER NOT NULL,
    impressions_ctr DOUBLE PRECISION,
    subscribers_gained INTEGER NOT NULL,
    relative_retention DOUBLE PRECISION,
    retention_curve_json JSON NOT NULL,
    source VARCHAR(40) NOT NULL,
    CONSTRAINT metric_snapshots_pkey PRIMARY KEY (id),
    CONSTRAINT metric_snapshots_episode_id_fkey
        FOREIGN KEY (episode_id)
        REFERENCES public.episodes (id)
        ON DELETE CASCADE
);

CREATE INDEX ix_metric_snapshots_episode_id
    ON public.metric_snapshots (episode_id);

COMMIT;
