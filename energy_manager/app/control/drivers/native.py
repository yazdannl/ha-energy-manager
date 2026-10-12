"""Native Zendure (SolarFlow) driver: the device's own local zenSDK HTTP API (SPEC 5, 9.2).

No Home Assistant integration is involved: the add-on speaks the device's documented local HTTP
JSON API directly (docs/EXTERNAL_APIS.md section 6, native local API), so it needs no `zendure_ha`
entities, no cloud account and no MQTT broker.

Verified against the vendor documentation and the reference implementation (docs section 7):

* ``GET <api_path>/report`` returns the full property report (``properties``, ``packData``,
  ``sn``); ``POST <api_path>/write`` takes ``{"sn": ..., "properties": {...}}`` with the raw
  zenSDK property vocabulary.  The vendor's own examples write plain HTTP on port 80 and no token.
* Write convention (magnitudes, direction in ``acMode`` -- 1 = input/charge, 2 = output/discharge):
  charge ``{smartMode: 1, acMode: 1, inputLimit: X, outputLimit: 0}``, discharge
  ``{smartMode: 1, acMode: 2, outputLimit: Y, inputLimit: 0}``, hold both limits 0 with
  ``smartMode: 0``.
* ``electricLevel`` is the device SoC, ``gridInputPower``/``outputHomePower`` the measured AC pair
  (charging/discharging), ``inputLimit``/``outputLimit`` the appliance's own limits,
  ``chargeMaxLimit``/``inverseMaxPower`` its own maxima, and ``packData[].minVol`` (0.01 V) the
  lowest pack cell voltage.  The device reports **no** capacity in kWh, so ``capacity_kwh`` stays
  the user's figure and is never fabricated from pack voltages.

Mapping keys (see `docs/REFERENCE.md` section 3.4; the request called the base path
``control_path``, stored here as ``api_path`` so it cannot be confused with the Zendure-HA
write-path field ``control_path``):

``host`` (required), ``serial`` (required), ``scheme`` (``http`` default), ``port`` (default
80/443), ``api_path`` (default ``/properties``), ``token`` (optional, sent as
``Authorization: Bearer ...`` -- assumed, the vendor documents only a possible 401), ``timeout_s``
(default 3), ``insecure_tls`` (opt-in for a self-signed HTTPS endpoint) and ``allow_unreachable``
(skips the save-time reachability probe and lets the card show the read error instead).
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Mapping

from ...models import BatteryState, clamp
from .base import BaseDriver, DriverError, _Write, parse_number, parse_soc_pct

log = logging.getLogger(__name__)

#: Mapping keys.  ``api_path`` is the API base: report = ``<api_path>/report``.
HOST_KEY = "host"
SERIAL_KEY = "serial"
SCHEME_KEY = "scheme"
PORT_KEY = "port"
API_PATH_KEY = "api_path"
TOKEN_KEY = "token"
TIMEOUT_KEY = "timeout_s"
INSECURE_TLS_KEY = "insecure_tls"
ALLOW_UNREACHABLE_KEY = "allow_unreachable"

DEFAULT_API_PATH = "/properties"
DEFAULT_TIMEOUT_S = 3.0
#: The save-time probe never waits longer than this, even when ``timeout_s`` is larger.
PROBE_TIMEOUT_CAP_S = 5.0
#: Node properties bound by the device's own constants.
SMART_MODE_KEY = "smartMode"
AC_MODE_KEY = "acMode"
INPUT_LIMIT_KEY = "inputLimit"
OUTPUT_LIMIT_KEY = "outputLimit"
AC_MODE_INPUT = 1
AC_MODE_OUTPUT = 2
#: Battery flow tolerance (W): the vendor's own constant, echoed back by the device.
ZENDURE_NATIVE_TOLERANCE_W = 5.0
#: Synthetic write identifier for the dispatch log -- never a host, serial or token.
DEVICE_WRITE_ID = "zen_sdk"
ZEN_SDK_WRITE_SERVICE = "zen_sdk.write"


class ZenSdkError(RuntimeError):
    """A transport, HTTP or payload failure against the device's local API."""


def _truthy(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in ("1", "true", "yes", "on")


def _clean_values(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """Mapping values with blanks dropped; connection keys are strings in ``BatteryConfig``."""
    return {
        str(key): str(value).strip()
        for key, value in (mapping or {}).items()
        if value is not None and str(value).strip() != ""
    }


def validate_connection(mapping: Mapping[str, Any] | None) -> list[str]:
    """Problems that make the connection settings unusable (checked at save time and creation)."""
    values = _clean_values(mapping)
    problems: list[str] = []
    host = values.get(HOST_KEY, "")
    if not host:
        problems.append("mapping: host is required (the device's IP address or hostname)")
    elif "://" in host or "/" in host or any(char.isspace() for char in host):
        problems.append(
            "mapping: host must be a bare IP address or hostname (no scheme, port or path)"
        )
    serial = values.get(SERIAL_KEY, "")
    if not serial:
        problems.append("mapping: serial is required (the device serial number, sn)")
    elif any(char.isspace() for char in serial):
        problems.append("mapping: serial must not contain whitespace")
    scheme = values.get(SCHEME_KEY, "http").lower()
    if scheme not in ("http", "https"):
        problems.append("mapping: scheme must be http or https")
    port = values.get(PORT_KEY)
    if port:
        try:
            number = int(float(port))
        except ValueError:
            problems.append("mapping: port must be a whole number")
        else:
            if not 1 <= number <= 65535:
                problems.append("mapping: port must be within 1..65535")
    timeout = values.get(TIMEOUT_KEY)
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError:
            problems.append("mapping: timeout_s must be a number of seconds")
        else:
            if not 0.0 < seconds <= 60.0:
                problems.append("mapping: timeout_s must be within 0..60 seconds")
    path = values.get(API_PATH_KEY, DEFAULT_API_PATH)
    if not path.startswith("/") or any(char.isspace() for char in path):
        problems.append("mapping: api_path must start with / and contain no whitespace")
    return problems


class ZenSdkClient:
    """Standard-library HTTP(S) client for one device's zenSDK endpoint.

    ``read`` parses the property report, ``write`` posts property values.  Both raise
    :class:`ZenSdkError` with a short, token-free message; the caller turns that into a driver
    error, and a failing input is never allowed to crash the tick.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        scheme: str = "http",
        token: str | None = None,
        api_path: str = DEFAULT_API_PATH,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        insecure_tls: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.scheme = scheme
        self.token = token or None
        self.api_path = api_path.rstrip("/") or DEFAULT_API_PATH
        self.timeout_s = float(timeout_s)
        self.insecure_tls = bool(insecure_tls)
        self._message_id = 0
        self._context = self._ssl_context()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "ZenSdkClient":
        """Build a client from a battery mapping, raising ``DriverError`` when malformed."""
        problems = validate_connection(mapping)
        if problems:
            raise DriverError("; ".join(problems))
        values = _clean_values(mapping)
        scheme = values.get(SCHEME_KEY, "http").lower()
        port = (
            int(float(values[PORT_KEY]))
            if values.get(PORT_KEY)
            else (443 if scheme == "https" else 80)
        )
        return cls(
            host=values[HOST_KEY],
            port=port,
            scheme=scheme,
            token=values.get(TOKEN_KEY) or None,
            api_path=values.get(API_PATH_KEY) or DEFAULT_API_PATH,
            timeout_s=float(values.get(TIMEOUT_KEY) or DEFAULT_TIMEOUT_S),
            insecure_tls=_truthy(values.get(INSECURE_TLS_KEY)),
        )

    def _ssl_context(self) -> "ssl.SSLContext | None":
        if self.scheme != "https":
            return None
        if not self.insecure_tls:
            return ssl.create_default_context()
        # Explicit opt-in only (`insecure_tls`): a device's self-signed certificate would
        # otherwise make the local API unusable.  Never the default.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def url(self, leaf: str) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.api_path}/{leaf}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme is http/https only
                request, timeout=self.timeout_s, context=self._context
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise ZenSdkError(self._http_message(int(exc.code))) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ZenSdkError(f"no response within {self.timeout_s:g} s") from None
            raise ZenSdkError(str(reason)[:160]) from None
        except TimeoutError:
            raise ZenSdkError(f"no response within {self.timeout_s:g} s") from None
        if status != 200:
            raise ZenSdkError(self._http_message(status))
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ZenSdkError("the device answered with malformed JSON") from None

    @staticmethod
    def _http_message(status: int) -> str:
        if status == 401:
            return (
                "the device refused the request (401): the local API may be disabled "
                "(enable HEMS in the Zendure app) or the token is wrong"
            )
        return f"the device answered HTTP {status}"

    def read(self) -> dict[str, Any]:
        """``GET <api_path>/report`` -> the parsed report (never a non-object)."""
        request = urllib.request.Request(self.url("report"), headers=self._headers(), method="GET")
        payload = self._request(request)
        if not isinstance(payload, dict):
            raise ZenSdkError("the device answered with a non-object JSON payload")
        return payload

    def write(self, serial: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        """``POST <api_path>/write`` with the required ``sn``; returns the parsed reply."""
        self._message_id += 1
        body = json.dumps(
            {"id": self._message_id, "sn": serial, "properties": dict(properties)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url("write"), data=body, headers=self._headers(), method="POST"
        )
        payload = self._request(request)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ZenSdkError("the device rejected the property write")
        return payload if isinstance(payload, dict) else {"result": payload}


def report_problems(report: Any, serial: str | None = None) -> list[str]:
    """Identity/shape problems of a property report; empty when it is a usable Zendure report."""
    if not isinstance(report, dict):
        return ["the device answered with a JSON value, not a properties report"]
    properties = report.get("properties")
    if not isinstance(properties, dict) or not properties:
        return [
            "the report carries no properties (enable the device's local API: add HEMS in the "
            "Zendure app and exit to apply)"
        ]
    expected = str(serial or "").strip()
    reported = str(report.get("sn") or "").strip()
    if expected and reported and reported != expected:
        return ["the device reports a different serial than the one configured (check the serial)"]
    return []


class ZendureNativeDriver(BaseDriver):
    """A SolarFlow inverter driven through its own local zenSDK HTTP API."""

    driver_name = "zendure_native"
    default_tolerance_w = ZENDURE_NATIVE_TOLERANCE_W

    def __init__(self, config, ha, *, client: ZenSdkClient | None = None, **kwargs: Any) -> None:
        super().__init__(config, ha, **kwargs)
        self._client = client or ZenSdkClient.from_mapping(config.mapping)
        self._report: dict[str, Any] | None = None
        self._report_error: str | None = None

    # -- config-time validation --------------------------------------------------------

    @classmethod
    def validate_mapping(cls, mapping: Mapping[str, Any]) -> list[str]:
        return validate_connection(mapping)

    @classmethod
    def probe_config(cls, config) -> list[str]:
        """Save-time reachability/identity check (engine calls this before persisting).

        ``allow_unreachable`` skips the probe deliberately: the card then shows the read error
        instead of the save being refused.  A probe must never raise.
        """
        mapping = dict(getattr(config, "mapping", None) or {})
        if _truthy(mapping.get(ALLOW_UNREACHABLE_KEY)):
            return []
        problems = validate_connection(mapping)
        if problems:
            return problems
        try:
            client = ZenSdkClient.from_mapping(mapping)
            client.timeout_s = min(client.timeout_s, PROBE_TIMEOUT_CAP_S)
            report = client.read()
        except (DriverError, ZenSdkError, OSError) as exc:
            return [f"cannot read the Zendure device over its local API: {exc}"]
        return report_problems(report, mapping.get(SERIAL_KEY))

    # -- device reads ------------------------------------------------------------------

    def mapped_entities(self) -> list[str]:
        """This driver reads the device, never HA entities (the mapping holds connection data)."""
        return []

    def read_state(self) -> BatteryState:
        """One HTTP report per read; every ``_read_*`` below uses that snapshot."""
        with self._lock:
            self._refresh()
            state = super().read_state()
            if self._report_error:
                state.available = False
                state.stale = True
                if not state.last_error:
                    state.last_error = self._report_error
                return state
            state.extra.update(
                {
                    "packs": len(self._pack_data()),
                    "cell_voltage_v": self._cell_voltage(),
                    "charge_max_w": self._device_charge_max(),
                    "discharge_max_w": self._device_discharge_max(),
                    "measured_pair": self._power_is_measured(),
                    # The device's own settings as it reports them (read-only here).
                    "ac_mode": self._prop(AC_MODE_KEY),
                    "smart_mode": self._prop(SMART_MODE_KEY),
                    "input_limit_w": self._prop(INPUT_LIMIT_KEY),
                    "output_limit_w": self._prop(OUTPUT_LIMIT_KEY),
                }
            )
            return state

    def _available(self, power: float | None, soc: float | None) -> bool:
        return isinstance(self._report, dict) and bool(self._properties())

    def read_cell_voltage(self) -> float | None:
        """Optional driver hook (SPEC 9.2): lowest pack cell voltage in V, else None.

        The low-voltage guard reads this; it is never inferred from a pack total voltage.
        """
        with self._lock:
            if self._report is None:
                self._refresh()
            return self._cell_voltage()

    def _refresh(self) -> dict[str, Any] | None:
        try:
            report = self._client.read()
        except ZenSdkError as exc:
            self._report = None
            self._report_error = str(exc)[:200]
            return None
        self._report = report
        self._report_error = None
        return report

    def _properties(self) -> dict[str, Any]:
        report = self._report
        if not isinstance(report, dict):
            return {}
        properties = report.get("properties")
        return properties if isinstance(properties, dict) else {}

    def _pack_data(self) -> list[dict[str, Any]]:
        report = self._report
        packs = report.get("packData") if isinstance(report, dict) else None
        if not isinstance(packs, list):
            return []
        return [pack for pack in packs if isinstance(pack, dict)]

    def _prop(self, key: str) -> float | None:
        return parse_number(self._properties().get(key))

    def _measured_pair(self) -> tuple[float | None, float | None]:
        """``(gridInputPower, outputHomePower)``; both are required to call a flow measured."""
        return self._prop("gridInputPower"), self._prop("outputHomePower")

    def _read_limit_setpoint(self) -> float | None:
        """The device's own limits as one signed value (positive = charge, models.py sign)."""
        charge = self._prop(INPUT_LIMIT_KEY)
        discharge = self._prop(OUTPUT_LIMIT_KEY)
        if charge is None and discharge is None:
            return None
        return abs(charge or 0.0) - abs(discharge or 0.0)

    def _read_limit_setpoint_fresh(self) -> float | None:
        self._refresh()
        return self._read_limit_setpoint()

    def _read_power_fresh(self) -> float | None:
        self._refresh()
        return self._read_power()

    def _read_power(self) -> float | None:
        """Measured AC power when the complete pair is present, else the limit setpoint."""
        drawn, output = self._measured_pair()
        if drawn is not None and output is not None:
            return drawn - output
        return self._read_limit_setpoint()

    def _power_is_measured(self) -> bool:
        drawn, output = self._measured_pair()
        return drawn is not None and output is not None

    def _read_soc(self) -> float | None:
        properties = self._properties()
        if "electricLevel" not in properties:
            return None
        return parse_soc_pct(properties.get("electricLevel"))

    def _read_capacity(self) -> float | None:
        """The device reports pack voltages, not capacity in kWh: never fabricate one.

        ``capacity_kwh`` therefore stays the user's figure (SPEC 5) and ``extra`` carries the
        pack count/voltages for context.
        """
        return None

    def _read_pv_power(self) -> float | None:
        return self._prop("solarInputPower")

    def _read_temperature(self) -> float | None:
        """Enclosure temperature: raw ``hyperTmp`` is 0.1 K (docs/EXTERNAL_APIS.md section 6)."""
        raw = self._prop("hyperTmp")
        if raw is None:
            return None
        celsius = (raw - 2731.0) / 10.0
        return celsius if -50.0 <= celsius <= 150.0 else None

    def _cell_voltage(self) -> float | None:
        voltages = [
            raw * 0.01
            for pack in self._pack_data()
            for raw in [parse_number(pack.get("minVol"))]
            if raw is not None and raw > 0
        ]
        return min(voltages) if voltages else None

    def _device_charge_max(self) -> float | None:
        value = self._prop("chargeMaxLimit")
        return value if value is not None and value > 0 else None

    def _device_discharge_max(self) -> float | None:
        value = self._prop("inverseMaxPower")
        return value if value is not None and value > 0 else None

    # -- writes ------------------------------------------------------------------------

    def _clamp_to_device(self, target_w: float) -> float:
        """Clamp to the *device's own* maxima as well as the configured limits.

        A device that reports its maximum (``chargeMaxLimit``/``inverseMaxPower``) is never asked
        for more; a missing or zero maximum leaves the configured limit in force.
        """
        if self._report is None:
            # First use of this driver (or after a failed read): load the device's own maxima
            # before deciding what it may be asked for.  A failed read leaves them unknown and the
            # configured limits apply.
            self._refresh()
        charge = abs(float(self.config.max_charge_w))
        discharge = abs(float(self.config.max_discharge_w))
        high = min(charge, self._device_charge_max() or charge)
        low = min(discharge, self._device_discharge_max() or discharge)
        return clamp(float(target_w), -low, high)

    def _apply(self, power_w: float, *, dry_run: bool, allow_idempotent_skip: bool = True):
        """Clamp before the base pipeline, so requested/applied/read-back all agree."""
        return super()._apply(
            self._clamp_to_device(power_w),
            dry_run=dry_run,
            allow_idempotent_skip=allow_idempotent_skip,
        )

    def _idempotent_power(self) -> float | None:
        """The limit setpoint echo, unless the smart-mode state would change with this command.

        The device applies limits immediately and echoes them, so the echo is the truthful "where
        it is" answer.  ``smartMode`` is part of the command: when it disagrees with the command
        the setpoint implies, a write must happen even if the limits already match (otherwise a
        smart mode could keep running through what looked like a hold).
        """
        self._refresh()
        setpoint = self._read_limit_setpoint()
        if setpoint is None:
            return self._read_power()
        smart = parse_number(self._properties().get(SMART_MODE_KEY))
        wanted = 0 if abs(setpoint) <= self.tolerance_w else 1
        if smart is not None and int(round(smart)) != wanted:
            return None
        return setpoint

    def _plan_writes(self, target_w: float) -> list[_Write]:
        """One property write: acMode picks the direction, both limits carry magnitudes.

        This is the vendor's own command shape (docs/EXTERNAL_APIS.md section 6), including
        ``smartMode`` off for a hold.  A device with off-grid output keeps ``smartMode`` on in the
        vendor's implementation; that is not modelled, so a hold always writes 0.
        """
        live = abs(target_w) > self.tolerance_w
        properties = {
            SMART_MODE_KEY: 1 if live else 0,
            AC_MODE_KEY: AC_MODE_INPUT if live and target_w > 0 else AC_MODE_OUTPUT,
            OUTPUT_LIMIT_KEY: int(round(max(0.0, -target_w))),
            INPUT_LIMIT_KEY: int(round(max(0.0, target_w))),
        }
        return [_Write(DEVICE_WRITE_ID, ZEN_SDK_WRITE_SERVICE, properties)]

    def _perform(self, write: _Write) -> None:
        properties = write.value if isinstance(write.value, dict) else {}
        serial = str(self.config.mapping.get(SERIAL_KEY) or "")
        self._client.write(serial, properties)

    def _verify_writes(self, target_w: float) -> tuple[float | None, str | None]:
        """Verify the device's own limit echo first, then the measured flow.

        The device applies the write itself and reports the limits back, so a matching echo is
        real evidence; a command that never confirms inside the bounded window is a driver error
        (which is what makes the failsafe hold run), never a reported success.
        """
        limits, limit_problem = self._verify_against(
            target_w, self._read_limit_setpoint_fresh, "limit read-back"
        )
        if limit_problem is None:
            return limits, None
        flow, flow_problem = self._verify_against(
            target_w, self._read_power_fresh, "measured read-back"
        )
        if flow_problem is None:
            return flow, None
        return None, f"{limit_problem}; {flow_problem}; the device did not confirm the write"

    # -- introspection -----------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        caps.pop("entities", None)  # the mapping is connection data, not HA entities
        caps.update(
            {
                "native": True,
                "manager": None,
                "transport": "zenSDK HTTP",
                "connection": {
                    "host": self._client.host,
                    "port": self._client.port,
                    "scheme": self._client.scheme,
                    "serial": str(self.config.mapping.get(SERIAL_KEY) or ""),
                    "api_path": self._client.api_path,
                    "token_set": bool(self._client.token),
                    "timeout_s": self._client.timeout_s,
                    "insecure_tls": bool(self._client.insecure_tls),
                },
                "device_limits": {
                    "max_charge_w": self._device_charge_max(),
                    "max_discharge_w": self._device_discharge_max(),
                },
                "cell_voltage_v": self._cell_voltage(),
                "measured_pair": self._power_is_measured(),
                "read_error": self._report_error,
            }
        )
        return caps
