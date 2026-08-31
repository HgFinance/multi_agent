begin;

-- LangGraph's PostgresSaver is the durable execution checkpoint for the CEO
-- mandate workflow.  The application runtime deliberately cannot create
-- schema objects, so provision the pinned langgraph-checkpoint-postgres
-- 3.1.2 schema through the canonical control-database migration chain.
create table if not exists public.checkpoint_migrations (
  v integer primary key
);

create table if not exists public.checkpoints (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  parent_checkpoint_id text,
  type text,
  checkpoint jsonb not null,
  metadata jsonb not null default '{}',
  primary key (thread_id, checkpoint_ns, checkpoint_id)
);

create table if not exists public.checkpoint_blobs (
  thread_id text not null,
  checkpoint_ns text not null default '',
  channel text not null,
  version text not null,
  type text not null,
  blob bytea,
  primary key (thread_id, checkpoint_ns, channel, version)
);

create table if not exists public.checkpoint_writes (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  task_id text not null,
  idx integer not null,
  channel text not null,
  type text,
  blob bytea not null,
  task_path text not null default '',
  primary key (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- Keep existing partial installations compatible with the pinned package
-- migration set as well as fresh installations.
alter table public.checkpoint_blobs alter column blob drop not null;
alter table public.checkpoint_writes
  add column if not exists task_path text not null default '';

create index if not exists checkpoints_thread_id_idx
  on public.checkpoints (thread_id);
create index if not exists checkpoint_blobs_thread_id_idx
  on public.checkpoint_blobs (thread_id);
create index if not exists checkpoint_writes_thread_id_idx
  on public.checkpoint_writes (thread_id);

-- The generic runtime inherits service_role.  Grant only the DML required by
-- PostgresSaver; never grant CREATE on public to an application login.
grant usage on schema public to service_role;
revoke all privileges on table public.checkpoint_migrations,
  public.checkpoints, public.checkpoint_blobs, public.checkpoint_writes from public;
grant select, insert, update, delete on table public.checkpoint_migrations,
  public.checkpoints, public.checkpoint_blobs, public.checkpoint_writes to service_role;

commit;
