-- A new message near a file/@all anchor reselects its complete available context.
WITH ordered AS (
    SELECT *, row_number() OVER (
        PARTITION BY platform, account_id, conversation_type, conversation_id
        ORDER BY sent_at, local_id, database_name, message_id, identity
    ) AS position
    FROM messages WHERE sent_at < :window_end
)
SELECT target.identity FROM ordered target
WHERE EXISTS (
    SELECT 1 FROM ordered anchor
    WHERE anchor.platform = target.platform AND anchor.account_id = target.account_id
      AND anchor.conversation_type = target.conversation_type
      AND anchor.conversation_id = target.conversation_id
      AND (anchor.mentions_all = 1 OR anchor.kind = 'file')
      AND abs(target.position - anchor.position) <= :recent_count
      AND EXISTS (
          SELECT 1 FROM ordered fresh
          WHERE fresh.platform = anchor.platform AND fresh.account_id = anchor.account_id
            AND fresh.conversation_type = anchor.conversation_type
            AND fresh.conversation_id = anchor.conversation_id
            AND fresh.eligible = 1
            AND abs(fresh.position - anchor.position) <= :recent_count
      )
);
