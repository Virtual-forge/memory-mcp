create extension if not exists pgcrypto;

create table if not exists entities (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    name       text not null,
    aliases    text[] not null default '{}',
    created_at timestamptz not null default now(),
    unique (scope, name)
);
create index if not exists entities_scope_lower_name_idx
    on entities (scope, lower(name));

create table if not exists project_registry (
    id              uuid primary key default gen_random_uuid(),
    scope           text not null,
    name            text not null,
    normalized_name text not null,
    aliases         text[] not null default '{}',
    root_path       text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (scope, normalized_name)
);
create index if not exists project_registry_scope_idx
    on project_registry (scope);

create table if not exists turns (
    id            uuid primary key default gen_random_uuid(),
    session_id    uuid not null,
    scope         text not null,
    source        text not null check (source in ('user', 'assistant', 'tool')),
    content       text not null,
    tool_name     text,
    tool_call_id  text,
    source_event_id text,
    source_path   text,
    source_heading text,
    source_start_line integer,
    source_end_line integer,
    source_hash   text,
    created_at    timestamptz not null default now()
);
alter table turns add column if not exists source_event_id text;
alter table turns add column if not exists source_path text;
alter table turns add column if not exists source_heading text;
alter table turns add column if not exists source_start_line integer;
alter table turns add column if not exists source_end_line integer;
alter table turns add column if not exists source_hash text;
create index if not exists turns_session_created_idx on turns (session_id, created_at);
create index if not exists turns_tool_call_idx
    on turns (tool_call_id) where tool_call_id is not null;
create unique index if not exists turns_source_event_idx
    on turns (session_id, scope, source_event_id);

create table if not exists extraction_coverage (
    turn_id      uuid primary key references turns(id) on delete cascade,
    processed_at timestamptz not null default now()
);

create table if not exists extraction_attempts (
    session_id                    uuid not null,
    scope                         text not null,
    attempted_through_created_at timestamptz not null,
    attempted_through_turn_id    uuid not null references turns(id) on delete cascade,
    attempted_at                 timestamptz not null default now(),
    primary key (session_id, scope)
);

create table if not exists atoms (
    id              uuid primary key default gen_random_uuid(),
    scope           text not null,
    statement       text not null,
    category        text not null check (category in ('fact', 'preference', 'decision', 'event')),
    origin          text not null check (origin in ('asserted', 'inferred')),
    scene_name      text not null,
    entity_id       uuid references entities(id),
    predicate       text,
    confidence      real check (confidence is null or confidence between 0 and 1),
    project_id      uuid references project_registry(id) on delete cascade,
    supersedes      uuid references atoms(id),
    source_deleted  boolean not null default false,
    created_at      timestamptz not null default now()
);
create index if not exists atoms_scope_category_idx on atoms (scope, category);
create index if not exists atoms_scope_created_idx on atoms (scope, created_at);
create index if not exists atoms_active_idx on atoms (scope) where source_deleted = false;
alter table atoms add column if not exists project_id uuid
    references project_registry(id) on delete cascade;
create index if not exists atoms_project_idx on atoms (project_id) where project_id is not null;

create table if not exists atom_sources (
    atom_id  uuid references atoms(id) on delete cascade,
    turn_id  uuid references turns(id) on delete cascade,
    primary key (atom_id, turn_id)
);
create index if not exists atom_sources_turn_idx on atom_sources (turn_id);

create table if not exists project_documents (
    id            uuid primary key default gen_random_uuid(),
    project_id    uuid not null references project_registry(id) on delete cascade,
    relative_path text not null,
    content_hash  text not null,
    byte_size     bigint not null,
    is_active     boolean not null default true,
    imported_at   timestamptz not null default now(),
    unique (project_id, relative_path, content_hash)
);
create unique index if not exists project_documents_active_path_idx
    on project_documents (project_id, relative_path)
    where is_active;

create table if not exists project_document_chunks (
    id            uuid primary key default gen_random_uuid(),
    document_id   uuid not null references project_documents(id) on delete cascade,
    turn_id       uuid not null unique references turns(id) on delete cascade,
    chunk_index   integer not null,
    heading       text not null,
    start_line    integer not null,
    end_line      integer not null,
    is_active     boolean not null default true,
    unique (document_id, chunk_index)
);
create index if not exists project_document_chunks_document_idx
    on project_document_chunks (document_id, chunk_index);
create index if not exists project_document_chunks_active_idx
    on project_document_chunks (is_active);

delete from extraction_coverage as ec
where not exists (
    select 1 from atom_sources as source where source.turn_id = ec.turn_id
);

create table if not exists synthesis_coverage (
    atom_id      uuid primary key references atoms(id) on delete cascade,
    processed_at timestamptz not null default now()
);

create table if not exists category_types (
    category text primary key,
    type     text not null check (type in ('agent', 'workspace', 'user'))
);

create table if not exists projects (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists project_atoms (
    project_id uuid references projects(id) on delete cascade,
    atom_id    uuid references atoms(id) on delete cascade,
    primary key (project_id, atom_id)
);

create table if not exists project_sections (
    id         uuid primary key default gen_random_uuid(),
    project_id uuid not null references project_registry(id) on delete cascade,
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists project_sections_project_title_idx
    on project_sections (project_id, lower(title));
create index if not exists project_sections_scope_idx
    on project_sections (scope, project_id);
create table if not exists project_section_atoms (
    section_id uuid references project_sections(id) on delete cascade,
    atom_id    uuid references atoms(id) on delete cascade,
    primary key (section_id, atom_id)
);

create table if not exists tools (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists tool_atoms (
    tool_id uuid references tools(id) on delete cascade,
    atom_id uuid references atoms(id) on delete cascade,
    primary key (tool_id, atom_id)
);

create table if not exists skills (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists skill_atoms (
    skill_id uuid references skills(id) on delete cascade,
    atom_id  uuid references atoms(id) on delete cascade,
    primary key (skill_id, atom_id)
);


create or replace function delete_orphaned_atom_after_source_delete()
returns trigger
language plpgsql
as $$
begin
    delete from atoms as a
    where a.id = old.atom_id
      and not exists (
          select 1 from atom_sources as remaining where remaining.atom_id = a.id
      );
    return old;
end;
$$;

drop trigger if exists atom_sources_delete_orphans on atom_sources;
create trigger atom_sources_delete_orphans
after delete on atom_sources
for each row execute function delete_orphaned_atom_after_source_delete();
create table if not exists profiles (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists profile_atoms (
    profile_id uuid references profiles(id) on delete cascade,
    atom_id    uuid references atoms(id) on delete cascade,
    primary key (profile_id, atom_id)
);

create table if not exists users (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists user_atoms (
    user_id uuid references users(id) on delete cascade,
    atom_id uuid references atoms(id) on delete cascade,
    primary key (user_id, atom_id)
);

create table if not exists docs (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists doc_atoms (
    doc_id  uuid references docs(id) on delete cascade,
    atom_id uuid references atoms(id) on delete cascade,
    primary key (doc_id, atom_id)
);

create table if not exists tasks (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    owner      text,
    deadline   timestamptz,
    status     text not null default 'todo'
        check (status in ('todo', 'doing', 'done', 'blocked', 'cancelled')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists task_atoms (
    task_id uuid references tasks(id) on delete cascade,
    atom_id uuid references atoms(id) on delete cascade,
    primary key (task_id, atom_id)
);

create table if not exists mcp (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists mcp_atoms (
    mcp_id  uuid references mcp(id) on delete cascade,
    atom_id uuid references atoms(id) on delete cascade,
    primary key (mcp_id, atom_id)
);

create table if not exists workflows (
    id         uuid primary key default gen_random_uuid(),
    scope      text not null,
    title      text not null,
    summary    text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table if not exists workflow_atoms (
    workflow_id uuid references workflows(id) on delete cascade,
    atom_id     uuid references atoms(id) on delete cascade,
    primary key (workflow_id, atom_id)
);

insert into category_types (category, type) values
    ('atoms', 'workspace'),
    ('projects', 'workspace'),
    ('project_sections', 'workspace'),
    ('docs', 'workspace'),
    ('tasks', 'workspace'),
    ('tools', 'agent'),
    ('skills', 'agent'),
    ('mcp', 'agent'),
    ('workflows', 'agent'),
    ('profiles', 'user'),
    ('users', 'user')
on conflict (category) do update set type = excluded.type;
