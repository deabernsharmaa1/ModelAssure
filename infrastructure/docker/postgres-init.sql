-- Runs once when the Postgres volume is first created.
-- The application connects as this NON-owner role so row-level security always applies to it.
-- Change the password for any shared environment (and MA_DATABASE_URL to match).
CREATE ROLE modelassure_app LOGIN PASSWORD 'modelassure_app';
