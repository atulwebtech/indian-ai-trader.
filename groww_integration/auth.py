"""Groww authentication with expiry handling.

Documented flows (groww.in/trade-api/docs/python-sdk):
  * API key + secret:  GrowwAPI.get_access_token(api_key=..., secret=...)   (needs DAILY approval on the
    Groww Cloud API Keys page, so the token effectively expires every day)
  * API key + TOTP:    GrowwAPI.get_access_token(api_key=..., totp=...)     (token has no expiry)
The result is passed to GrowwAPI(token). Credentials only ever come from .env.
"""
import logging
import time

from groww_integration.safe_client import SafeGroww

log = logging.getLogger(__name__)
MAX_AUTH_PER_DAY = 5   # Groww allows far more; we stay well under to avoid lock-outs


class GrowwAuthError(Exception):
    """Authentication is impossible or failed; the message tells the user what to do."""


def _auth_exceptions(sdk_exceptions):
    return tuple(getattr(sdk_exceptions, n) for n in ("GrowwAPIAuthenticationException", "GrowwAPIAuthorisationException")
                 if hasattr(sdk_exceptions, n))


def _extract_token(result) -> str:
    if isinstance(result, str) and result:
        return result
    if isinstance(result, dict):
        for k in ("token", "access_token", "accessToken"):
            if result.get(k):
                return result[k]
    raise GrowwAuthError("Groww returned an unexpected token format")


def _real_totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError as e:
        raise GrowwAuthError("TOTP login needs the 'pyotp' package: .venv/bin/python -m pip install pyotp") from e
    return pyotp.TOTP(secret).now()


class GrowwSession:
    def __init__(self, api_key: str, secret: str = "", totp_secret: str = "", growwapi=None, exceptions=None,
                 totp_factory=None, clock=time.time):
        if not api_key:
            raise GrowwAuthError("GROWW_API_KEY is empty. Fill it in .env after purchasing the Groww Trading API.")
        if not (secret or totp_secret):
            raise GrowwAuthError("Set GROWW_API_SECRET (needs daily approval on Groww's API Keys page) "
                                 "or GROWW_TOTP_SECRET in .env.")
        self.api_key, self.secret, self.totp_secret = api_key, secret, totp_secret
        self._api_cls = growwapi
        self._exceptions = exceptions
        self._totp_factory = totp_factory
        self.clock = clock
        self._auth_times = []
        self._client = None
        self.connected_at = None

    # ---- SDK loading (lazy, so the project runs without growwapi installed) ----
    def _sdk(self):
        if self._api_cls is None:
            try:
                from growwapi import GrowwAPI
                from growwapi.groww import exceptions
            except ImportError as e:
                raise GrowwAuthError("The 'growwapi' package is not installed. Run: .venv/bin/python -m pip install growwapi") from e
            self._api_cls, self._exceptions = GrowwAPI, exceptions
        return self._api_cls

    def _new_token(self) -> str:
        now = self.clock()
        self._auth_times = [t for t in self._auth_times if now - t < 86400]
        if len(self._auth_times) >= MAX_AUTH_PER_DAY:
            raise GrowwAuthError(f"Stopped after {MAX_AUTH_PER_DAY} login attempts in 24h to protect your account.")
        self._auth_times.append(now)
        api = self._sdk()
        try:
            if self.totp_secret:
                code = (self._totp_factory or _real_totp)(self.totp_secret)
                return _extract_token(api.get_access_token(api_key=self.api_key, totp=code))
            return _extract_token(api.get_access_token(api_key=self.api_key, secret=self.secret))
        except _auth_exceptions(self._exceptions or object()) as e:
            raise GrowwAuthError(
                "Groww rejected the login. If you use API key + secret, approve today's key on Groww Cloud's "
                f"API Keys page, then restart. (code {getattr(e, 'code', '?')})") from None

    def connect(self) -> SafeGroww:
        token = self._new_token()
        self._client = self._api_cls(token)
        self.connected_at = self.clock()
        log.info("Groww connected (read-only wrapper active)")
        return SafeGroww(self._client)

    @property
    def client(self) -> SafeGroww:
        if self._client is None:
            return self.connect()
        return SafeGroww(self._client)

    def call(self, method: str, **kwargs):
        """Call an allowed read-only method; on an authentication failure log in once more and retry."""
        auth_exc = _auth_exceptions(self._exceptions or object())
        for attempt in (1, 2):
            try:
                return getattr(self.client, method)(**kwargs)
            except auth_exc:
                if attempt == 2:
                    raise GrowwAuthError("Groww token expired or rejected and re-login failed. Approve today's key on "
                                         "Groww Cloud's API Keys page and restart.") from None
                log.warning("Groww token rejected; logging in again")
                self._client = None
