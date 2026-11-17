ALTER TABLE tasks ADD COLUMN priority integer;
CREATE TABLE task_notes (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    score integer NOT NULL
);
