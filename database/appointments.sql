-- Один раз выполни этот файл в SQL Editor Supabase, чтобы создать закрытую таблицу.
create table if not exists public.appointments (
    id bigint generated always as identity primary key,
    name text not null check (char_length(name) between 2 and 100),
    phone text not null check (char_length(phone) between 7 and 25),
    requested_at timestamp without time zone not null,
    time_zone text not null check (char_length(time_zone) between 1 and 100),
    time_zone_offset integer not null check (time_zone_offset between -840 and 840),
    note text check (note is null or char_length(note) <= 1000),
    status text not null default 'pending'
        check (status in ('pending', 'confirmed', 'declined')),
    consented_at timestamp with time zone not null default now(),
    created_at timestamp with time zone not null default now()
);

-- Только серверный service-role ключ может читать и изменять личные записи.
alter table public.appointments enable row level security;
revoke all on table public.appointments from anon, authenticated;
revoke all on sequence public.appointments_id_seq from anon, authenticated;
grant select, insert on table public.appointments to service_role;
grant usage, select on sequence public.appointments_id_seq to service_role;
