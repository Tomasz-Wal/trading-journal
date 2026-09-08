
create extension if not exists pgcrypto;

create table if not exists public.trades (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  trade_time timestamptz not null,
  instrument text not null,
  side text not null check (side in ('LONG','SHORT')),
  setup text not null default '',
  entry numeric,
  exit numeric,
  qty integer,
  pnl numeric not null default 0,
  rating integer check (rating is null or rating between 1 and 5),
  tags text not null default '',
  notes text not null default '',
  lesson text not null default '',
  screenshot_path text
);

create index if not exists trades_trade_time_idx on public.trades (trade_time desc);
create index if not exists trades_instrument_idx on public.trades (instrument);
create index if not exists trades_side_idx on public.trades (side);
create index if not exists trades_setup_idx on public.trades (setup);

alter table public.trades enable row level security;

-- No client-side direct access is needed.
-- The FastAPI backend uses the Supabase service role key.
