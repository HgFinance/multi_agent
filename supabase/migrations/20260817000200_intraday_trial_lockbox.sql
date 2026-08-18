begin;

-- Intraday discovery is adaptive: once a candidate lineage has observed a
-- session, a descendant must never present that date as independent forward
-- evidence.  These evidence tables are an append-only lockbox.  They deliberately
-- contain no migration-time backfill; the evaluator records an exposure only
-- when it actually reads that session.

create table quant.intraday_candidate_lineages (
  candidate_lineage_id          uuid primary key default gen_random_uuid(),
  root_lineage_id               uuid not null,
  parent_lineage_id             uuid,
  hypothesis_id                 uuid not null
    references quant.hypotheses(hypothesis_id) on delete restrict,
  candidate_identity_fingerprint text not null,
  candidate_ast_fingerprint     text not null,
  semantic_plan_fingerprint     text not null,
  baseline_ast_fingerprint      text,
  feature_spec_fingerprint      text not null,
  label_spec_fingerprint        text not null,
  model_spec_fingerprint        text not null,
  economic_family_id            text not null,
  evaluator_version             text not null,
  cost_model_version            text not null,
  created_by                    text not null,
  metadata                      jsonb not null default '{}'::jsonb,
  created_at                    timestamptz not null default now(),

  constraint uq_intraday_candidate_identity unique
    (hypothesis_id, candidate_identity_fingerprint),
  constraint uq_intraday_candidate_root_pair unique
    (candidate_lineage_id, root_lineage_id),
  constraint chk_intraday_candidate_identity_hash check
    (candidate_identity_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_ast_hash check
    (candidate_ast_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_semantic_hash check
    (semantic_plan_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_baseline_hash check
    (baseline_ast_fingerprint is null
     or baseline_ast_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_feature_hash check
    (feature_spec_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_label_hash check
    (label_spec_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_model_hash check
    (model_spec_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_candidate_root_shape check
    ((parent_lineage_id is null and root_lineage_id = candidate_lineage_id)
     or parent_lineage_id is not null),
  constraint chk_intraday_candidate_no_self_parent check
    (parent_lineage_id is null or parent_lineage_id <> candidate_lineage_id),
  constraint chk_intraday_candidate_text check
    (btrim(economic_family_id) <> ''
     and btrim(evaluator_version) <> ''
     and btrim(cost_model_version) <> ''
     and btrim(created_by) <> ''),
  constraint chk_intraday_candidate_metadata check
    (jsonb_typeof(metadata) = 'object')
);

-- The composite parent FK proves that every child keeps the same root as its
-- parent.  Both self-references are deferred so a root may point to itself in
-- the statement that creates it.
alter table quant.intraday_candidate_lineages
  add constraint fk_intraday_candidate_root
  foreign key (root_lineage_id)
  references quant.intraday_candidate_lineages(candidate_lineage_id)
  on delete restrict deferrable initially deferred,
  add constraint fk_intraday_candidate_parent_root
  foreign key (parent_lineage_id, root_lineage_id)
  references quant.intraday_candidate_lineages
    (candidate_lineage_id, root_lineage_id)
  on delete restrict deferrable initially deferred;

create sequence quant.intraday_forward_test_index_seq as bigint;

create table quant.intraday_experiment_rungs (
  experiment_rung_id            uuid primary key default gen_random_uuid(),
  candidate_lineage_id          uuid not null,
  root_lineage_id               uuid not null,
  experiment_id                 uuid not null
    references quant.experiments(experiment_id) on delete restrict,
  predecessor_rung_id           uuid
    references quant.intraday_experiment_rungs(experiment_rung_id)
    on delete restrict deferrable initially deferred,
  dataset_id                    uuid not null
    references quant.dataset_manifests(dataset_id) on delete restrict,
  rung                          text not null,
  evidence_purpose              text not null,
  planned_session_dates         date[] not null,
  planned_session_count         integer not null,
  planned_instrument_ids        uuid[] not null,
  planned_instrument_count      integer not null,
  session_set_fingerprint       text not null,
  instrument_set_fingerprint    text not null,
  rung_plan_fingerprint         text not null,
  selection_policy_version      text not null,
  dataset_cutoff                timestamptz not null,
  source_watermark              jsonb not null,
  lockbox_cutoff_session_date   date,
  forward_test_index            bigint,
  allocation_reason             text not null,
  allocated_by                  text not null,
  allocated_at                  timestamptz not null default now(),

  constraint uq_intraday_candidate_experiment_rung unique
    (candidate_lineage_id, experiment_id, rung),
  constraint uq_intraday_experiment_rung unique (experiment_id, rung),
  constraint uq_intraday_rung_lineage_root unique
    (experiment_rung_id, candidate_lineage_id, root_lineage_id),
  constraint fk_intraday_rung_lineage_root foreign key
    (candidate_lineage_id, root_lineage_id)
    references quant.intraday_candidate_lineages
      (candidate_lineage_id, root_lineage_id)
    on delete restrict,
  constraint chk_intraday_rung_name check
    (rung in
      ('CALIBRATION', 'DISCOVERY_6', 'VALIDATION_20', 'FULL_60', 'FORWARD')),
  constraint chk_intraday_rung_purpose check (
    (rung in ('CALIBRATION', 'DISCOVERY_6', 'VALIDATION_20', 'FULL_60')
      and evidence_purpose = 'ADAPTIVE_SEARCH')
    or (rung = 'FORWARD' and evidence_purpose = 'INDEPENDENT_FORWARD')
  ),
  constraint chk_intraday_rung_session_count check (
    cardinality(planned_session_dates) = planned_session_count
    and (
      (rung = 'CALIBRATION' and planned_session_count between 1 and 5)
      or (rung = 'DISCOVERY_6' and planned_session_count = 6)
      or (rung = 'VALIDATION_20' and planned_session_count = 20)
      or (rung = 'FULL_60' and planned_session_count = 60)
      or (rung = 'FORWARD' and planned_session_count >= 20)
    )
  ),
  constraint chk_intraday_rung_instrument_count check
    (planned_instrument_count >= 1
     and cardinality(planned_instrument_ids) = planned_instrument_count),
  constraint chk_intraday_rung_hashes check
    (session_set_fingerprint ~ '^[0-9a-f]{64}$'
     and instrument_set_fingerprint ~ '^[0-9a-f]{64}$'
     and rung_plan_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_rung_watermark check
    (jsonb_typeof(source_watermark) = 'object'
     and source_watermark <> '{}'::jsonb),
  constraint chk_intraday_rung_lockbox_cutoff check
    ((rung = 'FORWARD' and lockbox_cutoff_session_date is not null)
     or (rung <> 'FORWARD' and lockbox_cutoff_session_date is null)),
  constraint chk_intraday_rung_forward_test_index check
    ((rung = 'FORWARD' and forward_test_index is not null
       and forward_test_index > 0)
     or (rung <> 'FORWARD' and forward_test_index is null)),
  constraint chk_intraday_rung_text check
    (btrim(selection_policy_version) <> ''
     and btrim(allocation_reason) <> ''
     and btrim(allocated_by) <> '')
);

-- A durable access marker is committed before the worker is permitted to read
-- raw market rows.  Content evidence is appended separately afterwards: a
-- crash between the two leaves the date consumed, never silently fresh again.
create table quant.intraday_session_accesses (
  session_access_id             uuid primary key default gen_random_uuid(),
  experiment_rung_id            uuid not null,
  candidate_lineage_id          uuid not null,
  root_lineage_id               uuid not null,
  dataset_id                    uuid not null
    references quant.dataset_manifests(dataset_id) on delete restrict,
  session_date                  date not null,
  access_purpose                text not null,
  knowledge_clock_mode          text not null,
  access_fingerprint            text not null,
  instrument_ids                uuid[] not null,
  instrument_count              integer not null,
  knowledge_cutoff              timestamptz not null,
  source_watermark              jsonb not null,
  accessed_by                   text not null,
  accessed_at                   timestamptz not null default now(),

  constraint uq_intraday_root_session_access unique
    (root_lineage_id, session_date),
  constraint uq_intraday_access_rung_root unique
    (session_access_id, experiment_rung_id, candidate_lineage_id,
     root_lineage_id),
  constraint fk_intraday_access_rung_lineage_root foreign key
    (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    references quant.intraday_experiment_rungs
      (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    on delete restrict,
  constraint chk_intraday_access_purpose check
    (access_purpose in
      ('CALIBRATION', 'ADAPTIVE_SEARCH', 'FORWARD_CONFIRMATION')),
  constraint chk_intraday_access_clock check
    (knowledge_clock_mode in
      ('ARRIVAL_TIME_CAUSAL', 'EVENT_TIME_HISTORICAL_ONLY')),
  constraint chk_intraday_access_hash check
    (access_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_access_instruments check
    (instrument_count >= 1
     and cardinality(instrument_ids) = instrument_count),
  constraint chk_intraday_access_watermark check
    (jsonb_typeof(source_watermark) = 'object'
     and source_watermark <> '{}'::jsonb),
  constraint chk_intraday_access_actor check (btrim(accessed_by) <> '')
);

create table quant.intraday_session_exposures (
  session_exposure_id           uuid primary key default gen_random_uuid(),
  session_access_id             uuid not null,
  experiment_rung_id            uuid not null,
  candidate_lineage_id          uuid not null,
  root_lineage_id               uuid not null,
  dataset_id                    uuid not null
    references quant.dataset_manifests(dataset_id) on delete restrict,
  session_date                  date not null,
  exposure_purpose              text not null,
  knowledge_clock_mode          text not null,
  session_content_fingerprint   text not null,
  instrument_set_fingerprint    text not null,
  exposure_evidence_fingerprint text not null,
  instrument_ids                uuid[] not null,
  instrument_count              integer not null,
  quote_row_count               bigint not null,
  trade_row_count               bigint not null,
  knowledge_cutoff              timestamptz not null,
  source_watermark              jsonb not null,
  exposed_by                    text not null,
  exposed_at                    timestamptz not null default now(),

  -- A date is exposed to the whole ancestry, not merely one AST node.  This
  -- unique key is the lock that prevents a descendant or a new manifest from
  -- laundering the same date into forward evidence.
  constraint uq_intraday_root_session_exposure unique
    (root_lineage_id, session_date),
  constraint uq_intraday_access_evidence unique (session_access_id),
  constraint fk_intraday_exposure_access foreign key
    (session_access_id, experiment_rung_id, candidate_lineage_id,
     root_lineage_id)
    references quant.intraday_session_accesses
      (session_access_id, experiment_rung_id, candidate_lineage_id,
       root_lineage_id)
    on delete restrict,
  constraint fk_intraday_exposure_rung_lineage_root foreign key
    (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    references quant.intraday_experiment_rungs
      (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    on delete restrict,
  constraint chk_intraday_exposure_purpose check
    (exposure_purpose in
      ('CALIBRATION', 'ADAPTIVE_SEARCH', 'FORWARD_CONFIRMATION')),
  constraint chk_intraday_exposure_clock check
    (knowledge_clock_mode in
      ('ARRIVAL_TIME_CAUSAL', 'EVENT_TIME_HISTORICAL_ONLY')),
  constraint chk_intraday_exposure_hashes check
    (session_content_fingerprint ~ '^[0-9a-f]{64}$'
     and instrument_set_fingerprint ~ '^[0-9a-f]{64}$'
     and exposure_evidence_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_exposure_counts check
    (instrument_count >= 1
     and cardinality(instrument_ids) = instrument_count
     and quote_row_count >= 0 and trade_row_count >= 0),
  constraint chk_intraday_exposure_watermark check
    (jsonb_typeof(source_watermark) = 'object'
     and source_watermark <> '{}'::jsonb),
  constraint chk_intraday_exposure_actor check (btrim(exposed_by) <> '')
);

create table quant.intraday_forward_confirmations (
  forward_confirmation_id       uuid primary key default gen_random_uuid(),
  experiment_rung_id            uuid not null,
  candidate_lineage_id          uuid not null,
  root_lineage_id               uuid not null,
  decision                      text not null,
  gate_version                  text not null,
  prior_search_max_session_date date not null,
  forward_start_session_date    date not null,
  forward_end_session_date      date not null,
  forward_session_count         integer not null,
  confirmation_evidence_fingerprint text not null,
  gate_statistics               jsonb not null,
  gate_failures                 jsonb not null,
  decision_reason               text not null,
  confirmed_by                  text not null,
  confirmed_at                  timestamptz not null default now(),

  constraint uq_intraday_forward_rung unique (experiment_rung_id),
  constraint fk_intraday_forward_rung_lineage_root foreign key
    (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    references quant.intraday_experiment_rungs
      (experiment_rung_id, candidate_lineage_id, root_lineage_id)
    on delete restrict,
  constraint chk_intraday_forward_decision check
    (decision in ('PASS', 'FAIL', 'INCONCLUSIVE')),
  constraint chk_intraday_forward_dates check
    (forward_start_session_date > prior_search_max_session_date
     and forward_end_session_date >= forward_start_session_date),
  constraint chk_intraday_forward_count check (forward_session_count >= 20),
  constraint chk_intraday_forward_evidence_hash check
    (confirmation_evidence_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_forward_statistics check
    (jsonb_typeof(gate_statistics) = 'object'
     and gate_statistics <> '{}'::jsonb),
  constraint chk_intraday_forward_failures check
    (jsonb_typeof(gate_failures) = 'array'
     and (decision <> 'PASS' or gate_failures = '[]'::jsonb)),
  constraint chk_intraday_forward_text check
    (btrim(gate_version) <> ''
     and btrim(decision_reason) <> ''
     and btrim(confirmed_by) <> '')
);

-- Large structured reports do not belong in experiment_metrics.dimensions:
-- that column participates in a B-tree unique index with a ~2704 byte entry
-- limit.  Keep one append-only JSON document here and index only its digest.
create table quant.intraday_report_manifests (
  experiment_id              uuid primary key
    references quant.experiments(experiment_id) on delete restrict,
  report_fingerprint         text not null,
  manifest_version           text not null,
  report                     jsonb not null,
  created_by                 text not null,
  created_at                 timestamptz not null default now(),
  constraint chk_intraday_report_hash check
    (report_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_report_object check
    (jsonb_typeof(report) = 'object' and report <> '{}'::jsonb),
  constraint chk_intraday_report_text check
    (btrim(manifest_version) <> '' and btrim(created_by) <> '')
);

create index idx_intraday_lineage_root
  on quant.intraday_candidate_lineages(root_lineage_id, created_at);
create index idx_intraday_rung_root
  on quant.intraday_experiment_rungs(root_lineage_id, allocated_at);
create unique index uq_intraday_forward_test_index
  on quant.intraday_experiment_rungs(forward_test_index)
  where forward_test_index is not null;
create index idx_intraday_access_root_date
  on quant.intraday_session_accesses(root_lineage_id, session_date desc);
create index idx_intraday_exposure_root_date
  on quant.intraday_session_exposures(root_lineage_id, session_date desc);
create index idx_intraday_forward_decision
  on quant.intraday_forward_confirmations(decision, confirmed_at desc);

-- Validate the ordered rung declaration and seal its forward cutoff before a
-- worker can read any of the planned sessions.
create or replace function quant.validate_intraday_rung_allocation()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
declare
  canonical_dates date[];
  canonical_instruments uuid[];
  date_count integer;
  distinct_date_count integer;
  instrument_count integer;
  distinct_instrument_count integer;
  predecessor record;
  experiment record;
  expected_predecessor text;
  latest_exposed_session date;
begin
  -- Serialize every allocation/exposure/confirmation transition per lineage
  -- root.  Unique constraints alone do not close the READ COMMITTED race where
  -- two experiments allocate an unfinished FORWARD cohort concurrently.
  perform pg_advisory_xact_lock(
    hashtextextended(new.root_lineage_id::text, 0)
  );

  if new.rung = 'FORWARD' then
    if new.forward_test_index is not null then
      raise exception 'forward test index is database assigned';
    end if;
    new.forward_test_index := nextval(
      'quant.intraday_forward_test_index_seq'::regclass);
  elsif new.forward_test_index is not null then
    raise exception 'only FORWARD may carry a forward test index';
  end if;

  select array_agg(d order by d), count(*), count(distinct d)
    into canonical_dates, date_count, distinct_date_count
    from unnest(new.planned_session_dates) as d;

  if canonical_dates is distinct from new.planned_session_dates
     or date_count <> distinct_date_count then
    raise exception 'planned intraday sessions must be sorted and unique';
  end if;

  select array_agg(i order by i), count(*), count(distinct i)
    into canonical_instruments, instrument_count, distinct_instrument_count
    from unnest(new.planned_instrument_ids) as i;
  if canonical_instruments is distinct from new.planned_instrument_ids
     or instrument_count <> distinct_instrument_count
     or instrument_count <> new.planned_instrument_count then
    raise exception 'planned intraday instruments must be sorted, unique, and exact';
  end if;

  select e.hypothesis_id, e.dataset_id
    into experiment
    from quant.experiments e
   where e.experiment_id = new.experiment_id;
  if not found then
    raise exception 'intraday rung has no matching experiment';
  end if;
  if experiment.dataset_id <> new.dataset_id
     or not exists (
       select 1
         from quant.intraday_candidate_lineages c
        where c.candidate_lineage_id = new.candidate_lineage_id
          and c.root_lineage_id = new.root_lineage_id
          and c.hypothesis_id = experiment.hypothesis_id
     ) then
    raise exception 'intraday rung candidate or dataset differs from its experiment';
  end if;

  expected_predecessor := case new.rung
    when 'DISCOVERY_6' then 'CALIBRATION'
    when 'VALIDATION_20' then 'DISCOVERY_6'
    when 'FULL_60' then 'VALIDATION_20'
    when 'FORWARD' then 'FULL_60'
    else null
  end;

  if expected_predecessor is null then
    if new.predecessor_rung_id is not null then
      raise exception 'CALIBRATION cannot have a predecessor rung';
    end if;
  else
    if new.predecessor_rung_id is null then
      raise exception '% requires predecessor rung %',
        new.rung, expected_predecessor;
    end if;
    select r.rung, r.candidate_lineage_id, r.root_lineage_id,
           r.experiment_id, r.dataset_id, r.planned_session_dates,
           r.planned_instrument_ids
      into predecessor
      from quant.intraday_experiment_rungs r
     where r.experiment_rung_id = new.predecessor_rung_id;
    if not found
       or predecessor.rung <> expected_predecessor
       or predecessor.candidate_lineage_id <> new.candidate_lineage_id
       or predecessor.root_lineage_id <> new.root_lineage_id
       or predecessor.experiment_id <> new.experiment_id
       or predecessor.dataset_id <> new.dataset_id then
      raise exception 'intraday predecessor must be the prior rung of the same experiment and candidate lineage';
    end if;
    if predecessor.planned_instrument_ids is distinct from
         new.planned_instrument_ids then
      raise exception 'intraday rung cannot change the frozen instrument universe';
    end if;
    if new.rung in ('VALIDATION_20', 'FULL_60')
       and exists (
         select 1
           from unnest(predecessor.planned_session_dates) as prior(prior_date)
          where not (prior_date = any(new.planned_session_dates))
       ) then
      raise exception 'validation and full rungs must contain every prior search session';
    end if;
  end if;

  if new.rung = 'FORWARD' then
    if exists (
      select 1
        from unnest(predecessor.planned_session_dates) as prior(prior_date)
       where not exists (
         select 1
           from quant.intraday_session_exposures e
          where e.root_lineage_id = new.root_lineage_id
            and e.session_date = prior_date
       )
    ) then
      raise exception 'forward allocation requires complete FULL_60 exposure evidence';
    end if;
    select max(a.session_date)
      into latest_exposed_session
      from quant.intraday_session_accesses a
     where a.root_lineage_id = new.root_lineage_id;
    if latest_exposed_session is null
       or new.lockbox_cutoff_session_date <> latest_exposed_session then
      raise exception 'forward cutoff % must equal latest exposed session %',
        new.lockbox_cutoff_session_date, latest_exposed_session;
    end if;
    if canonical_dates[1] <= new.lockbox_cutoff_session_date then
      raise exception 'forward sessions must be strictly newer than the lineage lockbox cutoff';
    end if;
    if exists (
      select 1
        from quant.intraday_experiment_rungs fr
       where fr.root_lineage_id = new.root_lineage_id
         and fr.rung = 'FORWARD'
         and not exists (
           select 1
             from quant.intraday_forward_confirmations fc
            where fc.experiment_rung_id = fr.experiment_rung_id
         )
    ) then
      raise exception 'candidate lineage already has an unfinished forward rung';
    end if;
  end if;

  return new;
end;
$$;

create or replace function quant.validate_intraday_session_access()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
declare
  declared_rung record;
  canonical_instruments uuid[];
begin
  perform pg_advisory_xact_lock(
    hashtextextended(new.root_lineage_id::text, 0)
  );

  select r.rung, r.evidence_purpose, r.dataset_id,
         r.planned_session_dates, r.planned_instrument_ids,
         r.planned_instrument_count, r.dataset_cutoff,
         r.lockbox_cutoff_session_date
    into declared_rung
    from quant.intraday_experiment_rungs r
   where r.experiment_rung_id = new.experiment_rung_id
     and r.candidate_lineage_id = new.candidate_lineage_id
     and r.root_lineage_id = new.root_lineage_id;

  if not found then
    raise exception 'session access has no matching declared intraday rung';
  end if;
  if new.dataset_id <> declared_rung.dataset_id
     or new.knowledge_cutoff <> declared_rung.dataset_cutoff then
    raise exception 'session access dataset or knowledge cutoff differs from its frozen rung';
  end if;
  if not (new.session_date = any(declared_rung.planned_session_dates)) then
    raise exception 'session % was not preregistered in rung %',
      new.session_date, new.experiment_rung_id;
  end if;
  select array_agg(i order by i) into canonical_instruments
    from unnest(new.instrument_ids) as i;
  if canonical_instruments is distinct from new.instrument_ids
     or new.instrument_ids is distinct from declared_rung.planned_instrument_ids
     or new.instrument_count <> declared_rung.planned_instrument_count then
    raise exception 'session access differs from the exact frozen instrument universe';
  end if;

  if declared_rung.rung = 'FORWARD' then
    if new.access_purpose <> 'FORWARD_CONFIRMATION'
       or new.knowledge_clock_mode <> 'ARRIVAL_TIME_CAUSAL'
       or new.session_date <= declared_rung.lockbox_cutoff_session_date then
      raise exception 'forward access requires new arrival-time-causal evidence';
    end if;
  else
    if declared_rung.rung = 'CALIBRATION'
       and new.access_purpose <> 'CALIBRATION' then
      raise exception 'CALIBRATION rung requires CALIBRATION access purpose';
    elsif declared_rung.rung <> 'CALIBRATION'
       and new.access_purpose <> 'ADAPTIVE_SEARCH' then
      raise exception 'search rung requires ADAPTIVE_SEARCH access purpose';
    end if;
    if exists (
      select 1
        from quant.intraday_experiment_rungs fr
       where fr.root_lineage_id = new.root_lineage_id
         and fr.rung = 'FORWARD'
         and not exists (
           select 1 from quant.intraday_forward_confirmations fc
            where fc.experiment_rung_id = fr.experiment_rung_id
         )
    ) then
      raise exception 'candidate lineage is sealed by an unfinished forward rung';
    end if;
  end if;

  return new;
end;
$$;

create or replace function quant.validate_intraday_session_exposure()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
declare
  declared_rung record;
begin
  perform pg_advisory_xact_lock(
    hashtextextended(new.root_lineage_id::text, 0)
  );

  select r.rung, r.evidence_purpose, r.dataset_id,
         r.planned_session_dates, r.planned_instrument_ids,
         r.planned_instrument_count, r.dataset_cutoff,
         r.lockbox_cutoff_session_date
    into declared_rung
    from quant.intraday_experiment_rungs r
   where r.experiment_rung_id = new.experiment_rung_id
     and r.candidate_lineage_id = new.candidate_lineage_id
     and r.root_lineage_id = new.root_lineage_id;

  if not found then
    raise exception 'session exposure has no matching declared intraday rung';
  end if;
  if new.dataset_id <> declared_rung.dataset_id
     or new.knowledge_cutoff <> declared_rung.dataset_cutoff then
    raise exception 'session exposure dataset or knowledge cutoff differs from its frozen rung';
  end if;
  if not (new.session_date = any(declared_rung.planned_session_dates)) then
    raise exception 'session % was not preregistered in rung %',
      new.session_date, new.experiment_rung_id;
  end if;
  if new.instrument_ids is distinct from declared_rung.planned_instrument_ids
     or new.instrument_count <> declared_rung.planned_instrument_count then
    raise exception 'session exposure differs from the exact frozen instrument universe';
  end if;
  if not exists (
    select 1
      from quant.intraday_session_accesses a
     where a.session_access_id = new.session_access_id
       and a.experiment_rung_id = new.experiment_rung_id
       and a.candidate_lineage_id = new.candidate_lineage_id
       and a.root_lineage_id = new.root_lineage_id
       and a.dataset_id = new.dataset_id
       and a.session_date = new.session_date
       and a.instrument_ids = new.instrument_ids
       and a.knowledge_cutoff = new.knowledge_cutoff
       and a.access_purpose = new.exposure_purpose
       and a.knowledge_clock_mode = new.knowledge_clock_mode
  ) then
    raise exception 'session evidence requires a matching durable pre-read access marker';
  end if;

  if declared_rung.rung = 'FORWARD' then
    if new.exposure_purpose <> 'FORWARD_CONFIRMATION'
       or new.knowledge_clock_mode <> 'ARRIVAL_TIME_CAUSAL'
       or coalesce(new.source_watermark->>'content_digest_version', '') <>
          'ACTUAL_RAW_REPLAY_V1'
       or new.session_date <= declared_rung.lockbox_cutoff_session_date then
      raise exception 'forward exposure requires new arrival-time-causal evidence';
    end if;
  else
    if declared_rung.rung = 'CALIBRATION'
       and new.exposure_purpose <> 'CALIBRATION' then
      raise exception 'CALIBRATION rung requires CALIBRATION exposure purpose';
    elsif declared_rung.rung <> 'CALIBRATION'
       and new.exposure_purpose <> 'ADAPTIVE_SEARCH' then
      raise exception 'search rung requires ADAPTIVE_SEARCH exposure purpose';
    end if;
    -- An allocated but unfinished forward cohort seals the lineage.  This
    -- prevents a late adaptive read from contaminating that cohort between
    -- allocation and confirmation.
    if exists (
      select 1
        from quant.intraday_experiment_rungs fr
       where fr.root_lineage_id = new.root_lineage_id
         and fr.rung = 'FORWARD'
         and not exists (
           select 1 from quant.intraday_forward_confirmations fc
            where fc.experiment_rung_id = fr.experiment_rung_id
         )
    ) then
      raise exception 'candidate lineage is sealed by an unfinished forward rung';
    end if;
  end if;

  return new;
end;
$$;

create or replace function quant.validate_intraday_forward_confirmation()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
declare
  declared_rung record;
  access_dates date[];
  access_count integer;
  actual_dates date[];
  actual_count integer;
begin
  perform pg_advisory_xact_lock(
    hashtextextended(new.root_lineage_id::text, 0)
  );

  select r.rung, r.planned_session_dates, r.planned_session_count,
         r.lockbox_cutoff_session_date
    into declared_rung
    from quant.intraday_experiment_rungs r
   where r.experiment_rung_id = new.experiment_rung_id
     and r.candidate_lineage_id = new.candidate_lineage_id
     and r.root_lineage_id = new.root_lineage_id;

  if not found or declared_rung.rung <> 'FORWARD' then
    raise exception 'confirmation requires a matching FORWARD rung';
  end if;

  select array_agg(a.session_date order by a.session_date), count(*)
    into access_dates, access_count
    from quant.intraday_session_accesses a
   where a.experiment_rung_id = new.experiment_rung_id
     and a.access_purpose = 'FORWARD_CONFIRMATION';

  select array_agg(e.session_date order by e.session_date), count(*)
    into actual_dates, actual_count
    from quant.intraday_session_exposures e
   where e.experiment_rung_id = new.experiment_rung_id
     and e.exposure_purpose = 'FORWARD_CONFIRMATION';

  if access_count <> declared_rung.planned_session_count
     or access_dates is distinct from declared_rung.planned_session_dates
     or actual_count <> declared_rung.planned_session_count
     or actual_dates is distinct from declared_rung.planned_session_dates then
    raise exception 'all preregistered forward sessions require access and evidence before confirmation';
  end if;
  if new.prior_search_max_session_date <>
       declared_rung.lockbox_cutoff_session_date
     or new.forward_start_session_date <> actual_dates[1]
     or new.forward_end_session_date <>
       actual_dates[array_length(actual_dates, 1)]
     or new.forward_session_count <> actual_count then
    raise exception 'forward confirmation summary differs from immutable exposure evidence';
  end if;

  return new;
end;
$$;

create trigger intraday_rung_allocation_guard
before insert on quant.intraday_experiment_rungs
for each row execute function quant.validate_intraday_rung_allocation();

create trigger intraday_session_access_guard
before insert on quant.intraday_session_accesses
for each row execute function quant.validate_intraday_session_access();

create trigger intraday_session_exposure_guard
before insert on quant.intraday_session_exposures
for each row execute function quant.validate_intraday_session_exposure();

create trigger intraday_forward_confirmation_guard
before insert on quant.intraday_forward_confirmations
for each row execute function quant.validate_intraday_forward_confirmation();

create trigger intraday_candidate_lineages_append_only
before update or delete on quant.intraday_candidate_lineages
for each row execute function governance.reject_append_only_change();
create trigger intraday_experiment_rungs_append_only
before update or delete on quant.intraday_experiment_rungs
for each row execute function governance.reject_append_only_change();
create trigger intraday_session_accesses_append_only
before update or delete on quant.intraday_session_accesses
for each row execute function governance.reject_append_only_change();
create trigger intraday_session_exposures_append_only
before update or delete on quant.intraday_session_exposures
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_confirmations_append_only
before update or delete on quant.intraday_forward_confirmations
for each row execute function governance.reject_append_only_change();
create trigger intraday_report_manifests_append_only
before update or delete on quant.intraday_report_manifests
for each row execute function governance.reject_append_only_change();

alter table quant.intraday_candidate_lineages enable row level security;
alter table quant.intraday_experiment_rungs enable row level security;
alter table quant.intraday_session_accesses enable row level security;
alter table quant.intraday_session_exposures enable row level security;
alter table quant.intraday_forward_confirmations enable row level security;
alter table quant.intraday_report_manifests enable row level security;

-- The intraday lane is stock-only.  The source intersection currently has
-- 2,524 STOCK instruments and one ID with no reference metadata; absence must
-- never be interpreted as STOCK.  Column privileges expose only what the
-- quant runtime needs to make that fail-closed membership decision.  The RLS
-- predicate is a second boundary: even an accidentally broad SELECT cannot
-- read ETF/ETN/derivative rows through svc_quant.
grant usage on schema reference to svc_quant;
grant usage on schema reference, quant to service_role;
grant select (
  instrument_id, instrument_type, asset_class, market, venue,
  listed_from, listed_to, status
) on reference.instruments to svc_quant;
create policy reference_instruments_svc_quant_stock_only_select
  on reference.instruments for select to svc_quant
  using (upper(instrument_type) = 'STOCK');

-- Forward dates come from the governed KRX calendar rather than the presence
-- of market rows.  This prevents a full collection outage from disappearing
-- from the fixed cohort.  Expose only the columns needed to freeze a completed
-- regular-session schedule and restrict svc_quant to KRX rows.
grant select (
  calendar_version_id, market, version, published_at, effective_from,
  effective_to, content_hash, created_at
) on reference.market_calendar_versions to svc_quant, service_role;
grant select (
  calendar_version_id, market, trade_date, session_type, opens_at, closes_at,
  is_trading_day
) on reference.market_sessions to svc_quant, service_role;
create policy market_calendar_versions_svc_quant_krx_select
  on reference.market_calendar_versions for select to svc_quant
  using (market = 'KRX');
create policy market_sessions_svc_quant_krx_select
  on reference.market_sessions for select to svc_quant
  using (market = 'KRX');

-- The quant runtime may append and inspect lockbox evidence.  It cannot mutate
-- or remove it.  No authenticated/anonymous browser policy is created.
create policy intraday_candidate_lineages_svc_quant_select
  on quant.intraday_candidate_lineages for select to svc_quant using (true);
create policy intraday_candidate_lineages_svc_quant_insert
  on quant.intraday_candidate_lineages for insert to svc_quant with check (true);
create policy intraday_experiment_rungs_svc_quant_select
  on quant.intraday_experiment_rungs for select to svc_quant using (true);
create policy intraday_experiment_rungs_svc_quant_insert
  on quant.intraday_experiment_rungs for insert to svc_quant with check (true);
create policy intraday_session_accesses_svc_quant_select
  on quant.intraday_session_accesses for select to svc_quant using (true);
create policy intraday_session_accesses_svc_quant_insert
  on quant.intraday_session_accesses for insert to svc_quant with check (true);
create policy intraday_session_exposures_svc_quant_select
  on quant.intraday_session_exposures for select to svc_quant using (true);
create policy intraday_session_exposures_svc_quant_insert
  on quant.intraday_session_exposures for insert to svc_quant with check (true);
create policy intraday_forward_confirmations_svc_quant_select
  on quant.intraday_forward_confirmations for select to svc_quant using (true);
create policy intraday_forward_confirmations_svc_quant_insert
  on quant.intraday_forward_confirmations for insert to svc_quant with check (true);
create policy intraday_report_manifests_svc_quant_select
  on quant.intraday_report_manifests for select to svc_quant using (true);
create policy intraday_report_manifests_svc_quant_insert
  on quant.intraday_report_manifests for insert to svc_quant with check (true);

grant select, insert on
  quant.intraday_candidate_lineages,
  quant.intraday_experiment_rungs,
  quant.intraday_session_accesses,
  quant.intraday_session_exposures,
  quant.intraday_forward_confirmations,
  quant.intraday_report_manifests
to svc_quant, service_role;
grant usage, select on sequence quant.intraday_forward_test_index_seq
to svc_quant, service_role;
revoke update, delete, truncate on
  quant.intraday_candidate_lineages,
  quant.intraday_experiment_rungs,
  quant.intraday_session_accesses,
  quant.intraday_session_exposures,
  quant.intraday_forward_confirmations,
  quant.intraday_report_manifests
from svc_quant, service_role;

comment on table quant.intraday_candidate_lineages is
  'Immutable intraday candidate ancestry and exact AST/feature/label/model/evaluator identity. Descendants inherit every session exposed to their root.';
comment on policy reference_instruments_svc_quant_stock_only_select
  on reference.instruments is
  'Fail-closed stock-only universe boundary for svc_quant. Missing metadata and non-STOCK product types are not visible as eligible instruments.';
comment on policy market_sessions_svc_quant_krx_select
  on reference.market_sessions is
  'Read-only KRX schedule boundary used to preregister forward dates before any quote or trade row is inspected.';
comment on column quant.intraday_candidate_lineages.root_lineage_id is
  'Lockbox scope. A child keeps its parent root through a composite foreign key; changing AST does not reset session exposure.';
comment on table quant.intraday_experiment_rungs is
  'Immutable preregistered resource-allocation rungs. CALIBRATION seals teacher-fit sessions before DISCOVERY; CALIBRATION/DISCOVERY/VALIDATION/FULL are adaptive search, never independent confirmation.';
comment on column quant.intraday_experiment_rungs.forward_test_index is
  'Database-assigned globally unique immutable alpha-spending index for a FORWARD allocation.';
comment on column quant.intraday_experiment_rungs.rung_plan_fingerprint is
  'SHA-256 over the complete frozen allocation. Idempotent retries must match the same sessions, instruments, cutoff, watermark, predecessor, policy, and actor.';
comment on column quant.intraday_experiment_rungs.lockbox_cutoff_session_date is
  'For FORWARD only: every planned session must be strictly newer. Existing historical sessions are not silently declared unused.';
comment on table quant.intraday_session_exposures is
  'Append-only content evidence produced after a durable session-access marker consumed the date.';
comment on table quant.intraday_session_accesses is
  'Durable pre-read marker committed before raw market access. One root/date marker consumes the date even if the worker crashes before evidence is appended.';
comment on column quant.intraday_session_exposures.exposure_evidence_fingerprint is
  'SHA-256 over the exact first-read evidence, including rung, content, clock, row counts, cutoff, watermark, and actor.';
comment on column quant.intraday_session_exposures.knowledge_clock_mode is
  'EVENT_TIME_HISTORICAL_ONLY may be used for discovery but FORWARD requires ARRIVAL_TIME_CAUSAL.';
comment on table quant.intraday_forward_confirmations is
  'Immutable gate result over a complete preregistered FORWARD cohort. Database guards require all planned arrival-time-causal exposure rows first.';
comment on table quant.intraday_report_manifests is
  'Append-only full intraday report JSON. experiment_metrics stores only its compact SHA-256 reference so the metric unique B-tree never indexes a large document.';

commit;
