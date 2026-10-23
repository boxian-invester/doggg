-- Run once in Supabase SQL Editor. All tables are private by default to anon/authenticated clients.
create table if not exists public.stock_universe (
 symbol text primary key check (symbol ~ '^[0-9]{4,6}$'),
 name text not null, market text not null check (market in ('TWSE','TPEx')),
 active boolean not null default true,
 updated_at timestamptz not null default now()
);
create table if not exists public.daily_bars (
 symbol text not null references public.stock_universe(symbol),
 trade_date date not null,
 open numeric(18,5) not null, high numeric(18,5) not null,
 low numeric(18,5) not null, close numeric(18,5) not null,
 volume bigint not null check(volume >= 0),
 source text not null, adjusted boolean not null default true,
 loaded_at timestamptz not null default now(),
 primary key(symbol,trade_date),
 check (open>0 and high>=greatest(open,close,low) and low<=least(open,close,high))
);
create index if not exists idx_daily_bars_date on public.daily_bars(trade_date desc);
create table if not exists public.scan_runs (
 id bigint generated always as identity primary key,
 started_at timestamptz not null default now(),
 finished_at timestamptz,
 universe_count integer not null default 0,
 eligible_count integer not null default 0,
 failed_count integer not null default 0,
 latest_trade_date date,
 status text not null default 'running',
 notes text
);
create table if not exists public.scan_results (
 run_id bigint not null references public.scan_runs(id) on delete cascade,
 symbol text not null references public.stock_universe(symbol),
 trade_date date not null,
 score integer not null check (score between 0 and 100),
 close numeric(18,5), volume_ratio numeric(12,4),
 reasons jsonb not null default '[]'::jsonb,
 primary key(run_id,symbol)
);
create index if not exists idx_scan_results_rank on public.scan_results(run_id,score desc);
-- Explicitly deny access through Supabase public API keys; Python uses server-side DB credentials.
alter table public.stock_universe enable row level security;
alter table public.daily_bars enable row level security;
alter table public.scan_runs enable row level security;
alter table public.scan_results enable row level security;
-- No anon/authenticated policies are defined.
