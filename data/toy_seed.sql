DROP TABLE IF EXISTS claims;
DROP TABLE IF EXISTS encounters;
DROP TABLE IF EXISTS patients;

CREATE TABLE patients (
  patient_id SERIAL PRIMARY KEY,
  full_name  TEXT NOT NULL,
  birth_date DATE NOT NULL,
  sex        TEXT NOT NULL
);

CREATE TABLE encounters (
  encounter_id SERIAL PRIMARY KEY,
  patient_id   INT NOT NULL REFERENCES patients(patient_id),
  encounter_date DATE NOT NULL,
  department   TEXT NOT NULL
);

CREATE TABLE claims (
  claim_id    SERIAL PRIMARY KEY,
  encounter_id INT NOT NULL REFERENCES encounters(encounter_id),
  payer       TEXT NOT NULL,
  amount      NUMERIC(10,2) NOT NULL,
  status      TEXT NOT NULL
);

INSERT INTO patients (full_name, birth_date, sex) VALUES
('Ana Lopez', '1985-04-12', 'F'),
('James Kim', '1978-11-03', 'M'),
('Riley Chen', '1992-06-25', 'X');

INSERT INTO encounters (patient_id, encounter_date, department) VALUES
(1, '2025-10-01', 'Primary Care'),
(2, '2025-10-02', 'Emergency'),
(1, '2025-11-15', 'Cardiology');

INSERT INTO claims (encounter_id, payer, amount, status) VALUES
(1, 'Aetna', 125.50, 'Paid'),
(2, 'BCBS', 980.00, 'Denied'),
(3, 'Aetna', 450.00, 'Pending');

