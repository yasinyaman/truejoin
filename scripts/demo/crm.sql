-- The sample setup's second database, a MySQL: what a CRM would hold beside
-- the bank's own PostgreSQL (docker/smoke/init.sql). Made-up, like that one,
-- and keyed to it: a branch code, a customer id. The two databases know
-- nothing of each other; Truejoin joins them.
--
-- Plain ASCII on purpose: the container's first-start client need not agree
-- with the server about a character set.
CREATE TABLE sube_hedefleri (
    hedef_id    INT AUTO_INCREMENT PRIMARY KEY,
    sube_kodu   VARCHAR(10) NOT NULL,
    yil         INT NOT NULL,
    hedef_tutar DECIMAL(12, 2) NOT NULL
);
CREATE TABLE musteri_segmentleri (
    kayit_id   INT AUTO_INCREMENT PRIMARY KEY,
    musteri_id INT NOT NULL,
    kiraci     VARCHAR(10) NOT NULL,
    segment    VARCHAR(20) NOT NULL,
    telefon    VARCHAR(20) NOT NULL
);

INSERT INTO sube_hedefleri (sube_kodu, yil, hedef_tutar) VALUES
    ('IST01', 2026, 1200.00),
    ('ANK02', 2026,  150.00);
INSERT INTO musteri_segmentleri (musteri_id, kiraci, segment, telefon) VALUES
    (1, 'a', 'bireysel', '05321110146'),
    (2, 'a', 'kurumsal', '05322220292'),
    (3, 'b', 'bireysel', '05333330338');
