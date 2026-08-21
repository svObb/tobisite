"""Проверка токена Cloudflare Access на каждом запросе панели.

Снаружи в панель нельзя постучаться мимо туннеля, но заголовок проверяется всё
равно: без него любой контейнер во внутренней сети compose читал бы базу лидов.
Проверяются подпись по JWKS команды Access, aud, iss, срок и почта из клеймов.

Ключи Access живут долго и меняются редко, поэтому JWKS кэшируется на 12 часов;
единственный повод сходить за ними раньше — токен с неизвестным kid (ротация).
"""
import time

import aiohttp
import jwt

CF_HEADER = "Cf-Access-Jwt-Assertion"
JWKS_TTL = 12 * 3600
FETCH_TIMEOUT = 10


class Denied(Exception):
    """Причина отказа — только в лог: наружу уходит 403 без подробностей."""


class AccessVerifier:
    def __init__(self, *, team_domain: str, aud: str, allowed_emails,
                 jwks: dict | None = None):
        self.issuer = f"https://{team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.aud = aud
        self.emails = {e.strip().lower() for e in allowed_emails if e.strip()}
        # набор ключей, отданный конструктору, — это тесты: обновлять его
        # неоткуда и незачем, в сеть за ним не ходим
        self.static = jwks is not None
        self.keys = _parse(jwks) if jwks else {}
        self.fetched_at = 0.0

    async def email(self, token: str) -> str:
        """Почта из валидного токена. Любой отказ — Denied, а не пустой ответ."""
        if not token:
            raise Denied("нет заголовка " + CF_HEADER)
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise Denied(f"битый заголовок токена: {e}") from e
        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.aud,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise Denied(f"токен не принят: {e}") from e
        email = (claims.get("email") or "").strip().lower()
        if email not in self.emails:
            raise Denied(f"почта {email or '—'} не в списке доступа")
        return email

    async def _key(self, kid: str | None):
        keys = await self._keys()
        key = keys.get(kid)
        if key is None and not self.static:
            # Access перевыпустил ключи раньше, чем истёк наш кэш
            keys = await self._keys(force=True)
            key = keys.get(kid)
        if key is None:
            raise Denied(f"ключ {kid or '—'} не найден в JWKS")
        return key

    async def _keys(self, force: bool = False) -> dict:
        fresh = time.monotonic() - self.fetched_at < JWKS_TTL
        if self.static or (self.keys and fresh and not force):
            return self.keys
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.certs_url) as response:
                    response.raise_for_status()
                    data = await response.json()
            self.keys = _parse(data)
        except (aiohttp.ClientError, TimeoutError, ValueError,
                jwt.PyJWTError) as e:
            # без ключей проверить подпись нечем — закрываемся, а не пускаем
            raise Denied(f"JWKS недоступен: {e}") from e
        self.fetched_at = time.monotonic()
        return self.keys


def _parse(jwks: dict) -> dict:
    return {k.key_id: k for k in jwt.PyJWKSet.from_dict(jwks).keys}
