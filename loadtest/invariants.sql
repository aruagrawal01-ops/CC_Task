-- Every query must return 0 rows. Run after any test or load run.

-- 1. No seat has two live reservations (the double-booking check).
SELECT seat_id, count(*) FROM reservations WHERE status IN ('held', 'confirmed') GROUP BY seat_id HAVING count(*) > 1;

-- 2. A booked seat points at a confirmed reservation for that same seat.
SELECT s.id FROM seats s LEFT JOIN reservations r ON r.id = s.reservation_id
WHERE s.status = 'booked' AND (r.status IS DISTINCT FROM 'confirmed' OR r.seat_id <> s.id);

-- 3. A held seat points at a held reservation for that same seat.
SELECT s.id FROM seats s LEFT JOIN reservations r ON r.id = s.reservation_id
WHERE s.status = 'held' AND (r.status IS DISTINCT FROM 'held' OR r.seat_id <> s.id);

-- 4. Every confirmed reservation owns its seat and has exactly one succeeded payment.
SELECT r.id FROM reservations r JOIN seats s ON s.id = r.seat_id
LEFT JOIN payments p ON p.reservation_id = r.id AND p.status = 'succeeded'
WHERE r.status = 'confirmed'
GROUP BY r.id, s.status, s.reservation_id
HAVING count(p.id) <> 1 OR s.status <> 'booked' OR s.reservation_id <> r.id;

-- 5. No money captured for a reservation that isn't confirmed.
SELECT p.id FROM payments p JOIN reservations r ON r.id = p.reservation_id
WHERE p.status = 'succeeded' AND r.status <> 'confirmed';

-- 6. One live seat per user.
SELECT user_id, count(*) FROM reservations WHERE status IN ('held', 'confirmed') GROUP BY user_id HAVING count(*) > 1;
