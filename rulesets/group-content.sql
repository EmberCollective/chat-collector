-- Keep the group restriction and content alternatives inside the SAME rule.
SELECT identity FROM candidates
WHERE conversation_type = 'group' AND conversation_id = :group_id
  AND (kind = 'file' OR mentions_all = 1
       OR (length(:keyword) > 0 AND instr(lower(text), lower(:keyword)) > 0));
