-- Run this in the Supabase SQL editor once, before deploying.

create table if not exists sellers (
    id bigint generated always as identity primary key,
    phone text unique not null,
    name text,
    approved boolean default false,   -- gates whether their listings can go live
    created_at timestamptz default now()
);

create table if not exists listings (
    id bigint generated always as identity primary key,
    seller_id bigint references sellers (id),
    item text not null,
    quantity text,           -- kept as free text, e.g. "50kg", "20 pieces"
    price numeric not null,
    category text,
    area text,               -- general locality, e.g. "Sabo market" -- not a precise address
    image_url text,          -- public Supabase Storage URL, set once processed
    status text default 'pending_image' check (status in ('pending_image', 'pending_approval', 'active', 'sold', 'removed')),
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists buyer_requests (
    id bigint generated always as identity primary key,
    buyer_phone text not null,
    listing_id bigint references listings (id),
    message text,
    status text default 'new' check (status in ('new', 'contacted', 'closed')),
    created_at timestamptz default now()
);

-- Storage bucket for processed listing images. Create this via the
-- Supabase dashboard (Storage > New bucket > "listing-images", public)
-- rather than SQL, since bucket creation isn't a plain table operation.

-- Row Level Security: the buyer web app reads listings directly using the
-- public anon key, so lock that down to exactly what should be public.
alter table listings enable row level security;
alter table sellers enable row level security;
alter table buyer_requests enable row level security;

create policy "public can read active listings"
    on listings for select
    using (status = 'active');

-- No policies are created for sellers or buyer_requests, so RLS denies all
-- access to them from the anon key by default -- only the backend's
-- service-role key (which bypasses RLS) can read/write those.

