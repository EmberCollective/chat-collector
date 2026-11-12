-- Deprecated filename: keywords now match ONLY the current message.
-- Use keyword.sql for new registrations, context.sql for file/@all neighbors.
SELECT identity FROM candidates
WHERE length(:keyword) > 0 AND instr(lower(text), lower(:keyword)) > 0;
