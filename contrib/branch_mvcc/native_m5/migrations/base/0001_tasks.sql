-- IDs and field values are supplied by the app, with no shared sequence/default.
CREATE TABLE tasks (
    id uuid PRIMARY KEY,
    title text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL
);
