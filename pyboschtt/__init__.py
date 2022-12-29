"""Library to handle connection with Bosch Thermotechnology API."""
import asyncio
import json
import logging
from itertools import chain
import time
from urllib.parse import urlencode

import aiohttp
import async_timeout
from bs4 import BeautifulSoup
from yarl import URL
from base64 import urlsafe_b64encode, urlsafe_b64decode
import secrets
import hashlib


DEFAULT_TIMEOUT = 30
API_ENDPOINT = "https://pointt-api.bosch-thermotechnology.com/pointt-api/api/v1/"

_LOGGER = logging.getLogger(__name__)


class BoschTTConnection:
    """Class to comunicate with the BoschTT api."""

    def __init__(self, oauth, token_info, timeout=DEFAULT_TIMEOUT, websession=None):
        """Initialize the BoschTT connection."""
        if websession is None:

            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession
        self._timeout = timeout
        self.oauth = oauth
        self.token_info = token_info
        self._devices = []

    async def request(self, command, params=None, retry=3):
        """Request data."""
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token_info.get("access_token"),
        }

        url = API_ENDPOINT + command
        try:
            async with async_timeout.timeout(self._timeout):
                if params is None:
                    resp = await self.websession.get(
                        url, headers=headers, skip_auto_headers=("user-agent",)
                    )
                else:
                    resp = await self.websession.put(
                        url,
                        headers=headers,
                        json=params,
                        skip_auto_headers=("user-agent",),
                    )
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error(
                    "Timed out sending command to BoschTT: %s", command)
                return None
            return await self.request(command, params, retry - 1)
        except aiohttp.ClientError:
            _LOGGER.error(
                "Error sending command to BoschTT: %s", command, exc_info=True
            )
            return None
        if not resp.status in (200, 204):
            _LOGGER.error(await resp.text())
            return None
        return await resp.text()

    def find_device_by_room_name(self, room_name):
        """Get device by room name."""
        for device in self._devices:
            if device.name == room_name:
                return device
        return None

    async def find_devices(self):
        """Get users BoschTT device information."""
        res = await self.request("gateways/")
        if not res:
            return False
        res = json.loads(res)
        self._devices = []
        for device in res:
            dev = BoschTTDevice(device.get("deviceId"),
                                device.get("deviceType"), self)
            self._devices.append(dev)

        return bool(self._devices)

    def get_devices(self):
        """Get users BoschTT device information."""
        return self._devices

    async def refresh_access_token(self):
        """Refresh access token."""
        token_info = await self.oauth.refresh_access_token(self.token_info)
        if token_info is None:
            return None
        self.token_info = token_info
        return token_info


class BoschTTDevice:
    """Instance of BoschTT device."""


    def __init__(self, device_id, device_type, boschtt_control):
        """Initialize the BoschTT device class."""
        self._device_id = device_id
        self._device_type = device_type
        self.control = boschtt_control
        self.ir_features = None
        self.ac_data = None
        self._mode = None

    @property
    def device_id(self):
        """Return a device ID."""
        return self._device_id

    @property
    def name(self):
        """Return a device name."""
        return self._device_id

    async def request(self, command, params=None, retry=3):
        """Request data."""
        res = await self.control.request(
            f"gateways/{self.device_id}/{command}", params, retry
        )
        try:
            if res:
                res = json.loads(res)
            if params is not None:
                return res
            if isinstance(res, dict) and res.get("error"):
                _LOGGER.error(res.get("error"))
            return res
        except TypeError:
            if isinstance(res, dict):
                status = res.get("status")
                if status is not None:
                    if status == "ok":
                        return True
                    return False
        return None

    async def discover(self):
        await self.update()

    async def update(self):
        self.standard_functions = await self.request(
            "resource/airConditioning/standardFunctions"
        )
        self.advanced_functions = await self.request(
            "resource/airConditioning/advancedFunctions"
        )
        self.last_update = time.time()

    def invalidate(self):
        self.last_update = 0

    async def get_resources(self):
        if time.time() - self.last_update > 60:
            await self.update()
        return chain(
            self.standard_functions["references"], self.advanced_functions["references"]
        )

    async def get_resource(self, id):
        for ref in await self.get_resources():
            if ref["id"] == id:
                return ref
        return None

    async def set_value(self, id, new_value):
        for ref in await self.get_resources():
            if ref["id"] == id:
                if ref["writeable"]:
                    if ref["type"] == "stringValue":
                        if new_value in ref["allowedValues"]:
                            await self.request(f"resource{id}", {"value": new_value})
                            self.invalidate()
                            return
                        else:
                            _LOGGER.error(
                                f"{id} cannot be written the value {new_value} - it is not in the list of allowed values"
                            )
                    elif ref["type"] == "floatValue":
                        new_value = float(new_value)
                        if (
                            new_value >= ref["minValue"]
                            and new_value <= ref["maxValue"]
                        ):
                            await self.request(f"resource{id}", {"value": new_value})
                            self.invalidate()
                            return
                        else:
                            _LOGGER.error(
                                f"{id} cannot be written the value {new_value} - it is not within range {ref['minValue']}<= x <={ref['maxValue']}"
                            )
                else:
                    _LOGGER.error(f"{id} is not writable")

    async def get_sensor_temperature(self):
        """Get latest sensor temperature data."""
        res = await self.get_resource("/airConditioning/roomTemperature")
        if res is None:
            return None
        return res.get("value")

    async def set_target_temperature(self, temperature):
        """Set target temperature."""
        return await self.set_value("/airConditioning/temperatureSetpoint", temperature)

    async def is_turned_off(self):
        res = await self.get_resource("/airConditioning/acControl")
        return res.get("value") == "off"

    async def turn_off(self):
        """Turn off."""
        return await self.set_value("/airConditioning/acControl", "off")

    async def turn_on(self):
        """Turn on."""
        return await self.set_value("/airConditioning/acControl", "on")

    async def get_min_temp(self):
        """Get min temperature."""
        res = await self.get_resource("/airConditioning/temperatureSetpoint")
        return res["minValue"]

    async def get_max_temp(self):
        """Get max temperature."""
        res = await self.get_resource("/airConditioning/temperatureSetpoint")
        return res["maxValue"]


class BoschTTOauthError(Exception):
    """BoschTTOauthError."""


class BoschTTOAuth:
    """Implements Authorization Code Flow for BoschTT's OAuth implementation."""

    OAUTH_AUTHORIZE_URL = "https://identity.bosch.com/connect/authorize"
    OAUTH_TOKEN_URL = "https://identity.bosch.com/connect/token"

    def __init__(self, client_id, client_secret, websession=None):
        """Create a BoschTTOAuth object."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = "com.bosch.tt.dashtt.pointt://"
        if websession is None:

            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_running_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession

    async def authenticate(self, email, password):
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            urlsafe_b64encode(hashlib.sha256(
                code_verifier.encode("utf-8")).digest())
            .decode("utf-8")
            .rstrip("=")
        )

        authurl = self.get_authorize_url(code_challenge)
        result = await self.websession.get(
            authurl, allow_redirects=True, skip_auto_headers=("user-agent",)
        )
        result.raise_for_status()
        soup = BeautifulSoup(await result.read(), "html.parser")
        payload = {}
        for htmlinput in soup.find_all("input"):
            if not htmlinput.get("name"):
                continue
            payload[htmlinput.get("name")] = (
                htmlinput.get("value") if htmlinput.get(
                    "value") != None else ""
            )

        for k in payload.keys():
            if "email" in k.lower():
                payload[k] = email
            elif "password" in k.lower():
                payload[k] = password

        resp = await self.websession.post(
            result.url,
            data=payload,
            allow_redirects=False,
            skip_auto_headers=("user-agent",),
        )
        resp.raise_for_status()
        while resp.status in (301, 302):
            location = URL(resp.headers.get("location"))
            if not location.is_absolute():
                location = resp.request_info.url.join(location)

            if location.scheme not in ("http", "https"):
                break
            resp = await self.websession.get(
                location, allow_redirects=False, skip_auto_headers=("user-agent",)
            )
            resp.raise_for_status()

        if location.scheme == "com.bosch.tt.dashtt.pointt":
            return await self.get_access_token(location.query["code"], code_verifier)

        raise BoschTTOauthError()

    def get_authorize_url(self, code_challenge):
        """Get the URL to use to authorize this app."""
        payload = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": "yh5zM2WWv5K_OL-ScUtucQ",
            "nonce": "BEbxXXrubvNQbyFHVxFc_g",
            "scope": "openid offline_access user.identity user.write user.read company.identity company.read tos.write pointt.gateway.claiming pointt.gateway.removal pointt.gateway.list pointt.gateway.users pointt.gateway.resource.dashapp pointt.castt.flow.token-exchange email",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return self.OAUTH_AUTHORIZE_URL + "?" + urlencode(payload)

    async def get_access_token(self, code, code_verifier):
        """Get the access token for the app given the code."""
        payload = {
            "redirect_uri": self.redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        try:
            async with async_timeout.timeout(DEFAULT_TIMEOUT):
                response = await self.websession.post(
                    self.OAUTH_TOKEN_URL,
                    data=payload,
                    auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
                    skip_auto_headers=("user-agent",),
                    allow_redirects=True,
                )
                if response.status != 200:
                    raise BoschTTOauthError(response.status)
                token_info = await response.json()
                token_info["expires_at"] = int(
                    time.time()) + token_info["expires_in"]
                return token_info
        except (asyncio.TimeoutError, aiohttp.ClientError):
            _LOGGER.error("Timeout calling BoschTT to get auth token.")
            return None
        return None

    async def refresh_access_token(self, token_info):
        """Refresh access token."""
        if token_info is None:
            return token_info
        if not is_token_expired(token_info):
            return token_info

        payload = {
            "client_id": self.client_id,
            "refresh_token": token_info["refresh_token"],
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }

        refresh_token = token_info.get("refresh_token")

        try:
            async with async_timeout.timeout(DEFAULT_TIMEOUT):
                response = await self.websession.post(
                    self.OAUTH_TOKEN_URL,
                    data=payload,
                    skip_auto_headers=("user-agent",),
                    allow_redirects=True,
                )
                if response.status != 200:
                    _LOGGER.error(
                        "Failed to refresh access token: %s", response)
                    return None
                token_info = await response.json()
                token_info["expires_at"] = int(
                    time.time()) + token_info["expires_in"]
                if "refresh_token" not in token_info:
                    token_info["refresh_token"] = refresh_token
                return token_info
        except (asyncio.TimeoutError, aiohttp.ClientError):
            _LOGGER.error("Timeout calling BoschTT to get auth token.")
        return None


def is_token_expired(token_info):
    """Check if token is expired."""
    return token_info["expires_at"] - int(time.time()) < 60 * 60
