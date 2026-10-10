#!/bin/sh
# tools/audit.sh — PII / secret tripwire for this repository (SPEC.md R1 and section 14).
#
# Usage:  sh tools/audit.sh          (from anywhere; paths print relative to the repo root)
#
# Exit codes: 0 = clean, 1 = findings, 2 = the scanner itself could not run.
#
# Allowed in committed files: integration-level generic identifiers that are identical on every
# install, public API endpoints, reserved documentation placeholders, and obviously fake examples.
# Everything else that identifies a person, a house or an account is a finding.
#
# This is a tripwire, not a proof. Deliberate ceilings ("ponytail:"), the upgrade path being a real
# secret scanner such as gitleaks in CI:
#   * Coordinates: a decimal inside a lat/long range is a finding when it has >= 5 fractional
#     digits, when it has exactly 4 fractional digits on a line that names a location field
#     (latitude, longitude, geo, position, location, elevation, address, postcode, zip), or when it
#     is the first half of an unlabelled "lat, lon" pair with exactly 4 fractional digits each
#     (lat 0..90, lon 0..180, comma separated). A pair whose two values are both below 1 is treated
#     as normalised test data and skipped. Values below 0.01 in
#     magnitude are ignored, so fixed-point scale factors such as 1e-7 do not trip the gate. Still
#     missed: negative (southern/western) 4-decimal pairs, pairs separated by anything but a comma,
#     mixed decimal lengths, coordinates on separate lines and 1-3 decimal coordinates; conversely
#     any ordinary four-decimal pair in those ranges is a false positive.
#   * Private IPv4: only complete dotted quads in 10/8, 172.16/12 and 192.168/16. Loopback
#     (127.0.0.1) and bare prefixes such as the text "192.168." are intentionally not findings.
#   * Private / instance hostnames: a labelled host ending in .local, .lan, .home or .internal, the
#     Fritz!Box default domain, and "homeassistant" used as a host (a port or a URL authority).
#     Reserved placeholders are allowlisted in ALLOW_HOSTS. Still missed: other home-network
#     suffixes (myfritz.net, localdomain, a custom LAN domain), single-label hosts without a dot,
#     IPv6 literals and a bare reserved domain with no label (home.arpa). A compound language or CSS
#     selector whose class name is such a suffix word would be flagged; use an "audit-allow:" marker
#     for those.
#   * MAC / BSSID: six colon-separated two-hex-digit groups. Hyphen, dot and unseparated forms are
#     missed, and a six-group value that only looks hex, such as a clock-time list, is a false
#     positive.
#   * Bare hex identifiers: a standalone 32-character [0-9a-fA-F] run with a non-hex boundary (the
#     device_id / unique_id / serial shape) must contain at least one digit. An all-letter
#     32-character placeholder is not flagged, and a 32-hex run inside a longer digest (sha256, a
#     40-hex commit id) is not matched.
#   * Entity ids: flagged under known Home Assistant domains unless the object id is in an
#     obviously fake namespace (example_, fake_, test_, demo_, dummy_, mock_, sample_, synthetic_,
#     sem_), in a public integration namespace (zonneplan_, identical on every install), is an
#     obvious placeholder (missing, unknown, nonexistent, not_found, nope), names a public Home
#     Assistant service (turn_on, get_forecasts, ...) or, for non-sensor domains, lacks the
#     underscore this rule requires (which keeps plain code attribute access out of the noise).
#     Single-word real ids are still caught, because
#     sensor domains are matched broadly.
#   * Secrets: bearer tokens, JWTs, and token-like assignments whose value looks like a literal.
#     A bare lowercase word (indistinguishable from a variable name) is not flagged, and a bare
#     high-entropy string without a label is not detected at all.
#   * Serials: only labelled values (serial, sn, imei, unique_id, device_id) of at least eight
#     characters that contain a digit or an upper-case letter, or an all-caps hyphen shape whose left
#     part contains a letter (so all-digit export timestamps are not mistaken for serials). A test
#     placeholder such as an integration namespace plus a short lower-case word stays quiet.
#   * Line-level opt-out: a line containing "audit-allow:" is skipped. Use it only in prose that
#     must quote a forbidden shape, never to silence real data.

set -u

# shellcheck disable=SC1007  # CDPATH= is deliberate: it keeps CDPATH out of $root
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 2
cd "$root" || exit 2

files=$(mktemp) || exit 2
findings=$(mktemp) || exit 2
trap 'rm -f "$files" "$findings"' EXIT HUP INT TERM

# .git and the usual local/uncommitted junk are not scanned. Binary files are skipped by grep -I.
find . \
    \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache \
       -o -name node_modules -o -name .mypy_cache \) -prune -o \
    -type f -print | LC_ALL=C sort > "$files"

# Redaction: at most four leading characters of a match, so a scan never re-prints a value that may
# be sensitive (and never copies one into a log or a prompt).
redact() {
    printf '%s...' "$(printf '%.4s' "$1")"
}

# is_secret_value <matched assignment> — true when the value looks like a literal secret rather
# than a variable reference or a code expression.
is_secret_value() {
    v=${1#*[=:]}
    v=${v# }
    # Surrounding quotes and trailing code punctuation are not part of the value.
    v=$(printf '%s' "$v" | sed 's/[]),;:{}]*$//')
    quoted=0
    case "$v" in
        \"*) quoted=1; v=${v#\"} ;;
        \'*) quoted=1; v=${v#\'} ;;
    esac
    v=${v%\"}
    v=${v%\'}
    case "$v" in
        *'-}'*) return 1 ;;                # ${VAR:-} default expansion
        *[[:space:]]*) return 1 ;;         # prose, not a single value
    esac
    # A dot, dollar, parenthesis, bracket or angle bracket means an expression, not a literal.
    # shellcheck disable=SC2016  # the pattern is a regular expression, not an expansion
    printf '%s' "$v" | grep -q '[.$([<]' && return 1
    # Plainly synthetic labels: a test fixture is not a leaked credential.
    printf '%s' "$v" | grep -qiE '(example|fake|dummy|mock|sample|synthetic|placeholder|changeme|change_me|redacted|notsecret|test)' && return 1
    if [ "$quoted" = 1 ]; then
        [ "${#v}" -ge 6 ] || return 1
        case "$v" in
            *[0-9]*) return 0 ;;               # a digit makes it a value, not a word
            *[A-Z]*) return 0 ;;               # mixed or upper case
            *[!A-Za-z0-9_-]*) return 0 ;;      # symbols beyond a plain key-shaped word
        esac
        return 1                               # a plain lower-case word: a placeholder
    fi
    case "$v" in
        [A-Za-z_]*)
            case "$v" in
                *[!A-Za-z0-9_]*) return 0 ;;                  # symbol -> token-like
                *[A-Z]*[0-9]*|*[0-9]*[A-Z]*) return 0 ;;      # upper + digit -> token-like
            esac
            return 1                                          # bare word: a variable name
            ;;
    esac
    [ "${#v}" -ge 6 ] && return 0
    return 1
}

# is_serial_value <matched assignment> — true when a labelled value looks like a real device serial
# rather than a test placeholder or a variable reference.
is_serial_value() {
    v=$(printf '%s' "$1" | awk -F'[=:]' '{
        v = $NF
        gsub(/^[[:space:]]+/, "", v)
        gsub(/^["\047]/, "", v)
        gsub(/["\047,;:{}]+$/, "", v)
        print v
    }')
    [ "${#v}" -ge 8 ] || return 1
    printf '%s' "$v" | grep -qiE '(example|fake|test|demo|dummy|mock|sample|synthetic)' && return 1
    case "$v" in
        *[0-9]*) return 0 ;;
        *[A-Z]*) return 0 ;;
    esac
    return 1
}

# is_hex_id <matched token> -- true when the match is one standalone 32-character hex run (the
# device_id / unique_id / serial shape) that contains at least one digit. Boundary guards and
# quoting are stripped first; an all-letter run is a placeholder word, not an identifier.
is_hex_id() {
    v=$(printf '%s' "$1" | sed 's/^[^0-9A-Fa-f]*//; s/[^0-9A-Fa-f]*$//')
    [ "${#v}" -eq 32 ] || return 1
    case "$v" in
        *[0-9]*) return 0 ;;
    esac
    return 1
}

# rule <label> <ERE> [filter] [linefilter]
#   filter = secret     -> keep only matches whose value looks like a literal secret
#   filter = <ERE>      -> skip matches that match this allow-ERE (fake namespaces)
#   linefilter = <ERE>  -> keep the match only when its source line matches too
rule() {
    label=$1
    pattern=$2
    filter=${3:-}
    linefilter=${4:-}
    while IFS= read -r f; do
        # -I skips binary files; -o prints only the matched text; opt-out lines are skipped.
        grep -I -n -o -E -e "$pattern" -- "$f" 2>/dev/null |
            grep -v 'audit-allow:' |
            while IFS= read -r hit; do
                text=${hit#*:}
                # grep -o includes the single "guard" character before the keyword; drop it.
                case "$text" in
                    [A-Za-z0-9_]*) : ;;
                    ?*) text=${text#?} ;;
                esac
                if [ -n "$linefilter" ]; then
                    sed -n "${hit%%:*}p" -- "$f" | grep -qiE -e "$linefilter" || continue
                fi
                if [ "$filter" = secret ]; then
                    is_secret_value "$text" || continue
                elif [ "$filter" = serial ]; then
                    is_serial_value "$text" || continue
                elif [ "$filter" = hex ]; then
                    is_hex_id "$text" || continue
                elif [ -n "$filter" ]; then
                    printf '%s\n' "$text" | grep -qE -e "$filter" && continue
                fi
                printf '%s:%s: %s [%s]\n' "$f" "${hit%%:*}" "$(redact "$text")" "$label" \
                    >> "$findings"
            done
    done < "$files"
}

# Obviously fake entity id namespaces. sem_ is the add-on's own published contract (SPEC.md 12);
# zonneplan_ ids come from the public Zonneplan integration and are identical on every install
# (SPEC.md R1 allows integration-level generic identifiers); notify.mobile_service is a settings key
# from SPEC.md 3; the trailing alternatives are placeholders that API tests use for entities the
# test knows do not exist.
ALLOW_IDS='[.](example|fake|test|demo|dummy|mock|sample|synthetic|sem|zonneplan)_|notify[.]mobile_service|[.](missing|unknown|nonexistent|not_found|nope)$'

# Public Home Assistant service calls are dotted like entity ids but identical on every install.
ALLOW_SERVICE='[.](turn_on|turn_off|toggle|get_[a-z_]+|set_[a-z_]+|create|delete|reload|restart|purge|select_(option|first|last|next|previous))$'

# 1a. Plausible coordinates, unconditionally: >= 5 fractional digits with a magnitude of at least
#     0.01 (a fixed-point scale factor such as 1e-7 is not a coordinate).
rule coordinate '(^|[^0-9.])(([1-9][0-9]?|1[0-7][0-9])[.][0-9]{5,}|0[.](0[1-9][0-9]{3,}|[1-9][0-9]{4,}))([^0-9]|$)'

# 1b. Coordinates with exactly four fractional digits: only on a line that names a location field.
LOCATION_WORDS='(^|[^a-z_])(lat|lon|geo)[a-z_]*|(^|[^a-z_])(coordinate|position|location|elevation|address|postcode|zip)'
rule coordinate '(^|[^0-9.])(([1-9][0-9]?|1[0-7][0-9])[.][0-9]{4}|0[.](0[1-9][0-9]{2}|[1-9][0-9]{3}))([^0-9]|$)' '' "$LOCATION_WORDS"

# 1c. Unlabelled four-decimal coordinate pair "lat, lon" (the shape the final verifier planted):
#     lat 0..90 and lon 0..180, both exactly four decimals, comma separated. A pair whose two values
#     are both below 1 is ordinary normalised test data, not a place.
COORD_LAT='([0-9]|[1-8][0-9]|90)'
COORD_LON='([0-9]|[1-9][0-9]|1[0-7][0-9]|180)'
NORMALISED_PAIR='^0[.][0-9]{4}[[:space:]]*,[[:space:]]*0[.][0-9]{4}'
rule coordinate-pair "(^|[^0-9.])((${COORD_LAT})[.][0-9]{4}[[:space:]]*,[[:space:]]*(${COORD_LON})[.][0-9]{4})([^0-9]|$)" "$NORMALISED_PAIR"

# 2. Private IPv4 addresses (instance data). 127.x loopback is deliberately not a finding.
rule private-ip '(^|[^0-9.])((10[.][0-9]{1,3}|192[.]168|172[.](1[6-9]|2[0-9]|3[01]))[.][0-9]{1,3}[.][0-9]{1,3})([^0-9.]|$)'

# 3. sensor / binary_sensor entity ids: the main R1 risk (they carry device and user naming).
rule entity-sensor '(^|[^a-z0-9_.])(sensor|binary_sensor)[.][a-z0-9_]{2,}' "$ALLOW_IDS"

# 4. Other Home Assistant domains, only with an underscore in the object id (see ceiling above).
rule entity-domain '(^|[^a-z0-9_.])(light|switch|climate|cover|lock|fan|vacuum|media_player|camera|person|device_tracker|zone|calendar|notify|input_number|input_boolean|input_select|input_text|input_datetime|timer|counter|schedule|utility_meter|water_heater|siren|valve|humidifier|remote|button|todo|weather|sun|group|scene|script|automation|update|tts|conversation|number|select|text|date|datetime|time|event|image|alarm_control_panel|lawn_mower)[.][a-z0-9]+_[a-z0-9_]+' "$ALLOW_IDS|$ALLOW_SERVICE"

# 5. Credentials in the wild: bearer headers and JWTs.
rule credential '(^|[^A-Za-z0-9_.-])(Bearer|bearer|Token)[[:space:]]+[A-Za-z0-9._~+/=-]{20,}'
rule credential '(^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}[.]eyJ[A-Za-z0-9_-]{4,}'

# 6. token / password / api_key assignments whose value looks like a literal secret.
rule token-assignment '(^|[^A-Za-z0-9_])(token|password|passwd|pwd|api_?key|apikey|secret|access_?key|private_?key|client_?secret)[[:space:]]*[:=][[:space:]]*[^[:space:]]+' secret

# 7. Email addresses.
rule email '(^|[^A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+([.][A-Za-z0-9-]+)+'

# 8. Device serials: labelled values must look like a real serial (see is_serial_value), plus the
#    classic all-caps alphanumeric-with-hyphen shape.
rule device-serial '(^|[^A-Za-z0-9_])(serial|serial_?number|imei)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9][A-Za-z0-9._-]{4,}' serial
rule device-serial '(^|[^A-Za-z0-9_])(sn|device_?id|unique_?id)[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9][A-Za-z0-9._-]{4,}' serial
# Public protocol constants that look like serials but are identical on every install: the
# RFC 6455 WebSocket GUID used by haws.py.
ALLOW_SERIALS='258EAFA5|C5AB0DC85B11'
# A serial-shaped token must carry a letter left of the hyphen: an all-digit stamp such as the
# 20260101-120000 in an export filename is a timestamp, not a device serial.
rule device-serial '(^|[^A-Za-z0-9])[A-Z0-9]*[A-Z][A-Z0-9]*-[A-Z0-9]{5,}([^A-Za-z0-9]|$)' "$ALLOW_SERIALS"

# --- Private hostnames, MAC addresses and bare hex identifiers (final verification round) --------

# Allowlist for the hostname rules. Every entry is either a reserved placeholder or a public
# identifier that is identical on every install; nothing instance-specific may be added here:
#   ha.local                            documented dev placeholder, used across the tests
#   threading.local                     Python's thread-local class (same shape, not a hostname)
#   localhost, 127.0.0.1, 0.0.0.0, ::1  reserved loopback/unspecified hosts
#   example.com, example.org            reserved documentation domains
#   api.forecast.solar, api.energy-charts.info, energy-charts.info, github.com
#                                       public providers and the public repository host
# The last two groups cannot match the rules below (no private suffix, no "homeassistant" label);
# they are listed so the intended allowlist stays auditable in one place. The patterns are
# anchored to host-label boundaries on purpose: a substring match would let a longer hostname
# ending in the same word pass, which is exactly the instance data this allowlist must not hide.
ALLOW_HOSTS='(^|[^A-Za-z0-9-])(ha|threading)[.]local([^A-Za-z0-9-]|$)|(^|[^A-Za-z0-9-])(localhost|example[.](com|org))([^A-Za-z0-9-]|$)'

# 9. Private / instance hostnames: a labelled host that ends in a home-network suffix. The boundary
#    classes exclude "_" as well, so a code identifier such as internal.internal_objective stays quiet
#    while a real hostname does not (a host label cannot contain an underscore).
rule private-host '(^|[^A-Za-z0-9_-])[A-Za-z0-9][A-Za-z0-9-]*[.](local|lan|home|internal)([^A-Za-z0-9_-]|$)' "$ALLOW_HOSTS"
# 10. The FRITZ!Box default domain, with or without a host label.
rule private-host '(^|[^A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9-]*[.])?fritz[.]box([^A-Za-z0-9_-]|$)' "$ALLOW_HOSTS"
# 11. "homeassistant" used as a host: an explicit port, or a URL authority. The bare product name
#     and a public path such as github.com/homeassistant/core are not findings.
rule private-host '(^|[^A-Za-z0-9._-])homeassistant:[0-9]{1,5}' "$ALLOW_HOSTS"
rule private-host '(https?://|//)homeassistant([:/]|$)' "$ALLOW_HOSTS"

# 12. MAC / BSSID addresses: six colon-separated two-hex-digit groups (a longer run is not sliced).
rule mac '(^|[^0-9A-Fa-f:])([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}([^0-9A-Fa-f:]|$)'

# 13. Bare 32-character hex identifiers (device_id / unique_id / serial shape) without quotes.
rule hex-id '(^|[^0-9A-Fa-f])[0-9A-Fa-f]{32}([^0-9A-Fa-f]|$)' hex

count=$(wc -l < "$findings" | tr -d ' ')
if [ "$count" -gt 0 ]; then
    echo "audit: forbidden shapes found — see SPEC.md R1 (excerpts are deliberately truncated):"
    sed 's/^/  /' "$findings"
    echo "findings: $count"
    exit 1
fi

echo "audit: clean — $(wc -l < "$files" | tr -d ' ') files scanned"
exit 0
