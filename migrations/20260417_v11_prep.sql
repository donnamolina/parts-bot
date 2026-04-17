-- v11 pre-flight migration (applied 2026-04-17)
-- Project: egcnlimndtymtqqonnnp (parts-bot Supabase)

ALTER TABLE parts_sessions
  ADD COLUMN IF NOT EXISTS history jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS state text,
  ADD COLUMN IF NOT EXISTS last_activity_at timestamptz DEFAULT now(),
  ADD COLUMN IF NOT EXISTS total_tokens_estimate int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS turn_count int DEFAULT 0;

ALTER TABLE parts_cache
  ADD COLUMN IF NOT EXISTS vin text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_parts_corrections_unique
  ON parts_corrections (
    lower(vehicle_make), lower(vehicle_model),
    lower(part_name_original), lower(part_name_corrected)
  );

CREATE TABLE IF NOT EXISTS parts_agent_events (
  id bigserial PRIMARY KEY,
  phone_number text NOT NULL,
  session_code text,
  event_type text NOT NULL,
  tool_name text,
  args jsonb,
  result jsonb,
  latency_ms int,
  input_tokens int,
  output_tokens int,
  created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_events_phone_time
  ON parts_agent_events (phone_number, created_at DESC);
