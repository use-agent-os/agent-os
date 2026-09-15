#!/usr/bin/env bash
# Render a .pptx file to per-slide JPGs for visual QA.
#
# Pipeline:
#   .pptx --(soffice headless)--> .pdf --(pdftoppm)--> slide-NN.jpg
#
# Usage:
#   render_thumbs.sh deck.pptx                       # → deck-01.jpg, deck-02.jpg, ...
#   render_thumbs.sh deck.pptx --out-dir thumbs      # write into thumbs/
#   render_thumbs.sh deck.pptx --dpi 200             # higher resolution (default 150)
#   render_thumbs.sh deck.pptx --range 3-5           # render only slides 3..5
#   render_thumbs.sh deck.pptx --range 3             # render only slide 3
#   render_thumbs.sh deck.pptx --range 3-            # render slide 3 to the end
#   render_thumbs.sh deck.pptx --range -5            # render slides 1..5
#
# Output:
#   <out_dir>/<basename>-NN.jpg     per slide
#   <out_dir>/<basename>.pdf        intermediate, kept for re-rendering
#
# Exit codes:
#   0  ok
#   1  bad arguments / file missing
#   2  soffice missing
#   3  pdftoppm missing
#   4  conversion failed
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  render_thumbs.sh <deck.pptx> [--out-dir DIR] [--dpi N] [--range SELECTOR]

--range accepts a single slide (3), an open start (-5 for 1..5),
an open end (3- for 3..last), or a closed range (3-5).
EOF
  exit 1
}

if [[ $# -lt 1 ]] || [[ "${1:-}" == "-h" ]] || [[ "${1:-}" == "--help" ]]; then
  usage
fi

input="$1"
shift

out_dir=""
dpi=150
range=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --dpi)     dpi="${2:-}";     shift 2 ;;
    --range)   range="${2:-}";   shift 2 ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

if [[ ! -f "$input" ]]; then
  echo "File not found: $input" >&2
  exit 1
fi

# Parsed here rather than beside pdftoppm below: a selector this script will
# reject is worth rejecting before soffice spends a minute rendering a deck
# whose thumbnails are then thrown away. pdftoppm takes the two bounds
# separately (-f, -l) and either may stand alone, which is what lets an
# open-ended selector map onto it at all.
range_args=()
if [[ -n "$range" ]]; then
  from=""
  to=""
  if [[ "$range" =~ ^([0-9]+)$ ]]; then
    from="${BASH_REMATCH[1]}"
    to="${BASH_REMATCH[1]}"
  elif [[ "$range" =~ ^([0-9]+)-$ ]]; then
    from="${BASH_REMATCH[1]}"
  elif [[ "$range" =~ ^-([0-9]+)$ ]]; then
    to="${BASH_REMATCH[1]}"
  elif [[ "$range" =~ ^([0-9]+)-([0-9]+)$ ]]; then
    from="${BASH_REMATCH[1]}"
    to="${BASH_REMATCH[2]}"
  else
    echo "Invalid --range '$range'; expected N, N-, -N or FROM-TO (e.g. 3, 3-, -5, 3-5)" >&2
    exit 1
  fi
  # Slides are 1-based. Without this, --range 0 reaches pdftoppm and fails
  # there as a conversion error, long after the argument that caused it.
  if [[ "${from:-1}" -lt 1 ]] || [[ "${to:-1}" -lt 1 ]]; then
    echo "Invalid --range '$range'; slide numbers start at 1" >&2
    exit 1
  fi
  # A reversed range reads as the range it describes, the way merge.py and
  # split.py already read one. Passed through as written it rendered nothing
  # and still exited 0.
  if [[ -n "$from" ]] && [[ -n "$to" ]] && [[ "$from" -gt "$to" ]]; then
    swap="$from"
    from="$to"
    to="$swap"
  fi
  if [[ -n "$from" ]]; then
    range_args+=(-f "$from")
  fi
  if [[ -n "$to" ]]; then
    range_args+=(-l "$to")
  fi
fi

basename="$(basename "$input" .pptx)"
if [[ -z "$out_dir" ]]; then
  out_dir="$(dirname "$input")"
fi
mkdir -p "$out_dir"

# Resolve soffice. macOS Brew + Linux package both expose `soffice`.
# Some macOS installs only have `/Applications/LibreOffice.app/Contents/MacOS/soffice`.
soffice_bin="$(command -v soffice || true)"
if [[ -z "$soffice_bin" ]]; then
  if [[ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]]; then
    soffice_bin="/Applications/LibreOffice.app/Contents/MacOS/soffice"
  fi
fi
if [[ -z "$soffice_bin" ]]; then
  cat >&2 <<'EOF'
soffice not found. Install LibreOffice:
  macOS:        brew install libreoffice
  Debian/Ubuntu: sudo apt-get install -y libreoffice
  Windows:      install LibreOffice and add `program/` to PATH
EOF
  exit 2
fi

if ! command -v pdftoppm >/dev/null 2>&1; then
  cat >&2 <<'EOF'
pdftoppm not found. Install Poppler:
  macOS:         brew install poppler
  Debian/Ubuntu: sudo apt-get install -y poppler-utils
  Windows:       install Poppler-Windows or use the version bundled with LibreOffice
EOF
  exit 3
fi

# Step 1: pptx -> pdf
# soffice writes <basename>.pdf into --outdir; do that into out_dir directly.
if ! "$soffice_bin" --headless --norestore --nologo --nofirststartwizard \
    --convert-to pdf --outdir "$out_dir" "$input" >/dev/null; then
  echo "soffice failed to render $input" >&2
  exit 4
fi
pdf_path="$out_dir/$basename.pdf"
if [[ ! -f "$pdf_path" ]]; then
  echo "Expected $pdf_path but it was not produced" >&2
  exit 4
fi

# Step 2: pdf -> per-page jpg
if ! pdftoppm -jpeg -r "$dpi" "${range_args[@]}" \
    "$pdf_path" "$out_dir/$basename"; then
  echo "pdftoppm failed" >&2
  exit 4
fi

echo "$pdf_path"
ls "$out_dir/$basename"-*.jpg 2>/dev/null || true
