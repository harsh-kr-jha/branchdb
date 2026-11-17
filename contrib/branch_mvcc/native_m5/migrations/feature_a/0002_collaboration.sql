ALTER TABLE tasks ADD COLUMN assignee text;
CREATE TABLE task_notes (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    body text NOT NULL
);
