-- Запусти скрипт в SQL Editor того проекта Supabase, где хранится сайт.
begin;

-- Эта схема создаёт таблицу при первом запуске и дополняет прежнюю таблицу.
create table if not exists public.appointments (
    id bigint generated always as identity primary key,
    name text not null check (char_length(name) between 2 and 100),
    phone text not null check (char_length(phone) between 7 and 25),
    email text,
    requested_at timestamp without time zone not null,
    time_zone text not null default 'Europe/Moscow',
    time_zone_offset integer not null default 180 check (time_zone_offset between -840 and 840),
    starts_at timestamp with time zone,
    ends_at timestamp with time zone,
    note text check (note is null or char_length(note) <= 1000),
    status text not null default 'pending',
    client_email_status text not null default 'not_sent',
    owner_email_status text not null default 'not_sent',
    consented_at timestamp with time zone not null default now(),
    created_at timestamp with time zone not null default now()
);

alter table public.appointments
    add column if not exists email text,
    add column if not exists starts_at timestamp with time zone,
    add column if not exists ends_at timestamp with time zone,
    add column if not exists client_email_status text not null default 'not_sent',
    add column if not exists owner_email_status text not null default 'not_sent';

-- У прежних записей статус declined теперь называется cancelled.
alter table public.appointments drop constraint if exists appointments_status_check;
update public.appointments set status = 'cancelled' where status = 'declined';

-- Переводим старое локальное время в UTC с учётом сохранённого UTC-смещения.
update public.appointments
set starts_at = (requested_at - make_interval(mins => time_zone_offset)) at time zone 'UTC'
where starts_at is null;

update public.appointments
set ends_at = starts_at + interval '60 minutes'
where ends_at is null;

alter table public.appointments
    alter column starts_at set not null,
    alter column ends_at set not null;

-- Статус может быть только одним из явно поддерживаемых приложением.
alter table public.appointments
    drop constraint if exists appointments_email_status_check,
    drop constraint if exists appointments_time_range_check,
    add constraint appointments_status_check
        check (status in ('pending', 'confirmed', 'cancelled')),
    add constraint appointments_email_status_check
        check (
            client_email_status in ('not_sent', 'pending', 'sent', 'failed')
            and owner_email_status in ('not_sent', 'pending', 'sent', 'failed')
        ),
    add constraint appointments_time_range_check
        check (ends_at > starts_at);

-- При старых пересекающихся заявках остановимся с понятной ошибкой до создания ограничения.
do $$
begin
    if exists (
        select 1
        from public.appointments first_booking
        join public.appointments second_booking
          on first_booking.id < second_booking.id
         and first_booking.status in ('pending', 'confirmed')
         and second_booking.status in ('pending', 'confirmed')
         and tstzrange(first_booking.starts_at, first_booking.ends_at, '[)')
             && tstzrange(second_booking.starts_at, second_booking.ends_at, '[)')
    ) then
        raise exception
            'Есть пересекающиеся активные заявки. Отмени или перенеси дубликаты в appointments, затем запусти SQL ещё раз.';
    end if;
end
$$;

-- База, а не только браузер, блокирует две одновременные заявки на одно время.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.appointments'::regclass
          and conname = 'appointments_no_overlapping_bookings'
    ) then
        alter table public.appointments
            add constraint appointments_no_overlapping_bookings
            exclude using gist (
                tstzrange(starts_at, ends_at, '[)') with &&
            )
            where (status in ('pending', 'confirmed'));
    end if;
end
$$;

-- Записи доступны только серверу Flask с ключом service_role.
alter table public.appointments enable row level security;
revoke all on table public.appointments from public, anon, authenticated;
revoke all on sequence public.appointments_id_seq from public, anon, authenticated;
grant select, insert, update, delete on table public.appointments to service_role;
grant usage, select on sequence public.appointments_id_seq to service_role;

commit;
