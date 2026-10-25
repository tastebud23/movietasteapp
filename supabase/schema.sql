-- Run in your Supabase SQL editor before enabling account-backed profiles.
-- Separate names preserve all existing legacy tables and data.
begin;

create table if not exists public.movietaste_profiles (
  owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  profile_name text not null check (length(profile_name) between 1 and 60),
  taste_summary text not null default '',
  latest_taste_read text not null default '',
  latest_blueprint jsonb,
  primary key (owner_id, profile_name)
);

create table if not exists public.movietaste_movie_feedback (
  owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  profile_name text not null,
  movie_title text not null,
  status text not null check (status in ('saved', 'removed', 'watched')),
  source_slot text not null default '',
  updated_at timestamptz not null default now(),
  primary key (owner_id, profile_name, movie_title),
  foreign key (owner_id, profile_name) references public.movietaste_profiles on delete cascade
);

create table if not exists public.movietaste_recommendation_events (
  owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  event_id uuid not null default gen_random_uuid(),
  profile_name text not null,
  input_movies text not null default '',
  output_text text not null default '',
  latest_taste_read text not null default '',
  long_term_taste_summary_used text not null default '',
  reroll_count integer not null default 0,
  taste_blueprint jsonb,
  recommended_titles jsonb not null default '[]',
  created_at timestamptz not null default now(),
  event_type text not null default 'solo',
  profile_1_name text,
  profile_2_name text,
  best_pick text,
  pair_mode_type text,
  primary key (owner_id, event_id)
);
create index if not exists movietaste_events_profile_time
  on public.movietaste_recommendation_events (owner_id, profile_name, created_at);

create table if not exists public.movietaste_movie_cache (
  owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  watchmode_id text not null,
  title text,
  year integer,
  metadata jsonb,
  streaming_links jsonb,
  last_updated timestamptz not null default now(),
  primary key (owner_id, watchmode_id)
);

-- The authenticated JWT, not a profile name supplied by the UI, establishes ownership.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'movietaste_profiles', 'movietaste_movie_feedback',
    'movietaste_recommendation_events', 'movietaste_movie_cache'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('alter table public.%I force row level security', table_name);
    execute format('revoke all on public.%I from anon, public', table_name);
    execute format('grant select, insert, update, delete on public.%I to authenticated', table_name);
    execute format('drop policy if exists account_ownership on public.%I', table_name);
    execute format(
      'create policy account_ownership on public.%I for all to authenticated using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id)',
      table_name
    );
  end loop;
end $$;
commit;
