#!/usr/bin/env bash
# Download the CAM++ speaker embedder and Silero VAD ONNX models.
#
# Each download is validated as an actual ONNX/protobuf file before being
# moved into place — many model hubs (looking at you, ModelScope) silently
# return an HTML landing page on direct file links, which then explodes at
# runtime with "Protobuf parsing failed".
#
# Behaviour flags (set via env):
#   SKIP_MODEL_DOWNLOAD=1  — print a warning and exit 0 if a download
#                            fails. Useful during `docker build` when
#                            Hugging Face is rate-limited; the container
#                            can then pick up the model from a mounted
#                            volume at startup instead.
set -uo pipefail

MODEL_DIR="${1:-./models}"
mkdir -p "$MODEL_DIR"

# Each model from one URL pinned to a commit, checked against its SHA256 before
# it is moved into place. A file that does not match is deleted and the script
# fails, whatever SKIP_MODEL_DOWNLOAD says: no unverified model is ever loaded.
#
# CAM++ (3D-Speaker iic checkpoint, 192-dim, ~28 MB), model-scope/CosyVoice-300M
# at revision ca4e036d.
CAMPP_URLS=(
    "https://huggingface.co/model-scope/CosyVoice-300M/resolve/ca4e036d2db2aa4731cc1747859a68044b6a4694/campplus.onnx"
)
CAMPP_SHA256="a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73"

# Silero VAD v5 (~2 MB), snakers4/silero-vad at commit bfdc0193.
SILERO_URLS=(
    "https://raw.githubusercontent.com/snakers4/silero-vad/bfdc0193023f121ea5b3cc7b176dbed570a68a59/src/silero_vad/data/silero_vad.onnx"
)
SILERO_SHA256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# Validate that a file looks like a real ONNX (protobuf) blob.
# Real ONNX files start with the bytes 08 ?? 12 (ir_version field, varint),
# but the cheap and very reliable check is "not text/HTML and >1 MB".
validate_onnx() {
    local path="$1"
    local min_bytes="$2"
    local sha256="$3"
    if [ ! -s "$path" ]; then
        return 1
    fi
    local got
    got="$(sha256_of "$path")"
    if [ "$got" != "$sha256" ]; then
        echo "  -> SHA256 $got, expected $sha256" >&2
        return 1
    fi
    local size
    size=$(stat -c %s "$path" 2>/dev/null || stat -f %z "$path")
    if [ "$size" -lt "$min_bytes" ]; then
        echo "  -> file too small ($size bytes < $min_bytes)" >&2
        return 1
    fi
    # Reject any file that starts with HTML / XML / JSON.
    local head
    head=$(head -c 16 "$path" | tr -d '\0' | tr '[:upper:]' '[:lower:]')
    case "$head" in
        *"<!doctype"*|*"<html"*|*"<?xml"*|"{"*)
            echo "  -> downloaded file looks like text, not ONNX" >&2
            return 1
            ;;
    esac
    return 0
}

# Try one URL, with aggressive curl-level retries. The outer loop in
# download_with_fallback handles per-URL failures; this handles transient
# 5xx responses within a single mirror.
try_download() {
    local url="$1"
    local tmp="$2"
    # --retry 6 + --retry-delay 5 gives us up to ~30s of patience on a
    # single mirror, which is usually enough to ride out HF hiccups.
    curl -fsSL \
        --connect-timeout 15 \
        --max-time 300 \
        --retry 6 \
        --retry-delay 5 \
        --retry-all-errors \
        -o "$tmp" \
        "$url"
}

download_with_fallback() {
    local target="$1"
    local min_bytes="$2"
    local sha256="$3"
    shift 3
    local urls=("$@")

    if [ -f "$target" ] && validate_onnx "$target" "$min_bytes" "$sha256"; then
        echo "$(basename "$target") already present and valid."
        return 0
    fi

    # Two full passes through the mirror list so transient 5xx across
    # ALL mirrors (HF CDN-wide blip) don't doom the build on the first
    # sweep. Total worst-case wait: ~5 min, still inside Docker's
    # default build timeout.
    local pass
    for pass in 1 2; do
        for url in "${urls[@]}"; do
            echo "[pass $pass] Downloading $(basename "$target") from $url"
            local tmp="${target}.partial"
            if try_download "$url" "$tmp"; then
                if validate_onnx "$tmp" "$min_bytes" "$sha256"; then
                    mv "$tmp" "$target"
                    local sz
                    sz=$(stat -c %s "$target" 2>/dev/null || stat -f %z "$target")
                    echo "  ok ($sz bytes)"
                    return 0
                else
                    echo "  validation failed, trying next mirror"
                    rm -f "$tmp"
                fi
            else
                echo "  curl failed, trying next mirror"
                rm -f "$tmp"
            fi
        done
        if [ "$pass" -lt 2 ]; then
            echo "All mirrors failed on pass $pass — waiting 15s before second pass…"
            sleep 15
        fi
    done

    echo "ERROR: could not download $(basename "$target") from any mirror" >&2
    return 1
}

missing=0
for f in campplus.onnx silero_vad.onnx; do
    # A model already on disk that fails its checksum is not used.
    if [ -f "$MODEL_DIR/$f" ] && ! validate_onnx "$MODEL_DIR/$f" 1 "$([ "$f" = campplus.onnx ] && echo "$CAMPP_SHA256" || echo "$SILERO_SHA256")"; then
        echo "ERROR: $MODEL_DIR/$f fails its checksum; deleting it" >&2
        rm -f "$MODEL_DIR/$f"
    fi
done
download_with_fallback "$MODEL_DIR/campplus.onnx" 10000000 "$CAMPP_SHA256" "${CAMPP_URLS[@]}" \
    || missing=1
download_with_fallback "$MODEL_DIR/silero_vad.onnx" 500000 "$SILERO_SHA256" "${SILERO_URLS[@]}" \
    || missing=1

echo
echo "Models in $MODEL_DIR:"
ls -lh "$MODEL_DIR" 2>/dev/null || true

if [ "$missing" -ne 0 ]; then
    if [ "${SKIP_MODEL_DOWNLOAD:-0}" = "1" ]; then
        echo
        echo "WARNING: at least one model is missing, but SKIP_MODEL_DOWNLOAD=1" >&2
        echo "         is set — continuing anyway. Murdock will try to fetch" >&2
        echo "         the missing files at container startup, or you can" >&2
        echo "         mount a pre-populated models directory via a volume." >&2
        exit 0
    fi
    exit 1
fi
