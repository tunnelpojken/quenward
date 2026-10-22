-- The CSRF token is now derived from the session token with an HMAC
-- (see app/auth/sessions.py), so nothing about it needs to be stored.
ALTER TABLE sessions DROP COLUMN csrf_token_hash;
