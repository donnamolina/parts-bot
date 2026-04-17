-- v11 agent loop — conversations table
-- Applied 2026-04-17 to Supabase project egcnlimndtymtqqonnnp

CREATE TABLE IF NOT EXISTS conversations (
  user_id text PRIMARY KEY,
  messages jsonb NOT NULL DEFAULT '[]'::jsonb,
  active_session_code text,
  last_activity_at timestamptz DEFAULT now(),
  total_tokens_estimate int DEFAULT 0,
  turn_count int DEFAULT 0,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversations_last_activity
  ON conversations (last_activity_at DESC);
