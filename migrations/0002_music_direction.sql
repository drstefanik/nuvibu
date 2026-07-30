BEGIN;

SET LOCAL search_path TO public, pg_catalog;

ALTER TABLE public.episodes
    ADD COLUMN IF NOT EXISTS music_direction TEXT NOT NULL DEFAULT '';

COMMIT;
