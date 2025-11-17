-- Templated variables:
-- {{alias}}: The database alias to use

-- =================
-- Table definitions
-- =================

CREATE TABLE IF NOT EXISTS {{alias}}.cert_info (
    -- Logical PK: fingerprint
    fingerprint         VARCHAR NOT NULL,
    raw_cert            BLOB NOT NULL,
    version             INTEGER,
    subject             TEXT,
    issuer              TEXT,
    serial_number       TEXT,
    not_valid_before    TIMESTAMP,
    not_valid_after     TIMESTAMP,
    signature_algorithm TEXT,
    hash_algorithm      TEXT
);

CREATE TABLE IF NOT EXISTS {{alias}}.cert_observation (
    -- Logical PK: (timestamp, host, port, sni)
    -- Logical FK: fingerprint -> cert_info.fingerprint
    timestamp           TIMESTAMP NOT NULL,
    host                VARCHAR NOT NULL,
    port                INTEGER NOT NULL,
    sni                 VARCHAR,
    effective_host      VARCHAR NOT NULL,
    fingerprint         VARCHAR,
    valid               BOOLEAN,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS {{alias}}.dns_info (
    -- Logical PK: hash
    hash                VARCHAR NOT NULL,
    wire_bytes          BLOB
);

CREATE TABLE IF NOT EXISTS {{alias}}.dns_observation (
    -- Logical PK: (timestamp, host)
    -- Logical FK: *_semhash -> dns_info.hash
    timestamp           TIMESTAMP NOT NULL,
    host                VARCHAR NOT NULL,
    A_semhash           VARCHAR, A_expiry       TIMESTAMP,
    AAAA_semhash        VARCHAR, AAAA_expiry    TIMESTAMP,
    CAA_semhash         VARCHAR, CAA_expiry     TIMESTAMP,
    CNAME_semhash       VARCHAR, CNAME_expiry   TIMESTAMP,
    DMARC_semhash       VARCHAR, DMARC_expiry   TIMESTAMP,
    DNSKEY_semhash      VARCHAR, DNSKEY_expiry  TIMESTAMP,
    DS_semhash          VARCHAR, DS_expiry      TIMESTAMP,
    MX_semhash          VARCHAR, MX_expiry      TIMESTAMP,
    NS_semhash          VARCHAR, NS_expiry      TIMESTAMP,
    SOA_semhash         VARCHAR, SOA_expiry     TIMESTAMP,
    SPF_semhash         VARCHAR, SPF_expiry     TIMESTAMP,
    TXT_semhash         VARCHAR, TXT_expiry     TIMESTAMP
);

-- ============
-- Helper views
-- ============

CREATE VIEW IF NOT EXISTS {{alias}}.cert_join_view AS
SELECT
    o.timestamp,
    o.host, o.port, o.sni, o.effective_host,
    o.fingerprint, o.valid, o.error,
    i.subject, i.issuer, i.serial_number,
    i.not_valid_before, i.not_valid_after,
    i.signature_algorithm, i.hash_algorithm
FROM {{alias}}.cert_observation o
LEFT JOIN {{alias}}.cert_info i
  ON o.fingerprint = i.fingerprint;

CREATE VIEW IF NOT EXISTS {{alias}}.dns_join_view AS
SELECT
    o.timestamp,
    o.host,
    iA.wire_bytes     AS A,
    o.A_expiry        AS A_expiry,
    iAAAA.wire_bytes  AS AAAA,
    o.AAAA_expiry     AS AAAA_expiry,
    iCNAME.wire_bytes AS CNAME,
    o.CNAME_expiry    AS CNAME_expiry,
    iCAA.wire_bytes   AS CAA,
    o.CAA_expiry      AS CAA_expiry,
    iDMARC.wire_bytes AS DMARC,
    o.DMARC_expiry    AS DMARC_expiry,
    iDNSKEY.wire_bytes AS DNSKEY,
    o.DNSKEY_expiry    AS DNSKEY_expiry,
    iDS.wire_bytes     AS DS,
    o.DS_expiry        AS DS_expiry,
    iMX.wire_bytes     AS MX,
    o.MX_expiry        AS MX_expiry,
    iNS.wire_bytes     AS NS,
    o.NS_expiry        AS NS_expiry,
    iSOA.wire_bytes    AS SOA,
    o.SOA_expiry       AS SOA_expiry,
    iSPF.wire_bytes    AS SPF,
    o.SPF_expiry       AS SPF_expiry,
    iTXT.wire_bytes    AS TXT,
    o.TXT_expiry       AS TXT_expiry
FROM {{alias}}.dns_observation o
LEFT JOIN {{alias}}.dns_info iA       ON o.A_semhash     = iA.hash
LEFT JOIN {{alias}}.dns_info iAAAA    ON o.AAAA_semhash  = iAAAA.hash
LEFT JOIN {{alias}}.dns_info iCNAME   ON o.CNAME_semhash = iCNAME.hash
LEFT JOIN {{alias}}.dns_info iCAA     ON o.CAA_semhash   = iCAA.hash
LEFT JOIN {{alias}}.dns_info iDMARC   ON o.DMARC_semhash = iDMARC.hash
LEFT JOIN {{alias}}.dns_info iDNSKEY  ON o.DNSKEY_semhash = iDNSKEY.hash
LEFT JOIN {{alias}}.dns_info iDS      ON o.DS_semhash    = iDS.hash
LEFT JOIN {{alias}}.dns_info iMX      ON o.MX_semhash    = iMX.hash
LEFT JOIN {{alias}}.dns_info iNS      ON o.NS_semhash    = iNS.hash
LEFT JOIN {{alias}}.dns_info iSOA     ON o.SOA_semhash   = iSOA.hash
LEFT JOIN {{alias}}.dns_info iSPF     ON o.SPF_semhash   = iSPF.hash
LEFT JOIN {{alias}}.dns_info iTXT     ON o.TXT_semhash   = iTXT.hash;
