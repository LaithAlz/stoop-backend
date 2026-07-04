-- Early-access waitlist (issue #114)
CREATE TABLE IF NOT EXISTS waitlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  city TEXT,
  doors INTEGER,
  is_pm INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_waitlist_is_pm ON waitlist (is_pm);
