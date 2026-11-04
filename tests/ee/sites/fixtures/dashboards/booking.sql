-- Eval schema: appointment booking (epoch-second timestamps, staff and customer PII).
CREATE TABLE services (id INTEGER PRIMARY KEY, name TEXT, minutes INTEGER, price_cents INTEGER);
CREATE TABLE staff (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT);
CREATE TABLE appointments (id INTEGER PRIMARY KEY, service_id INTEGER REFERENCES services(id),
  staff_id INTEGER REFERENCES staff(id), starts_at INTEGER, status TEXT, customer_name TEXT,
  customer_phone TEXT);
INSERT INTO services VALUES (1, 'Cleaning', 30, 6000), (2, 'Whitening', 60, 15000);
INSERT INTO staff VALUES (1, 'Dr Grey', 'grey@clinic.io', 'dentist');
INSERT INTO appointments VALUES (1, 1, 1, 1790000000, 'booked', 'Ann', '+1 555'),
  (2, 2, 1, 1790086400, 'booked', 'Ben', '+1 556'),
  (3, 1, 1, 1790172800, 'cancelled', 'Cat', '+1 557');
