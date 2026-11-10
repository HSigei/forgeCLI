-- wrangler d1 execute forge-ads --remote --file=schema.sql
CREATE TABLE IF NOT EXISTS sponsors (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  name    TEXT NOT NULL DEFAULT '',          -- who paid (for you, never shown)
  text    TEXT NOT NULL,                     -- the one line users see, max 90 chars
  url     TEXT NOT NULL DEFAULT '',          -- one plain https link
  starts  INTEGER NOT NULL DEFAULT 0,        -- unix seconds
  ends    INTEGER NOT NULL DEFAULT 4102444800,
  paid    INTEGER NOT NULL DEFAULT 0,        -- showings paid for; stops at this many
  shown   INTEGER NOT NULL DEFAULT 0,        -- showings so far (bill from this)
  active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS caps (           -- frequency cap; hashed install ids, one row per day
  who   TEXT NOT NULL,
  day   TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (who, day)
);
