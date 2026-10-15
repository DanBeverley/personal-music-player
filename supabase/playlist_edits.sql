-- Apply after phase1_schema.sql, before enabling WEB_PLAYLIST_WRITES.
-- One transaction per edit; revisions also observe direct/older-client writes.
begin;

alter table public.playlists add column if not exists revision bigint not null default 0;

create or replace function public.neatie_playlist_revision()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
  if new.id <> old.id or new.user_id <> old.user_id then
    raise exception using errcode = '42501', message = 'playlist_owner_immutable';
  end if;
  new.revision := old.revision + 1;
  return new;
end;
$$;
drop trigger if exists neatie_playlist_revision on public.playlists;
create trigger neatie_playlist_revision before update on public.playlists
for each row execute function public.neatie_playlist_revision();

create or replace function public.neatie_playlist_track_revision()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare
  v_playlist text;
  v_user uuid;
begin
  if tg_op = 'DELETE' then
    v_playlist := old.playlist_id; v_user := old.user_id;
  else
    v_playlist := new.playlist_id; v_user := new.user_id;
    if tg_op = 'UPDATE' and
       (new.playlist_id <> old.playlist_id or new.user_id <> old.user_id or new.id <> old.id) then
      raise exception using errcode = '42501', message = 'playlist_entry_owner_immutable';
    end if;
  end if;
  -- This update locks the parent until commit, including legacy direct writes.
  update public.playlists set revision = revision + 1
    where id = v_playlist and user_id = v_user and user_id = auth.uid();
  if not found and tg_op <> 'DELETE' then
    raise exception using errcode = '42501', message = 'playlist_not_owned';
  end if;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end;
$$;
drop trigger if exists neatie_playlist_track_revision on public.playlist_tracks;
create trigger neatie_playlist_track_revision before insert or update or delete
on public.playlist_tracks for each row execute function public.neatie_playlist_track_revision();

drop policy if exists "playlist_tracks_manage_own" on public.playlist_tracks;
create policy "playlist_tracks_manage_own" on public.playlist_tracks for all
using (auth.uid() = user_id and exists (
  select 1 from public.playlists p where p.id = playlist_id and p.user_id = auth.uid()))
with check (auth.uid() = user_id and exists (
  select 1 from public.playlists p where p.id = playlist_id and p.user_id = auth.uid()));

create or replace function public.neatie_playlist_snapshot(p_playlist_id text)
returns jsonb language sql stable security invoker set search_path = '' as $$
  select jsonb_build_object('id', p.id, 'name', p.name, 'revision', p.revision,
      'tracks', coalesce((select jsonb_agg(jsonb_build_object(
        'id', t.id, 'position', t.position, 'track_id', t.track_id, 'track_data', t.track_data)
        order by t.position, t.id) from public.playlist_tracks t
        where t.playlist_id = p.id and t.user_id = auth.uid()), '[]'::jsonb))
  from public.playlists p where p.id = p_playlist_id and p.user_id = auth.uid();
$$;

create or replace function public.neatie_playlist_library()
returns jsonb language sql stable security invoker set search_path = '' as $$
  select coalesce(jsonb_agg(public.neatie_playlist_snapshot(p.id) order by p.created_at, p.id), '[]'::jsonb)
  from public.playlists p where p.user_id = auth.uid();
$$;

create or replace function public.neatie_edit_playlist(
  p_playlist_id text, p_action text, p_expected_revision bigint default null,
  p_payload jsonb default '{}'::jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_user uuid := auth.uid();
  v_revision bigint;
  v_name text;
  v_entry bigint;
  v_to integer;
  v_ids bigint[];
  v_offset integer;
begin
  if v_user is null then
    raise exception using errcode = '42501', message = 'sign_in_required';
  end if;
  if coalesce(length(trim(p_playlist_id)), 0) = 0 or length(p_playlist_id) > 200
      or jsonb_typeof(p_payload) is distinct from 'object' then
    raise exception using errcode = '22023', message = 'invalid_playlist_edit';
  end if;
  if p_action = 'create' then
    v_name := trim(p_payload->>'name');
    if coalesce(length(v_name), 0) not between 1 and 120 then
      raise exception using errcode = '22023', message = 'invalid_playlist_name';
    end if;
    insert into public.playlists(id, user_id, name) values (p_playlist_id, v_user, v_name);
    return public.neatie_playlist_snapshot(p_playlist_id);
  end if;

  select revision into v_revision from public.playlists
    where id = p_playlist_id and user_id = v_user for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'playlist_missing';
  end if;
  if p_expected_revision is null or p_expected_revision <> v_revision then
    raise exception using errcode = '40001', message = 'playlist_conflict';
  end if;
  if p_action = 'delete' then
    delete from public.playlists where id = p_playlist_id and user_id = v_user;
    return jsonb_build_object('id', p_playlist_id, 'deleted', true);
  elsif p_action = 'rename' then
    v_name := trim(p_payload->>'name');
    if coalesce(length(v_name), 0) not between 1 and 120 then
      raise exception using errcode = '22023', message = 'invalid_playlist_name';
    end if;
    update public.playlists set name = v_name where id = p_playlist_id and user_id = v_user;
  elsif p_action = 'append' then
    if coalesce(length(trim(p_payload->>'track_id')), 0) not between 1 and 512
       or jsonb_typeof(p_payload->'track_data') is distinct from 'object'
       or octet_length(p_payload::text) > 131072 then
      raise exception using errcode = '22023', message = 'invalid_playlist_track';
    end if;
    insert into public.playlist_tracks(user_id, playlist_id, position, track_id, track_data)
      select v_user, p_playlist_id, coalesce(max(position), -1) + 1,
        p_payload->>'track_id', p_payload->'track_data'
      from public.playlist_tracks where playlist_id = p_playlist_id and user_id = v_user;
  elsif p_action in ('remove', 'move') then
    v_entry := (p_payload->>'entry_id')::bigint;
    select array_agg(id order by position, id) into v_ids from public.playlist_tracks
      where playlist_id = p_playlist_id and user_id = v_user;
    if v_entry is null or array_position(v_ids, v_entry) is null then
      raise exception using errcode = '40001', message = 'playlist_conflict';
    end if;
    if p_action = 'remove' then
      delete from public.playlist_tracks where id = v_entry and user_id = v_user;
    else
      v_to := (p_payload->>'position')::integer;
      if v_to is null or v_to < 0 or v_to >= cardinality(v_ids) then
        raise exception using errcode = '22023', message = 'invalid_playlist_position';
      end if;
      v_ids := array_remove(v_ids, v_entry);
      v_ids := v_ids[1:v_to] || array[v_entry] || v_ids[v_to+1:cardinality(v_ids)];
      select coalesce(max(position), 0) + cardinality(v_ids) + 1 into v_offset
        from public.playlist_tracks where playlist_id = p_playlist_id and user_id = v_user;
      -- Shift out of the occupied range first; immediate unique indexes stay valid.
      update public.playlist_tracks set position = position + v_offset
        where playlist_id = p_playlist_id and user_id = v_user;
      update public.playlist_tracks t set position = ordered.n - 1
        from unnest(v_ids) with ordinality ordered(id, n)
        where t.id = ordered.id and t.playlist_id = p_playlist_id and t.user_id = v_user;
    end if;
  else
    raise exception using errcode = '22023', message = 'invalid_playlist_action';
  end if;
  return public.neatie_playlist_snapshot(p_playlist_id);
end;
$$;

revoke all on function public.neatie_playlist_revision() from public;
revoke all on function public.neatie_playlist_track_revision() from public;
revoke all on function public.neatie_playlist_snapshot(text) from public;
revoke all on function public.neatie_playlist_library() from public;
revoke all on function public.neatie_edit_playlist(text,text,bigint,jsonb) from public;
grant execute on function public.neatie_playlist_snapshot(text),
  public.neatie_playlist_library(), public.neatie_edit_playlist(text,text,bigint,jsonb) to authenticated;
notify pgrst, 'reload schema';
commit;
