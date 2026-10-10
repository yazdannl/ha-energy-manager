# External API contracts

Verified reference for every outside system this add-on talks to. Written by WP1 (api-scout).

Every fact below is marked **[V]** (verified against a primary source: upstream source code, vendor docs,
or a read-only live call) or **[U]** (uncertain / not verified). Section 7 states what code must do
about every **[U]**.

Method: upstream source at the pinned revision + live READ-ONLY calls (see "Verification log" at the
end). No live payload, entity id, coordinate, price or key appears in this file; all examples are
obviously fake (`sensor.example_*`, `52.0/5.0`, `Example Street 1`). Live probes printed only derived
facts (HTTP status, key names, ratios, counts, offsets).

Codeability rules that follow from section 7 and never bend:

* Each provider is a pure `fetch(...) -> model` behind the registry; failures return an error, never a
  fabricated zero.
* Every price is normalised to EUR/kWh tax-included per `models.eur_per_kwh_from_raw`; the provider
  declares `unit` and (for fixed-point integers) `scale_divisor`. **Never guess a scale from magnitude.**
* Nothing from `message.info`, device registries, or raw payloads is logged, cached to git, or shown
  in the UI beyond what the user needs.

---

## 1. Forecast.Solar v1

Base URL `https://api.forecast.solar`. Keyless works today. Upstream reference implementation:
`home-assistant-libs/forecast_solar` (`src/forecast_solar/forecast_solar.py`, `models.py`) and
HA core `components/forecast_solar/coordinator.py`.

### Endpoints

| Purpose | Request |
| --- | --- |
| Single plane estimate | `GET /estimate/{lat}/{lon}/{declination}/{azimuth}/{kwp}` |
| Multi-plane estimate (personal key only) | `GET /{api_key}/estimate/{lat}/{lon}/{dec1}/{az1}/{kwp1}/{dec2}/{az2}/{kwp2}/...` |
| Key/plane validation | `GET /{api_key}/info`, `GET /check/{lat}/{lon}/{planes...}` |
| v2 API | does not exist (any `/v2/...` -> 404) **[V]** |

* Plane order is `declination` (tilt), `azimuth`, `kwp`; first plane is mandatory, up to 4 planes
  documented. **[V]** (lib source, docs)
* Keyless multi-plane is rejected: live `GET /estimate/52.0/5.0/35/0/1.0/30/180/2.0` -> **404**
  `"Requested function not found"`. HA core also refuses >1 plane without a key. **[V]**
* Personal key goes in the **path** (not a header): `https://api.forecast.solar/<key>/estimate/...`.
  Live fake key -> 404 `Page not found`. **[V]** (lib source + live). Key raises the rate-limit tier and
  unlocks multi-plane, `actual=`, and 15-min resolution on higher account tiers. **[V]** (lib source, docs)
* Always send `?time=utc` (or `?time=iso8601`). Without it, timestamps are `YYYY-MM-DD HH:MM:SS` in the
  plant's local time **without offset** (live: 19 chars, no `T`, no offset) and cannot be placed on the
  add-on's UTC timeline. **[V]** (live)

### Response shape (live-verified)

```
result.watts               { "<iso ts>": W }             power per interval
result.watt_hours_period   { "<iso ts>": Wh }            energy per interval
result.watt_hours_day      { "YYYY-MM-DD": Wh }          energy per day
result.watt_hours          {...}                         extra series (not needed)
message.code               0 = success, >0 = error
message.type               "success" | "error"
message.text               human text (empty on success)
message.ratelimit          { zone, period, limit, remaining }
message.info               { distance, latitude, longitude, place, time, time_utc, timezone }
```

**[V]** live `/estimate/52.0/5.0/35/0/1.0?time=iso8601`. The documented/assumed
`result.watthours.periods` and `result.watthours.day` keys **do not exist** in v1: use
`watt_hours_period` and `watt_hours_day`. The parser must also tolerate missing/empty `result` on
errors (`result` is `null` or a string then).

`message.info` echoes the plant's coordinates and reverse-geocoded `place` — private data. Never log,
store or forward it. **[V]**

### Resolution and horizon

* Keyless output is **hourly** rows between sunrise and sunset (live histogram: mostly 60-min steps,
  plus irregular first/last fill rows, ~26 rows over 2 days, one long overnight gap). `resolution=15|30|60`
  is documented for accounts that provide shorter periods; keyless it returned 200 but the free output
  stayed hourly (hourly rows observed for the default call and for `limit=3`). **[V]** (live; docs)
* Default horizon = today + tomorrow (2 days, `watt_hours_day` had 2 entries). `limit=1..8` is
  documented (8 = today + 7 days), but keyless `limit=3` still returned 2 days. **[V]** (live)
* Provider contract: interpolate the irregular rows onto the add-on's 15-min grid, clamp negative or
  missing watts to 0, never assume uniform deltas, and treat "no row for tomorrow" as a warning, not a
  crash.

### Parameters

* `declination`: panel tilt in degrees; 0 = horizontal, 90 = vertical, positive for northern
  hemisphere. **[V]** (lib docs)
* `azimuth`: API convention = **compass azimuth − 180**, i.e. 0 = South, −90 = East, +90 = West,
  range −180..180. Store the user's compass value and subtract 180 at call time. **[V]**
  (doc.forecast.solar "Find your azimuth" + HA coordinator source)
* `damping`: comma-separated `morning,evening`; single value applies to both. `damping_morning` /
  `damping_evening` override `damping` when either is present. 0 = untouched. Docs conflict on the
  range (API page: −0.25..5; damping page: 0..1 float); all forms were accepted live. **[V]** (docs + live)
* `horizon`: comma-separated horizon heights in **degrees (0..90), clockwise starting at North**,
  equally spaced (>=12 values for a useful resolution; 4 values are ignored per the API page);
  `horizon=0` disables the built-in PVGIS horizon; plane-specific `horizon1..horizon4` exist.
  **[V]** (docs + live accepted 12 values)
* `inverter` (kW/kVA, caps output), `loss` (0..100 %, estimate mode), `no_sun=1`, `full=1`,
  `start=`, `transit=1`, `actual=<kWh>` (documented for keyed accounts; keyless accepted it too and
  returned 200). **[V]** (docs/live)
* Do **not** pass `format=key-value|start-end`; it changes the map shape and buys nothing here.

### Errors, rate limits, caching

* Status codes: 200 ok; 400 bad request; 401/403 bad key; 404 unknown function/dead key;
  422 invalid location/plane; 429 rate limited; 502/503 unreachable (503 during scheduled
  maintenance). Error bodies still carry the `{result, message}` envelope. **[V]** (lib + docs; 404 live)
* Keyless limit: live headers `X-Ratelimit-Limit: 12`, `X-Ratelimit-Period: 3600`, per IP; the body's
  `message.ratelimit` repeats `{limit, remaining, period, zone}`. Lib reads
  `X-Ratelimit-Limit`, `X-Ratelimit-Period`, `X-Ratelimit-Remaining`, `X-Ratelimit-Retry-At`.
  **[V]**
* Exact 429 body text: **[U]**. Treat any non-200 as failure, surface `message.text` when the body is
  JSON, honour the ratelimit fields/headers, back off exponentially and serve the SQLite cache
  (`forecast_cache`, 30-min refresh per SPEC is compatible: 4 planes = 8 calls/h < 12/h).

---

## 2. Energy-Charts day-ahead price

Base `https://api.energy-charts.info`, keyless. Provider used by SPEC: the legacy `/price` endpoint.
A v2 family exists and is the better long-term target.

### Legacy `GET /price?bzn=NL` **[V]** (live)

```
license_info   "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"
unix_seconds   [epoch seconds, one per slot]        96 slots = one local day, 15-min steps
price          [float]                               same length as unix_seconds
unit           "EUR / MWh"
deprecated     false
```

* Default window = **today only** (`start` defaults to today, `end` defaults to `start`). Request
  another day with `start=YYYY-MM-DD&end=YYYY-MM-DD` (v2 documents ISO-8601 day strings and unix
  seconds; the legacy endpoint accepted `YYYY-MM-DD` live). Tomorrow's prices before the auction
  result: **404 with a plain-text body `no content available`**. **[V]** (live)
* Invalid `bzn` -> **400 plain text** (`'ZZ' is not the code for an available bidding zone...`), not
  JSON. **[V]** (live)
* `bzn=NL` is the Netherlands and is explicitly in the CC-BY/redistributable list; the v2 enum
  `BiddingZoneV2` contains `NL` (and many other zones). Validate the configured zone against that
  enum; do not guess. **[V]** (OpenAPI spec)
* Price unit is the wholesale day-ahead price, EUR/MWh, **excluding VAT and the Dutch energy tax**
  (source is Bundesnetzagentur | SMARD.de, i.e. exchange data). Normalise: `/1000` to EUR/kWh, then
  apply `price.vat_pct` + `price.energy_tax_eur_per_kwh` (+ import fee). **[V]** (unit + license
  metadata; ex-tax nature is the definition of a day-ahead auction price)
* Attribution: response `license_info` is the attribution string; CC BY 4.0 requires showing it
  (Energy-Charts.info / Fraunhofer ISE and the data source Bundesnetzagentur | SMARD.de). **[V]**
* Number of days returned: one per request; loop per local day. There is no partial "tomorrow" until
  the auction result is published (early afternoon CET). **[V]** (live 404 + v2 docs)
* **Observation (2026-10-10, provider work):** with no `start`/`end`, `bzn=NL` returned **404
  `no content available`** on two probes while `bzn=FR` returned 200 and an invalid code returned 400 -
  so NL is a valid zone for which this source can have no prices, and the 404 must be read as "no prices
  for this window" (the same body as the pre-auction case), never as "unknown zone". The provider turns
  it into a named, actionable failure rather than a silent fallback, and no other API is called for it.
  **[V]** (live)

### `GET /v2/price`, `/v2/price_current`, `/v2/price_next_day` (recommended upgrade)

```
{ schema_version, endpoint, country, bidding_zone, timezone, resolution:"PT15M", interval_minutes:15,
  unit:"EUR / MWh", generated_at, available_from, available_until,
  series:[{id:"day_ahead_price", name}],
  data:[{timestamp:"2026-01-01T13:30:00", values:{"day_ahead_price": <float>}}],
  attributes:{}, license:"CC BY 4.0 ...", deprecated:false }
```

**[V]** (live + OpenAPI 2.0)

* `data[].timestamp` is a **naive local** ISO string in the envelope's `timezone` (e.g.
  `Europe/Amsterdam`), not UTC — parse with that zone. `values` is keyed by series id. **[V]**
* `/v2/price_current` = one row + `attributes.valid_until` (refetch after it). `/v2/price_next_day` =
  full next local day, or **404 until publication**, live body = plain text "…results typically become
  available in the early afternoon CET…". OpenAPI documents 400/404 as `{"detail": "..."}`; live 404
  was plain text -> accept both. **[V]**
* `start`/`end` accept ISO-8601, `YYYY-MM-DD` (local) or unix seconds; empty `start` = today, empty
  `end` = `start`. **[V]** (OpenAPI)

---

## 3. Zonneplan dynamic price sensor (`zonneplan_one`)

Provider: **`fsaris/home-assistant-zonneplan-one`** (HACS custom component, HA domain
`zonneplan_one`) — not HA core. Sources:
`custom_components/zonneplan_one/const.py`,
`coordinators/electricity_prices_data_coordinator.py`, and the integration's sensor platform module.

### Scale and unit (the critical bit) — **[V]**, live

Both price sensors publish `value_factor = 1e-7` and unit **€/kWh**; the raw forecast
numbers are fixed-point integers. Live comparison of the sensor state with the raw value of the slot
containing `now` gave exactly **1e7** for both sensors, i.e.:

* `forecast[].electricity_price` (legacy hourly list) -> **divide by 10^7** to get EUR/kWh.
* `forecast[].price_tax_included.amount` (15-min list) -> **divide by 10^7** to get EUR/kWh.

The sensor state itself is already EUR/kWh (tax included); do not divide the state.

Both attributes live under the same attribute name `forecast`, on two different sensors:

| Sensor (integration-generated id, same on every install) | Entry keys | Cadence |
| --- | --- | --- |
| object id `zonneplan_current_quarter_hourly_electricity_tariff` (enabled by default) | `start_date, end_date, price_tax_included{amount}, price_tax_excluded{amount}, sustainability_score` | 15 min |
| object id `zonneplan_current_electricity_tariff` (legacy hourly; **disabled by default** in current versions) | `start_date, datetime, electricity_price, electricity_price_excl_tax, sustainability_score, tariff_group` | 60 min |

Both are `sensor` entities created with the fixed object ids above (identical on every install), so
self-discovery can match them without an instance-specific name.

Upstream code proof: `SENSOR_TYPES[ELECTRICITY_PRICES]` sets `value_factor=1e-7` for
`current_electricity_tariff`, `current_quarter_hourly_electricity_tariff` and the forecast sensors;
`prepare_legacy_prices()` builds `electricity_price` from `price_tax_included.amount`, so both shapes
are the same API integer.

### Coverage, past slots, staleness — **[V]**, live

**Look-alike to reject (2026-10-10, live):** the *gas* tariff sensor
(`zonneplan_current_tariff_gas`) publishes `forecast[].price_tax_included.amount` in the **same
fixed-point shape**, and its own state agrees with its single 24 h slot, so neither the shape nor the
state/slot comparison separates it from an electricity tariff. Only two things do: the unit is **€/m³**
(not in the €/kWh family) and the list has **one entry spanning 24 h** (no 15-minute/hourly grid).
Locating a tariff sensor therefore requires a kWh unit **and** ≥ 2 entries on a regular 15-minute or
hourly grid. **[V]** (live)

* 15-min sensor: 220 entries, 15-min cadence, from ≈ −24.6 h to ≈ +30.4 h, 98 entries fully in the
  past. Legacy hourly: 55 entries, 60-min cadence, ≈ −24.6 h .. +29.4 h, 25 in the past.
* The list **contains past slots** — the provider must filter (`end_date > now` for the 15-min shape,
  `start_date + 1 h > now` for the legacy shape) and deduplicate by `start_date`.
* Refresh: coordinator `update_interval = 15 min` (+ 60 s debounce) and a re-render scheduled at every
  15-min boundary. On API failure the coordinator keeps the previous `price_data` (the entity stays
  "available" with old content) -> staleness must be detected by **coverage and timestamps**, not by
  `unavailable`.

### VAT and energy tax — **[V]**

The API publishes `price_tax_included` and `price_tax_excluded`; the integration's price sensors are
intentionally built from the **included** amount. The live excluded/included ratio varies per slot
(≈0.5–0.65, the signature of fixed per-kWh levies plus VAT), which is only possible if the included
value carries VAT + energy tax + supplier fees. So `price_tax_included` really is the consumer price:
do **not** apply `price.vat_pct` / `price.energy_tax_eur_per_kwh` on top of it.

### Provider contract

1. Read the state for "now"; read `forecast` for the horizon; divide raw integers by 10^7.
2. Handle both entry shapes and both sensors; prefer the quarter-hourly one when present.
3. Require a future slot covering `now` and at least `optimizer.horizon_hours` of coverage; otherwise
   `stale=True` and fall back per SPEC §4.
4. Never rely on `sustainability_score` / `tariff_group`; never assume `end_date` exists.
5. A startup self-check compares the sensor state with the raw value of the current slot and disables
   the provider with a visible error if the scale does not match (guards future upstream changes).

---

## 4. Home Assistant REST API (add-on, `SUPERVISOR_TOKEN`)

Add-on config: `homeassistant_api: true`. Base URL `http://supervisor/core/api`, header
`Authorization: Bearer ${SUPERVISOR_TOKEN}`; the supervisor proxies to Core, so no HA URL/port is
needed and the token must never be read from disk or printed. The token maps to the Core HA user
`Supervisor`, which is created with `GROUP_ID_ADMIN` (`components/hassio/__init__.py`), so
admin-only endpoints work. **[V]** (dev docs + core source)

### `GET /api/config` — **[V]** live (keys)

`components, config_dir, config_source, country, currency, debug, elevation, external_url,
internal_url, language, latitude, location_name, logging, longitude, radius, recovery_mode,
safe_mode, state, time_zone, unit_system, version, allowlist_external_dirs, allowlist_external_urls,
whitelist_external_dirs`.

Use `latitude`/`longitude`/`time_zone`/`currency`/`country`/`version`; treat latitude/longitude as
private (never log/store); `latitude: 0.0` means unset — refuse to plan rather than invent a location.

### `GET /api/states` and bounding

* Array of every state, no server-side filter/pagination. Live: 1,322 states = **855,086 bytes
  (~640 B/state)** uncompressed; gzip is enabled when the body exceeds 1,024 bytes
  (`MIN_COMPRESSED_RESPONSE_SIZE`). Non-admin callers get only permitted entities. **[V]** (live +
  `components/api/__init__.py`)
* Bound it: read only mapped entities via `GET /api/states/<entity_id>` (200, or 404
  `{"message":"Entity not found."}`), or use the WS entity registry plus targeted reads. Never poll the
  full dump from the control loop. **[V]** (source)

### `POST /api/states/<entity_id>` (publish `sensor.sem_*`)

* **Admin only** (401 otherwise). Body `{"state": <string>, "attributes": {...}, "force_update": bool}`.
  201 for a new entity, 200 for an existing one, `Location` header, response = state object. 400 for
  invalid JSON, non-object body, missing `state`, invalid entity id, or non-string state. **[V]** (source)
* A POST-created entity is state-machine only: **no entity-registry entry, no `unique_id`, no device,
  no restore, and it disappears on HA restart** until republished. Recorder history works if not
  excluded; long-term statistics are compiled for `sensor` states that carry `state_class` (recorder
  filters on the state attribute, not the registry) but will have gaps across restarts. Publish
  idempotent state + attributes, read back, and republish after reconnect. **[V]** (source)
* `DELETE /api/states/<entity_id>` (admin) -> `{"message":"Entity removed."}` or 404. **[V]**

### `POST /api/services/<domain>/<service>`

* Body = flat service data. Target selectors go **top level**: `entity_id`, `device_id`, `area_id`,
  `floor_id`, `label_id` — REST has no separate `target` argument (only the WS API does). A nested
  `"target": {...}` is a 400. **[V]** (source: `TargetSelection` reads those keys from the call data)
* Response without a query string: JSON array of states that changed during the call (may be empty).
  With `?return_response`: `{"changed_states": [...], "service_response": <any>}`. **[V]** (docs + source)
* 400 if `return_response` is used on a service that returns nothing, or omitted when the service
  requires a response (e.g. the weather forecast service); 400 for unknown service / validation error;
  500 for HomeAssistantError; 401 unauthorized. Calls are blocking; request body cap is 16 MiB
  (`MAX_CLIENT_SIZE`). **[V]** (source)
* Driver rule: after every write, read the entity back and treat a mismatch as a driver error. Keep
  payloads minimal; never send `return_response` for `number.set_value` / `select.select_option`.

### `GET /api/history/period/<timestamp>`

* `filter_entity_id` is **required** (comma-separated); optional `end_time`, `minimal_response`,
  `no_attributes`, `significant_changes_only`; default window = last 24 h. Response = array of arrays
  (one inner array per entity), each item a state object, chronological ascending; with
  `minimal_response` only first/last rows carry attributes. **[V]** (docs)
* Recorder retention: `purge_keep_days` default **10**, `auto_purge` nightly at 04:12 local,
  `commit_interval` 5 s, and `include`/`exclude` filters can keep entities (and the whole recorder)
  away entirely. A normal purge removes events, states and the 5-minute short-term statistics only;
  the hourly long-term statistics table is never purged — use WS statistics for >10 days and for
  load profiles. **[V]** (recorder docs + `recorder/purge.py`)

### `POST /api/template`

* Admin only. Body `{"template": "...", "variables": {...}}`; returns the rendered **plain text**
  (`parse_result=False`); a render error is 400 `{"message":"Error rendering template: ..."}`. **[V]**
  (docs + source). Note: the `hassio` CLI classifies this POST as a write and refuses it under
  `--read-only`; that is a CLI safety choice, not an HA restriction.

### Errors and limits

* Codes used by Core: **400, 401, 404, 405** and 5xx. **Core does not emit 422** (no
  `UNPROCESSABLE` in the API views) — 422 belongs to Energy-Charts. **[V]** (source)
* No REST rate limiter in Core. The only IP-ban mechanism guards repeated failed UI logins
  (`ip_ban_enabled`, `login_attempts_threshold`) and does not apply to a valid supervisor token.
  Practical caps: 16 MiB request body; WS queue cap (section 5). **[V]** (source/docs)

---

## 5. Home Assistant WebSocket API

URLs: Core `ws://<ha-host>:8123/api/websocket`; add-on proxy `ws://supervisor/core/websocket`
(no port, requires `homeassistant_api: true`). The supervisor proxy performs its own handshake,
accepting `{"type":"auth","access_token": SUPERVISOR_TOKEN}` (legacy `api_password` also accepted), and
then talks to Core with its own privileged token. **[V]** (dev docs + supervisor `api/proxy.py`)

### Handshake and envelope — **[V]** (docs + source)

```
server -> {"type":"auth_required","ha_version":"..."}
client -> {"type":"auth","access_token":"..."}
server -> {"type":"auth_ok","ha_version":"..."}   |   {"type":"auth_invalid","message":"..."} + close
```

* Optional first command: `{"id":1,"type":"supported_features","features":{"coalesce_messages":1}}`.
  When coalescing is on the server may send one TEXT frame containing a JSON **array** of messages.
* Commands: `{"id": <int>, "type": "..."}`; results:
  `{"id":..,"type":"result","success":true,"result":...}` or
  `{"id":..,"type":"result","success":false,"error":{"code":"...","message":"..."}}`. Subscription
  events reuse the subscription id and `type:"event"`. Error codes include `invalid_format`,
  `unknown_command`, `not_found`, `unauthorized`, `id_reuse`, `service_validation_error`,
  `template_error`.

### `config/entity_registry/list` — **[V]** live field list

`area_id, categories, config_entry_id, config_subentry_id, created_at, device_id, disabled_by,
entity_category, entity_id, has_entity_name, hidden_by, icon, id, labels, modified_at, name,
next_name_part, options, original_name, platform, translation_key, unique_id`

* `config/entity_registry/get` returns the **extended** dict, which adds `aliases`, `capabilities`,
  `device_class`, `original_device_class`, `original_icon`. **[V]** (source)
* No admin requirement. The list **includes disabled entries** (`disabled_by != null`) unlike
  `list_for_display`; skip those.
* **SPEC correction**: Zendure-HA 1.4.4 unique ids are **not** prefixed with `zendure_ha.`; they are
  `<device_name_snake>_<entity_uniqueid>` (live sample prefixes confirmed). Discover Zendure entities
  by `platform == "zendure_ha"` **and** `translation_key`, never by a `zendure_ha.` unique-id prefix.
  **[V]** (source + live registry)

### `config/device_registry/list` — **[V]** live field list

`area_id, config_entries, config_entries_subentries, config_entry_id, config_subentry_id,
configuration_url, connections, created_at, disabled_by, entry_type, hw_version, id, identifiers,
labels, manufacturer, model, model_id, modified_at, name, name_by_user, next_name_part,
parent_device_id, primary_config_entry, serial_number, sw_version, via_device_id`

`serial_number`, `connections` and `identifiers` are device/serial data -> never log or commit.

### `recorder/statistics_during_period`

* Request: `start_time` (ISO string, required), `end_time` (optional), `statistic_ids` (required,
  non-empty list), `period` ∈ `5minute|hour|day|week|month|year` (required), `types` ⊆
  `change|last_reset|max|mean|min|state|sum` (default all), `units` (optional).
  `change` is derived from `sum`. **[V]** (source + live)
* Permission: no admin decorator on the command — any authenticated user; the add-on's supervisor
  token is admin anyway. **[V]** (source)
* Response `result` = `{statistic_id: [row, ...]}` ascending; each row has `start` and `end` in
  **epoch milliseconds** plus the requested fields. Live mean-type sensor (hour period) returned
  `start, end, last_reset, max, mean, min`; sum types return `state, sum, change` (+ `last_reset`).
  Impossible columns are dropped (`mean` for energy never appears). `day`/`week`/`month` rows are
  aligned in HA's local timezone; `hour` is a UTC-hour aggregate. **[V]** (source + live)
* `recorder/list_statistic_ids` gives `{statistic_id, name, source, unit_class,
  statistics_unit_of_measurement, display_unit_of_measurement, has_mean, has_sum, mean_type}`.
  **[V]** (live)

### Frames, pings, limits

* Server heartbeat: Core sends WebSocket **PING** control frames every 55 s (`heartbeat=55`), the
  supervisor proxy every 30 s; a client that cannot PONG is disconnected. A stdlib client **must**
  answer PING frames with PONG (and may also send `{"id":n,"type":"ping"}` -> `{"type":"pong"}`).
  **[V]** (core source; docs for the application-level ping)
* Outgoing queue cap `MAX_PENDING_MSG = 4096`; staying above `PENDING_MSG_PEAK = 1024` for more than
  `10 s` also disconnects; the writer limit is raised to 1 MiB after auth. **[V]** (core source)
* Inbound frames: aiohttp `WebSocketResponse` default `max_msg_size = 4 MiB` and HA does not override
  it, so keep client frames well under 4 MiB. **[V]** (aiohttp source)

---

## 6. Zendure SolarFlow control semantics (Zendure-HA 1.4.4)

Upstream `github.com/Zendure/Zendure-HA`, tag **1.4.4** (commit `e71b0b8`), HA component
`custom_components/zendure_ha`. Files: `manager.py`, `device.py`, `fusegroup.py`, `number.py`,
`select.py`, `translations/en.json`; live read-only confirmation of entity ranges/options.

### Manager: operation and manual power

Manager entities live on the "Zendure Manager" device and are discovered by translation key:
`operation` (select), `manual_power` (number), `global_soc`, `power`, `available_kwh`, `total_kwh`,
`operation_state`. **[V]** (source + live)

`select` options are exactly `off, manual, smart, smart_discharging, smart_charging, store_solar`
(live-verified); the internal `ManagerMode` enum is `OFF=0, MANUAL=1, MATCHING=2,
MATCHING_DISCHARGE=3, MATCHING_CHARGE=4, STORE_SOLAR=5`. **[V]** (source + live)

`number.<manager>_manual_power`: W, `device_class: power`, `[-12000, 12000]`, step 1 (live). The
manager reads it on each **P1-meter state change**:

* `manual_power > 0` -> `power_discharge(setpoint)` — battery AC output to the home.
* `manual_power < 0` -> `power_charge(setpoint)` — AC charging; the device property written is
  `inputLimit = -power`.
* `manual_power == 0` -> `power_charge(0, ...)` -> input/output limits 0.

**[V]** (`manager.py powerChanged`). Consequences a driver must honour:

1. `manual_power` is applied **system-wide**: one number for all devices of the manager, split across
   the fuse group by SoC weights. It is not per device.
2. Nothing is dispatched by the number write itself. The setpoint is applied on the next P1 event
   (`_p1_changed`), throttled to ≥2.2 s fast / ~4 s normal. If the configured P1 sensor is missing or
   silent, manual control silently does nothing — read back and raise a driver error.
3. On mode `off`, `update_operation()` performs a one-shot `power_off()` on every device.

### Per-device entities (translation keys) — **[V]** (live registry + source)

| translation_key | domain | live attributes |
| --- | --- | --- |
| `input_limit` | number | W, power, [0, abs(charge_limit)] (live SF800: 0..1000) |
| `output_limit` | number | W, power, [0, discharge_limit] (live SF800: 0..800) |
| `electric_level` | sensor | %, battery, measurement — **the SoC** |
| `soc_set` | number | %, soc, [0,100] — target SoC |
| `min_soc` | number | %, soc, [0,100] — discharge floor |
| `available_kwh` | sensor | kWh, energy_storage, **no state_class** |
| `total_kwh` | sensor | kWh, energy_storage, measurement |
| `soc_status` | sensor | calibration state: 0 = Operation, 1 = Auto-Calibration (**not SoC**) |
| `ac_mode` | select | input / output |
| `solar_input_power`, `output_home_power`, `grid_input_power`, `pack_input_power`, `output_pack_power` | sensor | W, power |
| `connection` | select | cloud / local (zenSDK) |
| `operation_state` | sensor | idle / charge / discharge / off |

* `soc_status` is a **trap**: SPEC lists it among SoC keys, but the authoritative SoC is
  `electric_level`; `soc_status` only reports calibration. Manager `global_soc` is the
  capacity-weighted SoC over devices seen in the last dispatch (0 when none) — display/aggregate use
  only; clamp per device with `electric_level` + `min_soc`/`soc_set`.
* `available_kwh = (electric_level − min_soc)/100 × total_kwh` (energy above the configured floor;
  updated on each SoC/min_soc property message), while `total_kwh` = sum of installed pack capacities
  (design capacity). Use `total_kwh` as `BatteryConfig.capacity_kwh`, `available_kwh` as current usable
  energy. **[V]** (source)
* Writing `input_limit`/`output_limit` publishes the raw device properties `inputLimit`/`outputLimit`
  without `acMode`/`smartMode`; while the manager is not `off` it overwrites them on the next P1 event.
  Direct per-device limit control is therefore a **fight with the manager** and is **[U]** — see §7.

### AC limits and the write path — **[V]** (source)

`ZendureDevice.setLimits(charge, discharge)` (per model, updated when the device reports
`inverseMaxPower` / `chargeLimit`) sets `charge_limit` (negative) and `discharge_limit` (positive) and
clamps writes; the number entities advertise `[0, abs(charge_limit)]` / `[0, discharge_limit]` but
`update_range()` only ever **widens** them, so the entity min/max can be wider than the current device
limit. Examples: SF800 `(-1000, 800)`, Hyper2000 `(-1200, 1200)`, SF2400 `(-2400, 2400)`,
SF2400 Pro/Plus `(-3200, 2400)`, Hub2000 `(0, 1200)` (no AC charging), ACE1500 `(-900, 800)`.

Command payloads the integration sends (properties, via MQTT / zenSDK / cloud depending on
`connection`):

* charge: `{smartMode: 1, acMode: 1, outputLimit: 0, inputLimit: -power}` (power < 0 internally)
* discharge: `{smartMode: 1, acMode: 2, outputLimit: power, inputLimit: 0}`
* off/hold: `{smartMode: 0, acMode: 2, outputLimit: 0, inputLimit: 0}`

Idempotence: the integration skips a write when
`|requested − (homeOutput − homeInput)| <= POWER_TOLERANCE (5 W)`. Emulate this in the driver.

### Smart modes (do not fight them; disable before manual control) — **[V]** (source)

* `smart` (grid matching): on every P1 event, charge from export surplus and discharge to cover
  import, targeting grid = 0.
* `smart_discharging`: discharge `max(produced, demand)`; never charge.
* `smart_charging`: charge from surplus; may discharge PV production but otherwise only charge.
* `store_solar`: charge from surplus only.
* All of them re-assert their own setpoint on every P1 event, so an external controller that writes
  device limits while any smart mode is active is overwritten within seconds.

### Minimal write sequences (recommended driver behaviour)

```
(a) charge at X W     select operation -> "manual"; number manual_power -> -X
(b) discharge at Y W  select operation -> "manual"; number manual_power -> +Y
(c) hold at 0 W       select operation -> "manual"; number manual_power -> 0
```

* Write `operation` before the first power write; verify the select state read-back.
* After the manual_power write, read back `output_home_power`/`grid_input_power` and
  `electric_level` within a bounded timeout (the next P1 event). No change within timeout -> driver
  error, retry once, then hold.
* "Hold" only means **AC = 0**. DC PV keeps charging the battery: `acMode`/`inputLimit`/`outputLimit`
  govern AC only, `solar_input_power` is independent, and at SOCFULL the surplus can bypass to the
  home (`exports_bypass` / `gridReverse`). Never assume SoC follows the AC plan; keep the add-on's own
  SoC clamp and treat DC charging as non-dispatchable.
* On shutdown/stop-now: set `manual_power = 0` (or `operation = off`, which triggers a one-shot
  `power_off()`). There is no watchdog: the manager keeps re-applying the last manual setpoint on every
  P1 event, so the device holds its last AC power indefinitely if the controller vanishes. Device
  firmware still enforces `soc_set`/`min_soc`.
* Multi-battery: because manual_power is system-wide, model the Zendure manager as **one** aggregated
  `BatteryConfig` (capacity = `total_kwh`, limits from the model/entity limits).

### Rate limits and polling

* Integration self-throttle: ≥2.2 s between fast dispatches, ~4 s normal; device property polling
  every 60 s (`SCAN_INTERVAL`). No documented device-side write-rate budget. **[V]** (source)
* Driver guard: at most one setpoint write per `control.tick_seconds` (30 s) per battery, idempotent
  skip within 5 W read-back, verify after each write. Device/cloud-side throttling is **[U]**.

---

## 7. Defensive assumptions (what code must do for every **[U]**)

| # | Uncertain fact | Required code behaviour |
| --- | --- | --- |
| 1 | Forecast.Solar exact 429 body | Do not parse it. Any non-200 = failure; read `message.text` only if the body is JSON; honour `message.ratelimit` + `X-Ratelimit-*`; exponential backoff; serve `forecast_cache`. |
| 2 | Forecast.Solar keyless horizon (`limit` ignored) | Treat keyless horizon as 2 days; refresh every 30 min; if tomorrow's rows are missing, keep today's with `stale=True` and log `forecast_unavailable`; with >1 plane and no key, make one single-plane call per plane inside the 12/h budget or refuse with a clear message. |
| 3 | Forecast.Solar non-uniform rows | Interpolate sunrise/sunset fill rows and the overnight gap onto the 15-min grid; clamp negatives to 0; never assume equal deltas. |
| 4 | Forecast.Solar `damping` range (−0.25..5 vs 0..1) | Configurable constant clamped to `[0, 1]`; on 400/422 drop the parameter and retry once; never let a damping value fail the whole forecast. |
| 5 | Forecast.Solar `message.info` privacy | Never log, store, or send `place`/lat/lon; keep only parsed watts/Wh in memory. |
| 6 | Forecast.Solar keyed multi-plane path (not exercised with a real key) | Build exactly `/{key}/estimate/{lat}/{lon}/{planes...}`; validate a user key with `/{key}/info` first (expect `message.code == 0`); treat 401/403/404 as invalid key and keep keyless mode. |
| 7 | Energy-Charts tomorrow availability | Poll normally; 404 (plain text or `{"detail"}`) = "not published yet": keep the previous cache, mark `stale=True`, retry after ~13:00 CET; never fabricate tomorrow. 400 = bad `bzn` -> config error + alert. |
| 8 | Energy-Charts v2 `timestamp` timezone | Parse naive timestamps with the envelope `timezone`; never as UTC; fall back to legacy `unix_seconds` (true epoch) if in doubt. |
| 9 | Energy-Charts tax treatment | Prices are ex-tax EUR/MWh: always `/1000` then apply configured VAT/energy tax/import fee; never hardcode tax values. |
| 10 | Zonneplan raw scale | Keep `1e7` as a per-source constant; on first use compare sensor state with the current slot raw value and disable the provider + alert on mismatch. |
| 11 | Zonneplan entry shape varies by version/sensor | Accept both `electricity_price` and `price_tax_included.amount`; ignore `tariff_group`/`sustainability_score`; handle missing/None dates by dropping the entry. |
| 12 | Zonneplan upstream failure keeps the attribute available | Detect staleness from coverage (`last future slot >= now + horizon`) and entity timestamps; require ≥1 slot covering `now`; otherwise `stale=True` + fallback chain. |
| 13 | Zonneplan legacy sensor disabled by default | Discovery must look for both price sensors; if neither is enabled, surface which entity to enable instead of failing silently. |
| 14 | `POST /api/states` not persistent | Read back after publish; republish on HA reconnect and after restarts; never assume statistics continuity; always send `state` as a string. |
| 15 | REST service target semantics | Send `entity_id`/`device_id`/... at the top level, never a nested `target` (400); no `return_response` for native service calls; map 400/401/404/500 to driver errors with a visible alert. |
| 16 | HA "success" of a service call | Always read the entity back; a 200 only means the service ran. Treat a mismatch > tolerance as `driver_error` and hold. |
| 17 | WS frame/queue limits and pings | Answer PING control frames promptly; cap client frames ≪4 MiB; handle coalesced JSON-array frames; reconnect with backoff on `auth_invalid`/close. |
| 18 | WS statistics optional fields | Request exactly the `types` you need; do not assume `mean` for energy or `sum` for power; treat `start`/`end` as ms epoch; `change` is relative to the previous row (first row baseline is the sum before `start_time`); day/week/month rows are local-timezone aligned. |
| 19 | Entity registry disabled entries / unique ids | Filter `disabled_by != null`; key discovery on `platform` + `translation_key`, never on a `zendure_ha.` unique-id prefix (does not exist). |
| 20 | Zendure per-device limit writes | Unverified/undocumented and overwritten by the manager: keep the Zendure manager aggregated (one battery, `manual_power`), or gate per-device mode behind an explicit experimental flag; never mix with an active smart mode. |
| 21 | Zendure manual writes need a P1 event | After every manual write, poll read-back within the next tick(s); if the configured P1 sensor is absent or silent, refuse to control with a driver error instead of reporting success. |
| 22 | Zendure "hold" is AC-only | Keep the add-on's own SoC/power clamps; expect DC PV charging during hold; never claim a SoC target was held. |
| 23 | Zendure per-model per-firmware limits change at runtime | Clamp to `min(configured max, live entity min/max, observed limit)`; re-read limits periodically; verify read-back power. |
| 24 | HA location may be unset (`latitude: 0.0`) | `resolve_location` fails loudly; solar disabled with a visible warning; never invent coordinates. |
| 25 | Full state dump size | Never poll `/api/states` in the tick loop; resolve entity ids once (registry / user mapping) and read only mapped entities. |
| 26 | External rate budgets | Forecast.Solar keyless 12/h/IP (≤8/h planned); Energy-Charts undocumented -> 1 call per refresh with cache; Zendure no documented budget -> ≤1 write per tick, idempotent. |

---

## Verification log (what was run, no secrets, no instance values)

Read-only HA CLI (always `HASSIO_READONLY=true` or `--read-only`; never `settings get --show-token`):

```
hassio status
hassio config --format json | <filter printing key names only>
hassio states --count                              # 1322
hassio states --format json | wc -c                # 855086 (uncompressed)
hassio ws call -T config/entity_registry/list      # field names + zendure_ha translation_keys
hassio ws call -T config/device_registry/list      # field names
hassio ws call -T recorder/list_statistic_ids
hassio ws call -T recorder/statistics_during_period -d '{...}'   # row keys + ms timestamps
hassio states <price sensor> | <filter printing only scale ratio/counts/offsets>
hassio states select.example_battery_manager_operation | <options only>
```

External calls (structural output only; synthetic `52.0/5.0` plant; no live payload kept):

```
curl 'https://api.forecast.solar/estimate/52.0/5.0/35/0/1.0?time=utc'
curl 'https://api.forecast.solar/estimate/52.0/5.0/35/0/1.0/30/180/2.0?time=utc'   # 404
curl 'https://api.energy-charts.info/price?bzn=NL'
curl 'https://api.energy-charts.info/price?bzn=ZZ'                                 # 400 text
curl 'https://api.energy-charts.info/price?bzn=NL&start=<tomorrow>&end=<day+2>'     # 404 text
curl 'https://api.energy-charts.info/v2/price?bzn=NL'
curl 'https://api.energy-charts.info/v2/price_next_day?bzn=NL'                      # 404 text
curl 'https://api.energy-charts.info/openapi.json'
```

Sources (pinned where it matters):

* Forecast.Solar: `https://doc.forecast.solar/api`, `/damping`, `/horizon`, `/find_your_azimuth`;
  `https://raw.githubusercontent.com/home-assistant-libs/forecast_solar/master/src/forecast_solar/{forecast_solar,models}.py`;
  HA core `components/forecast_solar/coordinator.py`.
* Energy-Charts: `https://api.energy-charts.info/openapi.json` (API 2.0), live `/price`, `/v2/*`.
* Zonneplan: `https://github.com/fsaris/home-assistant-zonneplan-one` (`const.py`,
  `coordinators/electricity_prices_data_coordinator.py`, sensor platform module).
* HA REST/WS: `https://developers.home-assistant.io/docs/api/rest/`, `.../api/websocket`,
  `https://developers.home-assistant.io/docs/add-ons/communication/`; core source
  `components/api/__init__.py`, `helpers/{target,entity_registry,device_registry}.py`,
  `components/recorder/websocket_api.py`, `components/recorder/statistics.py`,
  `components/websocket_api/{http,const,auth}.py`, `components/http/server.py`,
  `components/hassio/__init__.py`; supervisor `supervisor/api/proxy.py`;
  aiohttp `aiohttp/web_ws.py`; recorder docs.
* Zendure: `https://github.com/Zendure/Zendure-HA` tag `1.4.4` (`manager.py`, `device.py`,
  `fusegroup.py`, `number.py`, `select.py`, `translations/en.json`, `devices/*.py`).
