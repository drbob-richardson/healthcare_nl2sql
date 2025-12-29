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
  claim_id     SERIAL PRIMARY KEY,
  encounter_id INT NOT NULL REFERENCES encounters(encounter_id),
  payer        TEXT NOT NULL,
  amount       NUMERIC(10,2) NOT NULL,
  status       TEXT NOT NULL
);

-- Patients
INSERT INTO patients (full_name, birth_date, sex)
SELECT
  'Patient ' || gs::text,
  date '1940-01-01' + (random() * 30000)::int,
  (ARRAY['F','M','X'])[1 + (random()*2)::int]
FROM generate_series(1, 1000) gs;

-- Encounters
INSERT INTO encounters (patient_id, encounter_date, department)
SELECT
  1 + (random()*999)::int,
  date '2024-01-01' + (random() * 730)::int,
  (ARRAY['Emergency','Primary Care','Cardiology','Oncology','Orthopedics','Pediatrics'])[1 + (random()*5)::int]
FROM generate_series(1, 5000);

-- Claims (2 claims per encounter on average)
INSERT INTO claims (encounter_id, payer, amount, status)
SELECT
  1 + (random()*4999)::int,
  (ARRAY['Aetna','BCBS','Cigna','Medicare','Medicaid','United'])[1 + (random()*5)::int],
  round((20 + random()*5000)::numeric, 2),
  (ARRAY['Paid','Denied','Pending'])[1 + (random()*2)::int]
FROM generate_series(1, 10000);

