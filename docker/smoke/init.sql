-- The release smoke's database: two tenants, a personal column, and keys the
-- joins can follow. Small enough to read, and every expected number in
-- scripts/smoke.py is computed from these rows.
CREATE TABLE subeler (
    sube_kodu VARCHAR(10) PRIMARY KEY,
    sehir     VARCHAR(40) NOT NULL
);
CREATE TABLE musteriler (
    musteri_id SERIAL PRIMARY KEY,
    ad         VARCHAR(60) NOT NULL,
    tc_kimlik  VARCHAR(11) NOT NULL,
    kiraci     VARCHAR(10) NOT NULL,
    sube_kodu  VARCHAR(10) NOT NULL REFERENCES subeler (sube_kodu)
);
CREATE TABLE islemler (
    islem_id   SERIAL PRIMARY KEY,
    musteri_id INT NOT NULL REFERENCES musteriler (musteri_id),
    kiraci     VARCHAR(10) NOT NULL,
    tutar      NUMERIC(12, 2) NOT NULL,
    durum      VARCHAR(20) NOT NULL
);

INSERT INTO subeler VALUES ('IST01', 'Istanbul'), ('ANK02', 'Ankara');
INSERT INTO musteriler (ad, tc_kimlik, kiraci, sube_kodu) VALUES
    ('Ayse',   '10000000146', 'a', 'IST01'),
    ('Mehmet', '20000000292', 'a', 'ANK02'),
    ('Zeynep', '30000000338', 'b', 'IST01');
INSERT INTO islemler (musteri_id, kiraci, tutar, durum) VALUES
    (1, 'a', 100.00, 'basarili'),
    (1, 'a',  50.00, 'iptal'),
    (2, 'a', 200.00, 'basarili'),
    (3, 'b', 999.00, 'basarili');
