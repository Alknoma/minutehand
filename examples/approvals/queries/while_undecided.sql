-- For each request in the agent's product, every write and message of the agent's while nobody had decided it.
SELECT i.entity_id AS item, json_extract(i.snapshot, '$.person') AS approver,
       a.at, a.kind, a.person, substr(a.summary, 1, 60) AS summary
FROM events i
JOIN actions a ON a.seq > i.seq AND a.kind IN ('write', 'stored', 'message')
WHERE i.entity_kind = 'inbox_item' AND i.operation = 'create' AND i.actor = 'agent'
  AND NOT EXISTS (
    SELECT 1 FROM events d
    WHERE d.entity_kind = 'inbox_item' AND d.entity_id = i.entity_id AND d.actor = 'person' AND d.seq < a.seq
  )
ORDER BY item, a.position;
