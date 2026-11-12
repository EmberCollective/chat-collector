SELECT identity FROM candidates
WHERE length(:keyword) > 0 AND instr(lower(text), lower(:keyword)) > 0;
