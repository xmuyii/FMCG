-- Run this in the Supabase SQL editor once, before deploying.

create table if not exists sellers (
    id bigint generated always as identity primary key,
    phone text unique not null,
    name text,
    created_at timestamptz default now()
);

create table if not exists listings (
    id bigint generated always as identity primary key,
    seller_id bigint references sellers (id),
    item text not null,
    quantity text,           -- kept as free text, e.g. "50kg", "20 pieces"
    price numeric not null,
    image_url text,          -- public Supabase Storage URL, set once processed
    status text default 'pending_image' check (status in ('pending_image', 'active', 'sold', 'removed')),
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
