SELECT r.person, r.decision, a.at, a.kind, a.provider, a.summary
FROM replies r
JOIN actions a ON a.seq > r.seq
WHERE r.kind = 'decision' AND r.decision = 'reject'
  AND a.kind IN ('write', 'stored', 'message')
ORDER BY a.position;
