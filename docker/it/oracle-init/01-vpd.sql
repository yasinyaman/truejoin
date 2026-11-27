-- Run by gvenzl/oracle-free as SYS on the first start: the grant the Oracle VPD
-- integration test needs (tests/warp/integration/test_session_identity.py).
ALTER SESSION SET CONTAINER = FREEPDB1;
GRANT EXECUTE ON SYS.DBMS_RLS TO warp;
